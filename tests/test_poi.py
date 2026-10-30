"""poi_agent 节点单元测试：正常路径 + 工具（MCP）失败路径。

说明：
- 采用 monkeypatch 替换 poi.call_poi_tools / poi.call_poi_llm，避免真实调用 MCP / LLM
  （无 key / 离线可跑），与 supervisor 测试范式一致。
- 测试函数本身为同步函数，内部用 asyncio.run 驱动 async 节点，因此不需要 pytest-asyncio。
"""
import asyncio
from datetime import date

from travel_planner.graph.nodes import poi
from travel_planner.graph.nodes.poi import (
    poi_agent_node,
    PoiToolError,
    PoiSelection,
)
from travel_planner.models.common import GeoPoint
from travel_planner.models.poi import POI
from travel_planner.models.requests import UserRequest
from travel_planner.state import GraphState


def _base_state(preferences=None, excluded=None) -> GraphState:
    req = UserRequest(
        destination="北京",
        start_date=date(2026, 10, 1),
        days=3,
        total_budget=8000.0,
        preferences=preferences if preferences is not None else ["美食", "历史"],
        excluded_poi_ids=excluded or [],
    )
    return {
        "request": req,
        "pois": [],
        "itinerary": None,
        "budget_report": None,
        "review": None,
        "routing": None,
        "reselect_count": 0,
        "excluded_poi_ids": excluded or [],
        "trace": [],
        "errors": [],
    }


def _fake_candidates() -> list[POI]:
    """用真实数据形状构造候选，避免与 POI 模型脱节（b1/b2 命中偏好，b3 历史类）。"""
    return [
        POI(poi_id="b1", name="故宫", category="历史", location=GeoPoint(lng=116.39, lat=39.91),
            tags=["历史", "古迹"], rating=4.8),
        POI(poi_id="b2", name="簋街", category="美食", location=GeoPoint(lng=116.41, lat=39.93),
            tags=["美食", "小吃"], rating=4.5),
        POI(poi_id="b3", name="天安门", category="历史", location=GeoPoint(lng=116.39, lat=39.90),
            tags=["历史"], rating=4.7),
    ]


async def _fake_tools_ok(state: GraphState) -> list[POI]:
    return _fake_candidates()


async def _fake_llm_ok(
    candidates: list[POI], request: UserRequest, budget_pressure: str | None = None
) -> PoiSelection:
    # 选中所有候选（由 candidates 动态生成，避免依赖具体 id）
    return PoiSelection(
        selected_poi_ids=[c.poi_id for c in candidates],
        reason="match_prefs",
    )


async def _fake_tools_fail(state: GraphState) -> list[POI]:
    # 模拟真实链路：call_poi_tools 已把 MCP 失败统一包成 PoiToolError
    raise PoiToolError("mcp down")


def test_poi_normal_path(monkeypatch):
    """正常路径：工具返回候选 + LLM 选点，节点写回 pois（阶段0 POI 模型）。"""
    monkeypatch.setattr(poi, "call_poi_tools", _fake_tools_ok)
    monkeypatch.setattr(poi, "call_poi_llm", _fake_llm_ok)

    # 场景 A：无排除，应选中全部 3 个候选
    result = asyncio.run(poi_agent_node(_base_state()))
    assert isinstance(result, dict)
    assert "pois" in result
    assert len(result["pois"]) == 3
    assert all(isinstance(p, POI) for p in result["pois"])  # 严格是阶段0 POI 模型
    # 正常路径不应产生错误
    assert result["errors"] == []
    # 可观测的结构化 trace
    assert any(s.agent == "poi_agent" for s in result["trace"])

    # 场景 B：回退边消费——排除 b1，节点应在写回前把它过滤掉
    result_excl = asyncio.run(
        poi_agent_node(_base_state(excluded=["b1"]))
    )
    got_ids = {p.poi_id for p in result_excl["pois"]}
    assert "b1" not in got_ids
    assert got_ids == {"b2", "b3"}


def test_poi_tool_failure_path(monkeypatch):
    """失败路径：MCP 工具调用失败，节点不得抛异常，应走降级并写入 errors。"""
    monkeypatch.setattr(poi, "call_poi_tools", _fake_tools_fail)
    # 即便 LLM 也 mock，工具先失败，节点应在工具阶段降级
    monkeypatch.setattr(poi, "call_poi_llm", _fake_llm_ok)

    # 关键断言 1：工具失败不能中断图（不抛异常）
    result = asyncio.run(poi_agent_node(_base_state()))

    # 关键断言 2：不更新 pois（保持原状，让 Supervisor 决策），返回 dict 不含 pois 键
    assert isinstance(result, dict)
    assert "pois" not in result

    # 关键断言 3：错误被记录进 state["errors"]
    assert len(result["errors"]) >= 1
    assert "mcp down" in result["errors"][0]
