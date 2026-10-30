"""API 层测试：用 httpx.ASGITransport 在进程内打真实 HTTP 请求。

为什么用 ASGITransport 而不是 TestClient：
- TestClient 走同步包装，会在自己线程里起事件循环，和后台 asyncio.Task
  （规划任务）容易抢同一个 loop，导致后台任务被打断。
- ASGITransport + AsyncClient 直接在**当前**事件循环里跑，后台 task 与请求
  共享同一个 loop，语义和 uvicorn 一致。

LLM 全部换成确定性替身（同 test_graph_e2e 的做法），这样测的是真实图 + 真实
API 编排，只把"模型判断"固定住，测试可复现。
"""
from __future__ import annotations

import asyncio
import json
from datetime import date
from typing import AsyncIterator

import httpx
import pytest

from travel_planner.api.app import create_app
from travel_planner.api.tasks import task_store
from travel_planner.graph.nodes import budget as budget_mod
from travel_planner.graph.nodes import poi as poi_mod
from travel_planner.graph.nodes import route as route_mod
from travel_planner.graph.nodes import supervisor as sup_mod


# --------------------------------------------------------------------------- #
# 确定性 LLM 替身
# --------------------------------------------------------------------------- #
@pytest.fixture
def deterministic_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _sup(state):
        from travel_planner.models.routing import NextStep, RoutingDecision

        if not state.get("pois"):
            return RoutingDecision(next=NextStep.POI, reason_code="initial")
        review = state.get("review")
        if review is not None and review.passed:
            return RoutingDecision(next=NextStep.END, reason_code="done")
        if not state.get("route_order"):
            return RoutingDecision(next=NextStep.ROUTE, reason_code="need_route")
        if state.get("budget_report") is None:
            return RoutingDecision(next=NextStep.BUDGET, reason_code="need_budget")
        if state.get("itinerary") is None:
            return RoutingDecision(next=NextStep.ITINERARY, reason_code="need_itinerary")
        return RoutingDecision(next=NextStep.REVIEW, reason_code="need_review")

    async def _poi(candidates, request, budget_pressure=None):
        from travel_planner.graph.nodes.poi import PoiSelection

        pool = list(candidates)
        if budget_pressure:
            pool.sort(key=lambda p: (p.ticket_price or 0.0, p.poi_id))
        else:
            pool.sort(key=lambda p: p.poi_id)
        want = max(1, request.max_poi_per_day) * request.days
        return PoiSelection(
            selected_poi_ids=[p.poi_id for p in pool[:want]],
            reason="budget_cut_for_cost" if budget_pressure else "match_prefs",
        )

    async def _route_llm(order, segments, request):
        from travel_planner.graph.nodes.route import RouteNarrative

        return RouteNarrative(day_notes=[f"第{i + 1}天行程紧凑" for i in range(len(order))])

    async def _budget_llm(request, ticket, transport, options, within):
        from travel_planner.graph.nodes.budget import BudgetPlan

        if not options:
            return BudgetPlan(star_level=0, reason_code="no_hotel")
        levels = sorted(int(o["star_level"]) for o in options)
        lv = levels[0] if not within else levels[len(levels) // 2]
        return BudgetPlan(
            star_level=lv,
            reason_code="budget_first" if not within else "balanced",
            note="按预算选档",
        )

    monkeypatch.setattr(sup_mod, "call_supervisor_llm", _sup)
    monkeypatch.setattr(poi_mod, "call_poi_llm", _poi)
    monkeypatch.setattr(route_mod, "call_route_llm", _route_llm)
    monkeypatch.setattr(budget_mod, "call_budget_llm", _budget_llm)


# --------------------------------------------------------------------------- #
# 客户端 fixture
# --------------------------------------------------------------------------- #
@pytest.fixture
async def client(tmp_path) -> AsyncIterator[httpx.AsyncClient]:
    """每个测试一个独立 sqlite 文件，互不污染。"""
    task_store.clear()
    app = create_app(str(tmp_path / "api_test.sqlite"))
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            yield c


BEIJING = {
    "destination": "北京",
    "start_date": "2026-11-01",
    "days": 3,
    "travelers": 1,
    "total_budget": 3000,
    "preferences": ["历史", "美食"],
    "transport_pref": "public",
}


async def _wait_done(client: httpx.AsyncClient, task_id: str, timeout: float = 30.0) -> dict:
    """轮询直到任务离开 queued/running。返回 GET /plan/{id} 的 data。"""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        r = await client.get(f"/plan/{task_id}")
        assert r.status_code == 200, r.text
        data = r.json()["data"]
        if data["status"] not in ("queued", "running"):
            return data
        await asyncio.sleep(0.05)
    raise AssertionError(f"任务 {task_id} 超时未完成")


# --------------------------------------------------------------------------- #
# 1. POST /plan：立刻返回 task_id
# --------------------------------------------------------------------------- #
async def test_submit_returns_task_id_immediately(client, deterministic_llm) -> None:
    """POST /plan 必须**不等规划跑完**就返回 202 + task_id。"""
    r = await client.post("/plan", json=BEIJING)

    assert r.status_code == 202, r.text
    body = r.json()
    assert body["ok"] is True
    data = body["data"]
    assert len(data["task_id"]) == 12
    assert data["status"] in ("queued", "running")
    assert data["status_url"] == f"/plan/{data['task_id']}"
    assert data["stream_url"] == f"/plan/{data['task_id']}/stream"

    await _wait_done(client, data["task_id"])


# --------------------------------------------------------------------------- #
# 2. GET /plan/{id}：从 Checkpointer 读结果
# --------------------------------------------------------------------------- #
async def test_get_plan_returns_itinerary_from_checkpointer(client, deterministic_llm) -> None:
    """跑完后结果必须完整，且确实来自 Checkpointer（新建连接也能读到）。"""
    sub = (await client.post("/plan", json=BEIJING)).json()["data"]
    data = await _wait_done(client, sub["task_id"])

    assert data["status"] == "completed", data
    assert data["partial"] is False

    it = data["itinerary"]
    assert it is not None, "必须有行程"
    assert len(it["days"]) == 3
    assert it["ordered_poi_ids"], "行程不能为空"
    assert all(d["schedule"] for d in it["days"]), "每天都要有安排"

    # 预算不超支
    assert data["budget"]["within_budget"] is True

    # review 通过
    assert data["review"]["passed"] is True

    # POI 明细也带上了
    assert len(data["pois"]) > 0
    assert data["pois"][0]["poi_id"]
    assert data["elapsed_ms"] is not None and data["elapsed_ms"] > 0


async def test_get_plan_unknown_task_returns_404(client) -> None:
    r = await client.get("/plan/nonexistent")
    assert r.status_code == 404
    body = r.json()
    assert body["ok"] is False
    assert body["error"]["code"] == "TASK_NOT_FOUND"
    assert "nonexistent" in body["error"]["message"]


# --------------------------------------------------------------------------- #
# 3. GET /trace：节点耗时与错误
# --------------------------------------------------------------------------- #
async def test_trace_has_per_node_latency(client, deterministic_llm) -> None:
    """trace 必须给出每个节点的耗时，且节点名与调用次数对得上。"""
    sub = (await client.post("/plan", json=BEIJING)).json()["data"]
    await _wait_done(client, sub["task_id"])

    r = await client.get(f"/plan/{sub['task_id']}/trace")
    assert r.status_code == 200
    data = r.json()["data"]

    assert data["node_count"] > 0
    assert data["node_count"] == len(data["steps"])

    # 五个专家都被调用了一次
    calls = data["node_calls"]
    for node in ("poi_agent", "route_agent", "budget_agent", "itinerary_agent", "review_agent"):
        assert calls.get(node) == 1, f"{node} 应被调用 1 次，实际 {calls.get(node)}"

    # 每一步都要有耗时（耗时是 astream 逐节点测的墙钟时间）
    latencies = [s["node_latency_ms"] for s in data["steps"]]
    assert all(v is not None for v in latencies), "每步都应有耗时"
    assert all(v >= 0 for v in latencies)
    assert sum(latencies) > 0

    # 节点名与 action 都非空，可读
    assert all(s["agent"] for s in data["steps"])
    assert all(s["action"] for s in data["steps"])

    # 没有回退（正常路径 reselect/repair 都应为 0）
    assert data["counts"] == {"reselect_count": 0, "repair_count": 0}
    # token 字段存在但当前为 null（未接入计费回调，契约保留）
    assert data["total_tokens"] is None
    assert all(s["tokens"] is None for s in data["steps"])


async def test_trace_unknown_task_returns_404(client) -> None:
    r = await client.get("/plan/xxx/trace")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "TASK_NOT_FOUND"


# --------------------------------------------------------------------------- #
# 4. 参数校验：422 + 统一错误结构
# --------------------------------------------------------------------------- #
async def test_validation_error_uses_unified_envelope(client) -> None:
    """days=0 违反 gt=0 → 422，且错误结构是统一信封 + 字段级 details。"""
    bad = dict(BEIJING)
    bad["days"] = 0
    bad["total_budget"] = -1

    r = await client.post("/plan", json=bad)

    assert r.status_code == 422
    body = r.json()
    assert body["ok"] is False
    assert body["error"]["code"] == "VALIDATION_ERROR"
    assert isinstance(body["error"]["details"], list)
    fields = {d["field"] for d in body["error"]["details"]}
    assert "days" in fields
    assert "total_budget" in fields


async def test_missing_required_field_returns_422(client) -> None:
    r = await client.post("/plan", json={"destination": "北京"})
    assert r.status_code == 422
    body = r.json()
    assert body["error"]["code"] == "VALIDATION_ERROR"
    fields = {d["field"] for d in body["error"]["details"]}
    assert {"start_date", "days", "total_budget"} <= fields


# --------------------------------------------------------------------------- #
# 5. SSE 流
# --------------------------------------------------------------------------- #
async def test_stream_emits_node_events(client, deterministic_llm) -> None:
    """SSE 必须推 task_start / node_end* / task_end 三类事件。"""
    sub = (await client.post("/plan", json=BEIJING)).json()["data"]
    task_id = sub["task_id"]

    events: list[dict] = []
    async with client.stream("GET", f"/plan/{task_id}/stream") as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        event_name = None
        async for line in resp.aiter_lines():
            if line.startswith("event:"):
                event_name = line.split(":", 1)[1].strip()
            elif line.startswith("data:"):
                payload = json.loads(line.split(":", 1)[1].strip())
                events.append({"event": event_name, "payload": payload})
                if payload.get("type") == "task_end":
                    break

    types = [e["payload"]["type"] for e in events]
    assert types[0] == "task_start"
    assert types[-1] == "task_end"
    assert types.count("node_end") >= 5, f"至少 5 个节点事件，实际 {types}"

    # node_end 事件必须带节点名 / 动作 / 耗时
    node_events = [e["payload"] for e in events if e["payload"]["type"] == "node_end"]
    for ev in node_events:
        assert ev["node"]
        assert ev["action"]
        assert isinstance(ev["latency_ms"], (int, float)) and ev["latency_ms"] >= 0

    # 事件名要与 payload type 一致（前端可以只按 event: 分发）
    assert all(e["event"] == e["payload"]["type"] for e in events)

    # 终止事件带最终状态
    assert events[-1]["payload"]["status"] == "completed"


async def test_stream_replays_history_when_connected_late(client, deterministic_llm) -> None:
    """跑完之后再连 SSE，必须回放完整历史（不能因为"错过了"就空流）。"""
    sub = (await client.post("/plan", json=BEIJING)).json()["data"]
    task_id = sub["task_id"]
    await _wait_done(client, task_id)  # 先等它跑完

    events: list[dict] = []
    async with client.stream("GET", f"/plan/{task_id}/stream") as resp:
        async for line in resp.aiter_lines():
            if line.startswith("data:"):
                payload = json.loads(line.split(":", 1)[1].strip())
                events.append(payload)
                if payload.get("type") == "task_end":
                    break

    assert events, "跑完的连上也必须回放到历史事件"
    types = [e["type"] for e in events]
    assert types[0] == "task_start"
    assert types[-1] == "task_end"
    assert types.count("node_end") >= 5


async def test_stream_unknown_task_returns_404(client) -> None:
    r = await client.get("/plan/nope/stream")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "TASK_NOT_FOUND"


# --------------------------------------------------------------------------- #
# 6. 任务隔离 + partial 语义
# --------------------------------------------------------------------------- #
async def test_two_tasks_are_isolated(client, deterministic_llm) -> None:
    """两个 task 各自独立：不同 task_id 互不干扰，结果独立。"""
    a = (await client.post("/plan", json=BEIJING)).json()["data"]["task_id"]
    b_req = dict(BEIJING)
    b_req["destination"] = "上海"
    b_req["total_budget"] = 5000
    b = (await client.post("/plan", json=b_req)).json()["data"]["task_id"]

    assert a != b
    da = await _wait_done(client, a)
    db = await _wait_done(client, b)

    assert da["status"] == "completed"
    assert db["status"] == "completed"
    # 目的地不同，POI 必然不同（不同城市的 mock 数据）
    a_ids = {p["poi_id"] for p in da["pois"]}
    b_ids = {p["poi_id"] for p in db["pois"]}
    assert a_ids != b_ids


async def test_impossible_budget_yields_partial_not_500(client, deterministic_llm) -> None:
    """预算极低 → 走 fallback → status=partial + reasons，**不是** 500。

    这是硬约束 3 在 API 层的体现：结果不理想也要交付，且明确告知原因。
    """
    tight = dict(BEIJING)
    tight["total_budget"] = 150  # 连一晚酒店都不够

    sub = (await client.post("/plan", json=tight)).json()["data"]
    data = await _wait_done(client, sub["task_id"], timeout=60.0)

    assert data["status"] == "partial", data
    assert data["partial"] is True
    assert data["reasons"], "partial 必须给出原因"
    # 原因要能读懂：明确指出是预算超支，而不是一句 "error"
    assert any("budget" in r.lower() or "超支" in r for r in data["reasons"]), data["reasons"]
    # 即便 partial，也应有可交付的行程（不是空结果）
    assert data["itinerary"] is not None
    assert data["itinerary"]["days"]
    # 预算确实超了，且接口如实反映
    assert data["budget"]["within_budget"] is False


def _break_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    """模拟"LLM 整体不可用"（比如没配 API Key）。

    这是真实场景：忘了配 OPENAI_API_KEY，或者 key 过期 / 网络断了。
    必须验证两个行为：
    1. **快速失败**：不能 supervisor→poi 无限空转到 recursion_limit(80)；
    2. **不许假成功**：没产出行程就不能报 completed。
    """

    async def _boom(*args, **kwargs):
        raise RuntimeError("Missing credentials: no api_key")

    monkeypatch.setattr(sup_mod, "call_supervisor_llm", _boom)
    monkeypatch.setattr(poi_mod, "call_poi_llm", _boom)
    monkeypatch.setattr(route_mod, "call_route_llm", _boom)
    monkeypatch.setattr(budget_mod, "call_budget_llm", _boom)


async def test_llm_unavailable_fails_fast_not_recursion(client, monkeypatch) -> None:
    """LLM 整体不可用 → 快速失败，而不是空转到 80 步才报 recursion limit。

    回归用例：修复前实测跑了 80 个节点（supervisor/poi 各 40 次），
    最后只说一句"规划步数超限"，掩盖了真正原因（缺 API Key）。
    """
    _break_llm(monkeypatch)

    sub = (await client.post("/plan", json=BEIJING)).json()["data"]
    data = await _wait_done(client, sub["task_id"], timeout=30.0)

    # 1) 必须快速失败：节点数远小于 recursion_limit
    tr = (await client.get(f"/plan/{sub['task_id']}/trace")).json()["data"]
    assert tr["node_count"] <= 6, f"应快速失败，实际跑了 {tr['node_count']} 个节点"
    assert tr["counts"]["reselect_count"] == 0

    # 2) 状态必须是 failed，且原因指向真实问题（凭证），不是含糊的"步数超限"
    assert data["status"] == "failed", data
    assert data["partial"] is False
    assert data["itinerary"] is None
    joined = " ".join(data["reasons"])
    assert "credentials" in joined.lower() or "api_key" in joined.lower(), data["reasons"]
    assert "超限" not in joined, f"不该报 recursion limit：{joined}"


async def test_no_itinerary_never_reports_completed(client, monkeypatch) -> None:
    """图正常收尾但没产出行程时，绝不能报 completed —— 那是"假成功"。

    回归用例：修复前无 API Key 的场景会返回
    `200 + status=completed + itinerary=null`，调用方会误以为规划成功。
    """
    _break_llm(monkeypatch)

    sub = (await client.post("/plan", json=BEIJING)).json()["data"]
    data = await _wait_done(client, sub["task_id"], timeout=30.0)

    assert not (data["status"] == "completed" and data["itinerary"] is None), (
        "completed 却没有行程，属于假成功"
    )
    assert data["status"] == "failed"
    assert data["reasons"], "失败必须给出原因"


async def test_healthz(client) -> None:
    r = await client.get("/healthz")
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["status"] == "ok"
    # 健康检查要摊开 LLM 配置状态：没配 key 时 configured=False，但不泄露明文
    assert "llm" in data
    assert isinstance(data["llm"]["configured"], bool)
    assert "base_url" in data["llm"] and "model" in data["llm"]
    if data["llm"]["configured"]:
        # key_hint 只能是首尾片段，绝不能是完整 key
        assert len(data["llm"]["key_hint"]) <= 16


async def test_openapi_contains_all_endpoints(client) -> None:
    """OpenAPI 文档必须包含全部 5 个接口（前端/curl 依赖它）。"""
    r = await client.get("/openapi.json")
    assert r.status_code == 200
    paths = r.json()["paths"]
    assert "/plan" in paths
    assert "/plan/{task_id}" in paths
    assert "/plan/{task_id}/trace" in paths
    assert "/plan/{task_id}/stream" in paths
    assert "post" in paths["/plan"]
