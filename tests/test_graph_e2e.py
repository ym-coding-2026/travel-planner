"""图级 end-to-end 测试：跑通全流程，并验证三条控制流分支。

设计说明（重要）：
- LLM 用**可控替身**：不是让流程空转，而是按"当前应该处在哪一步"返回合法结构化输出。
  这样跑的是真实的图、真实的节点、真实的算术与校验，只有"决策"被确定化。
- 校验的核心不是"能跑完"，而是四条硬约束真的在生效：
  ① Itinerary 非空；② 预算不超支（或用尽重试后 partial 且如实说明）；
  ③ 预算超支确实回 POI 且最多 2 次；④ review fail 确实按失败项回对应专家；
  ⑤ 计数超限确实走 fallback 而不是死循环。
"""
from __future__ import annotations

import asyncio
import threading
from datetime import date
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict

import pytest

from travel_planner.graph import edges
from travel_planner.graph.builder import build_graph, make_async_checkpointer
from travel_planner.graph.nodes import budget as budget_mod
from travel_planner.graph.nodes import poi as poi_mod
from travel_planner.graph.nodes import route as route_mod
from travel_planner.graph.nodes import supervisor as sup_mod
from travel_planner.models.requests import TransportPref, UserRequest


# --------------------------------------------------------------------------- #
# 运行器：节点是 async → 必须用 AsyncSqliteSaver + ainvoke
# （SqliteSaver 不支持 ainvoke，会抛 NotImplementedError）
# --------------------------------------------------------------------------- #
def _run_in_new_loop(coro_factory: Callable[[], Awaitable[Any]]) -> Any:
    """在**独立线程**里跑一个新事件循环。

    直接用 asyncio.run() 在 pytest 里连续调用会遇到"事件循环已关闭/已在运行"的
    不稳定问题（尤其配合 aiosqlite 的线程亲和性）。放进独立线程最省心，
    每个测试拿到干净循环，互不污染。
    """
    box: Dict[str, Any] = {}

    def _worker() -> None:
        try:
            box["value"] = asyncio.run(coro_factory())
        except BaseException as e:  # noqa: BLE001 — 跨线程传回原始异常
            box["error"] = e

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    t.join()
    if "error" in box:
        raise box["error"]
    return box["value"]


def run_with_saver(
    scenario: Callable[[Any], Awaitable[Dict[str, Any]]],
    db_path: str = ":memory:",
) -> Dict[str, Any]:
    """在一个事件循环里：建异步 saver → 建图 → 跑场景 → 关连接。"""

    async def _main() -> Dict[str, Any]:
        saver = await make_async_checkpointer(db_path)
        try:
            graph = build_graph(checkpointer=saver)
            return await scenario(graph)
        finally:
            await saver.conn.close()

    return _run_in_new_loop(_main)


async def _arun(graph, state: dict, thread_id: str, recursion_limit: int = 80) -> dict:
    """异步跑图。recursion_limit 是防死循环的保险丝。"""
    cfg = {
        "configurable": {"thread_id": thread_id},
        "recursion_limit": recursion_limit,
    }
    return await graph.ainvoke(state, cfg)


def _request(budget: float = 3000.0, days: int = 3) -> UserRequest:
    return UserRequest(
        destination="北京",
        start_date=date(2026, 11, 1),
        days=days,
        travelers=1,
        total_budget=budget,
        preferences=["历史", "美食"],
        transport_pref=TransportPref.PUBLIC,
    )


def _initial_state(budget: float = 3000.0, days: int = 3) -> dict:
    return {
        "request": _request(budget, days),
        "pois": [],
        "segments": [],
        "reselect_count": 0,
        "repair_count": 0,
        "excluded_poi_ids": [],
        "errors": [],
        "trace": [],
    }


def _run(graph, state: dict, thread_id: str, recursion_limit: int = 60) -> dict:
    """同步跑图，返回最终 state。recursion_limit 用来防"真的死循环"把测试挂死。"""
    config = {
        "configurable": {"thread_id": thread_id},
        "recursion_limit": recursion_limit,
    }
    return asyncio.run(graph.ainvoke(state, config))


# --------------------------------------------------------------------------- #
# 可控 LLM 替身：按 state 的进度返回合法结构化输出
# --------------------------------------------------------------------------- #
def _install_deterministic_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    """把所有 LLM 调用换成按 State 进度决策的替身。

    真实工具（MCP mock）与真实算术全部保留，只有"模型判断"被确定化。
    """

    async def _sup(state) -> "sup_mod.RoutingDecision":
        """按 prompts/supervisor.md 的规则顺序推进。

        关键：判"路线算过"用 route_order，不用 segments —— 只有 1 个景点时
        路线段合法为 0，用 segments 判断会导致无限重复调用 route_agent。
        """
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

        per_day = max(1, request.max_poi_per_day)
        want = per_day * request.days
        # 遵守 budget_pressure：降价时优先拿便宜的点
        pool = list(candidates)
        if budget_pressure:
            pool.sort(key=lambda p: (p.ticket_price or 0.0, p.poi_id))
        else:
            pool.sort(key=lambda p: p.poi_id)
        picked = [p.poi_id for p in pool[:want]]
        return PoiSelection(
            selected_poi_ids=picked,
            reason="budget_cut_for_cost" if budget_pressure else "match_prefs",
        )

    async def _route_llm(order, segments, request):
        from travel_planner.graph.nodes.route import RouteNarrative

        return RouteNarrative(day_notes=[f"第{i + 1}天行程紧凑" for i in range(len(order))])

    async def _budget_llm(request, ticket, transport, options, within):
        from travel_planner.graph.nodes.budget import BudgetPlan

        if not options:
            return BudgetPlan(star_level=0, reason_code="no_hotel")
        # 明确策略：预算够选中间档；不够选最低档（与 budget.md 规则一致）
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
    # 真实 MCP mock 保持不动：这是端到端的一部分

# --------------------------------------------------------------------------- #
# 图结构自检
# --------------------------------------------------------------------------- #
def test_e2e_mermaid_contains_all_branches() -> None:
    """图结构自检：三条回退分支与 fallback 出口都在图里。"""

    async def _scenario(graph) -> dict:
        return {"code": graph.get_graph().draw_mermaid()}

    out = run_with_saver(_scenario)
    code = out["code"]

    assert "reselect_guard" in code
    assert "repair_guard" in code
    assert "fallback" in code
    # 预算回退边
    assert "budget_agent -.-> reselect_guard" in code
    # guard 回 POI
    assert "reselect_guard --> poi_agent" in code
    # review 三条出边
    assert "review_agent -.-> repair_guard" in code
    assert "review_agent -.-> fallback" in code
    # fallback 出口
    assert "fallback --> __end__" in code
    # 入口是 supervisor
    assert "__start__ --> supervisor" in code


# --------------------------------------------------------------------------- #
# E2E 主用例：北京 3 天 / 预算 3000
# --------------------------------------------------------------------------- #
def test_e2e_beijing_3days_budget_3000(monkeypatch: pytest.MonkeyPatch) -> None:
    """跑通全流程，断言 Itinerary 非空、预算不超支、review 通过。"""
    _install_deterministic_llm(monkeypatch)

    async def _scenario(graph) -> dict:
        return await _arun(graph, _initial_state(3000.0, 3), thread_id="e2e-beijing")

    final = run_with_saver(_scenario)

    # ① Itinerary 必须非空
    itinerary = final.get("itinerary")
    assert itinerary is not None, "全流程跑完必须产出 Itinerary"
    assert itinerary.days and len(itinerary.days) == 3, "应为 3 天"
    assert itinerary.ordered_poi_ids, "行程里必须有景点"
    assert all(d.schedule for d in itinerary.days), "每天都要有安排"

    # ② 预算不超支
    report = final.get("budget_report")
    assert report is not None
    assert report.within_budget is True, f"预算超支了：{report.over_by}"
    assert report.total_estimated <= 3000.0
    assert report.over_by == 0.0

    # ③ review 通过，四项检查都在
    review = final.get("review")
    assert review is not None
    assert review.passed is True, [(c.name, c.detail) for c in review.checks]
    assert {c.name for c in review.checks} == {
        "poi_exists", "time_conflict", "daily_commute", "budget"
    }

    # ④ 没有走 fallback
    assert final.get("partial") is not True

    # ⑤ trace 记录了完整链路
    agents = [s.agent for s in final.get("trace", [])]
    for expected in ["supervisor", "poi_agent", "route_agent", "budget_agent",
                     "itinerary_agent", "review_agent"]:
        assert expected in agents, f"trace 缺少 {expected}: {agents}"

    # ⑥ 预算明细自洽（各项之和 == 总额）
    assert round(sum(i.amount for i in report.items), 2) == report.total_estimated


def test_e2e_itinerary_has_no_time_conflict(monkeypatch: pytest.MonkeyPatch) -> None:
    """真实排班结果本身不能自相矛盾：同日内不得重叠、结束时间必须晚于开始。

    这个断言必须跑在**真实的** itinerary_agent 输出上（而非构造数据），
    否则排班算法的重叠 bug 会被测试数据掩盖。
    """
    _install_deterministic_llm(monkeypatch)

    async def _scenario(graph) -> dict:
        return await _arun(graph, _initial_state(3000.0, 3), thread_id="e2e-time")

    final = run_with_saver(_scenario)

    from travel_planner.graph.nodes.review import parse_hhmm

    days = final["itinerary"].days
    assert days, "必须产出行程"
    checked = 0
    for day in days:
        spans = []
        for slot in day.schedule:
            s, e = parse_hhmm(slot.start_time), parse_hhmm(slot.end_time)
            assert s is not None and e is not None, f"时刻格式非法：{slot}"
            assert e > s, f"第{day.day_index + 1}天 {slot.poi_id} 结束早于开始"
            spans.append((s, e))
            checked += 1
        spans.sort()
        for a, b in zip(spans, spans[1:]):
            assert b[0] >= a[1], f"第{day.day_index + 1}天时段重叠"
    assert checked > 0, "一个时段都没排出来，断言形同虚设"


# --------------------------------------------------------------------------- #
# 分支 A：预算超支 → 回 POI 重选（最多 2 次）
# --------------------------------------------------------------------------- #
def test_budget_overrun_loops_back_to_poi_at_most_twice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """预算极紧时一定超支：必须回 POI 重选，且 reselect_count 不超过 2。"""
    _install_deterministic_llm(monkeypatch)

    async def _scenario(graph) -> dict:
        return await _arun(graph, _initial_state(300.0, 3), thread_id="e2e-tight")

    final = run_with_saver(_scenario)

    assert final.get("reselect_count", 0) <= edges.MAX_RESELECT, (
        f"重选次数超过上限 {edges.MAX_RESELECT}：{final.get('reselect_count')}"
    )
    # 关键：超支不应该崩，最终仍交付一份行程
    assert final.get("itinerary") is not None, "超支也必须交付行程，不能直接失败"
    # 预算压力标记确实被写入过（说明真的走了回退边）
    assert (
        final.get("budget_pressure") == edges.BUDGET_PRESSURE
        or final.get("reselect_count", 0) > 0
    )


def test_overrun_marks_partial_with_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    """超支且重试用尽 → fallback 标记 partial 并给出原因（不是静默失败）。"""
    _install_deterministic_llm(monkeypatch)

    async def _scenario(graph) -> dict:
        return await _arun(graph, _initial_state(300.0, 3), thread_id="e2e-partial")

    final = run_with_saver(_scenario)

    report = final.get("budget_report")
    assert report is not None and report.within_budget is False
    assert final.get("partial") is True, "用尽重试仍超支，必须显式标记部分满足"
    assert final.get("status") == "partial"
    joined = " ".join(final.get("errors") or [])
    assert "budget_overrun" in joined or "超支" in joined, joined


# --------------------------------------------------------------------------- #
# 分支 B：review fail → 按失败项回对应专家
# --------------------------------------------------------------------------- #
def test_review_fact_error_routes_back_to_poi(monkeypatch: pytest.MonkeyPatch) -> None:
    """事实错误（poi_exists fail）→ 必须回 poi_agent 修复。"""
    _install_deterministic_llm(monkeypatch)

    from travel_planner.graph.nodes import review as review_mod

    real_review = review_mod.check_poi_exists
    calls = {"n": 0}

    def _flaky_poi_exists(pois, candidates):
        """第一次故意判 fail（模拟上游塞了个假景点），之后恢复正常。"""
        calls["n"] += 1
        if calls["n"] == 1:
            from travel_planner.models.review import CheckResult, Issue, IssueSeverity

            return (
                CheckResult(name="poi_exists", passed=False, detail="fail: 测试注入事实错误"),
                [
                    Issue(
                        severity=IssueSeverity.ERROR,
                        code="poi_not_in_candidates",
                        message="测试注入",
                        related_poi_ids=[pois[0].poi_id] if pois else [],
                    )
                ],
            )
        return real_review(pois, candidates)

    monkeypatch.setattr(review_mod, "check_poi_exists", _flaky_poi_exists)

    async def _scenario(graph) -> dict:
        return await _arun(graph, _initial_state(5000.0, 3), thread_id="e2e-fact-error")

    final = run_with_saver(_scenario)

    assert final.get("repair_count", 0) >= 1, "review fail 后应触发修复路由"
    poi_calls = [s for s in final.get("trace", []) if s.agent == "poi_agent"]
    assert len(poi_calls) >= 2, f"poi_agent 应被重复调用，实际 {len(poi_calls)} 次"


def test_review_time_conflict_routes_back_to_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """时间冲突（time_conflict fail）→ 必须回 route_agent 修复。"""
    _install_deterministic_llm(monkeypatch)

    from travel_planner.graph.nodes import review as review_mod

    real = review_mod.check_time_conflict
    calls = {"n": 0}

    def _flaky(itinerary):
        calls["n"] += 1
        if calls["n"] == 1:
            from travel_planner.models.review import CheckResult, Issue, IssueSeverity

            return (
                CheckResult(name="time_conflict", passed=False, detail="fail: 测试注入时段重叠"),
                [
                    Issue(
                        severity=IssueSeverity.ERROR,
                        code="time_overlap",
                        message="测试注入",
                    )
                ],
            )
        return real(itinerary)

    monkeypatch.setattr(review_mod, "check_time_conflict", _flaky)

    async def _scenario(graph) -> dict:
        return await _arun(graph, _initial_state(5000.0, 3), thread_id="e2e-time-conflict")

    final = run_with_saver(_scenario)

    assert final.get("repair_count", 0) >= 1
    route_calls = [s for s in final.get("trace", []) if s.agent == "route_agent"]
    assert len(route_calls) >= 2, f"route_agent 应被重复调用，实际 {len(route_calls)} 次"


def test_repair_exhausted_goes_to_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """review 永远 fail → 修复 2 次后必须走 fallback，绝不能死循环。"""
    _install_deterministic_llm(monkeypatch)

    from travel_planner.graph.nodes import review as review_mod

    def _always_fail(pois, candidates):
        from travel_planner.models.review import CheckResult, Issue, IssueSeverity

        return (
            CheckResult(name="poi_exists", passed=False, detail="fail: 永久失败"),
            [Issue(severity=IssueSeverity.ERROR, code="poi_not_in_candidates", message="永久")],
        )

    monkeypatch.setattr(review_mod, "check_poi_exists", _always_fail)

    async def _scenario(graph) -> dict:
        # recursion_limit 卡在 60：如果真死循环，这里会抛 GraphRecursionError
        return await _arun(
            graph, _initial_state(5000.0, 3), thread_id="e2e-exhausted", recursion_limit=60
        )

    final = run_with_saver(_scenario)

    assert final.get("repair_count", 0) <= edges.MAX_REPAIR
    assert final.get("partial") is True
    joined = " ".join(final.get("errors") or [])
    assert "repair_exhausted" in joined or "review_failed" in joined, joined


# --------------------------------------------------------------------------- #
# Checkpointer：thread_id 隔离 + 断点恢复 + 真落盘
# --------------------------------------------------------------------------- #
def test_thread_isolation(monkeypatch: pytest.MonkeyPatch) -> None:
    """不同 thread_id 互不干扰：各自的 state 独立。"""
    _install_deterministic_llm(monkeypatch)

    async def _scenario(graph) -> dict:
        f1 = await _arun(graph, _initial_state(3000.0, 3), thread_id="thread-A")
        f2 = await _arun(graph, _initial_state(3000.0, 2), thread_id="thread-B")
        return {"f1": f1, "f2": f2}

    out = run_with_saver(_scenario)
    assert len(out["f1"]["itinerary"].days) == 3
    assert len(out["f2"]["itinerary"].days) == 2


def test_checkpoint_resumes_with_same_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    """断点恢复：同 thread_id 能从检查点读回已完成状态。"""
    _install_deterministic_llm(monkeypatch)

    async def _scenario(graph) -> dict:
        cfg = {"configurable": {"thread_id": "resume-1"}, "recursion_limit": 80}
        first = await graph.ainvoke(_initial_state(3000.0, 3), cfg)
        snapshot = await graph.aget_state(cfg)
        return {"first": first, "snapshot": snapshot}

    out = run_with_saver(_scenario)
    assert out["first"].get("itinerary") is not None
    assert out["snapshot"] is not None
    assert out["snapshot"].values.get("itinerary") is not None
    assert len(out["snapshot"].values["itinerary"].days) == 3


def test_sqlite_persists_across_graph_instances(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """真正落盘的证据：换一个新 saver + 新图实例，仍能读到同一 thread 的状态。"""
    _install_deterministic_llm(monkeypatch)
    db = str(tmp_path / "ckpt.sqlite")

    async def _write_then_read() -> dict:
        saver1 = await make_async_checkpointer(db)
        try:
            g1 = build_graph(checkpointer=saver1)
            await _arun(g1, _initial_state(3000.0, 3), thread_id="persist-1")
        finally:
            await saver1.conn.close()

        saver2 = await make_async_checkpointer(db)
        try:
            g2 = build_graph(checkpointer=saver2)
            snap = await g2.aget_state({"configurable": {"thread_id": "persist-1"}})
            return {"itinerary": snap.values.get("itinerary")}
        finally:
            await saver2.conn.close()

    out = _run_in_new_loop(_write_then_read)
    assert out["itinerary"] is not None, "新实例读不到旧 thread 状态（没有真正落盘）"
