"""route_agent 节点单元测试：正常路径 + 工具（MCP get_route）失败路径。

说明：
- 沿用 supervisor / poi 测试范式：monkeypatch 替换 route.call_route_tools / route.call_route_llm，
  离线不依赖 LLM / MCP key。
- 算法部分（plan_days）使用真实 state["pois"] 运行（纯本地计算），验证 route_order 真正由算法产出。
"""
import asyncio
from datetime import date

from travel_planner.graph.nodes import route
from travel_planner.graph.nodes.route import (
    route_agent_node,
    RouteToolError,
    RouteNarrative,
)
from travel_planner.models.common import GeoPoint, TransportMode
from travel_planner.models.poi import POI, RouteSegment
from travel_planner.models.requests import TransportPref, UserRequest
from travel_planner.state import GraphState


def _base_state(pois=None) -> GraphState:
    if pois is None:
        # 北京中心 3 点 + 东北 1 点：算法应聚成 2 天（3+1）
        pois = [
            POI(poi_id="a1", name="A1", category="c", location=GeoPoint(lat=39.90, lng=116.40)),
            POI(poi_id="a2", name="A2", category="c", location=GeoPoint(lat=39.905, lng=116.405)),
            POI(poi_id="a3", name="A3", category="c", location=GeoPoint(lat=39.91, lng=116.41)),
            POI(poi_id="b1", name="B1", category="c", location=GeoPoint(lat=39.99, lng=116.45)),
        ]
    req = UserRequest(
        destination="北京",
        start_date=date(2026, 10, 1),
        days=2,
        total_budget=8000.0,
        transport_pref=TransportPref.WALK,
    )
    return {
        "request": req,
        "pois": pois,
        "itinerary": None,
        "budget_report": None,
        "review": None,
        "routing": None,
        "reselect_count": 0,
        "excluded_poi_ids": [],
        "segments": [],
        "route_order": [],
        "route_notes": [],
        "trace": [],
        "errors": [],
    }


async def _fake_tools_ok(state: GraphState, order) -> list:
    # 用真实 RouteSegment 构造（基于算法 order 的相邻对），验证节点能正确消费
    from travel_planner.models.poi import RouteSegment

    out = []
    for day in order:
        for i in range(len(day) - 1):
            out.append(
                RouteSegment(
                    from_poi_id=day[i].poi_id,
                    to_poi_id=day[i + 1].poi_id,
                    mode=TransportMode.WALK,
                    distance_km=1.0,
                    duration_min=12,
                    cost=0.0,
                )
            )
    return out


async def _fake_llm_ok(order, segments, request) -> RouteNarrative:
    return RouteNarrative(
        day_notes=["第1天：A1→A2→A3 步行约顺畅", "第2天：B1 单独无需转场"]
    )


async def _fake_tools_fail(state: GraphState, order):
    # 模拟真实链路：call_route_tools 已把 MCP 致命失败包成 RouteToolError
    raise RouteToolError("mcp down")


def test_route_normal_path(monkeypatch):
    """正常路径：算法产出 route_order + 工具算段 + LLM 润色，节点写回三个字段。"""
    monkeypatch.setattr(route, "call_route_tools", _fake_tools_ok)
    monkeypatch.setattr(route, "call_route_llm", _fake_llm_ok)

    result = asyncio.run(route_agent_node(_base_state()))

    assert isinstance(result, dict)
    # 核心：segments 是阶段0 RouteSegment 模型实例
    assert "segments" in result and len(result["segments"]) >= 1
    assert all(isinstance(s, RouteSegment) for s in result["segments"])
    # 算法产出的每天顺序（route_order 长度 == days）
    assert "route_order" in result and len(result["route_order"]) == 2
    # LLM 润色说明已写入
    assert result["route_notes"] == ["第1天：A1→A2→A3 步行约顺畅", "第2天：B1 单独无需转场"]
    # 正常路径无错误
    assert result["errors"] == []
    assert any(s.agent == "route_agent" for s in result["trace"])


def test_route_tool_failure_path(monkeypatch):
    """失败路径：MCP get_route 致命失败，节点不得抛异常，应走降级且不更新 segments。"""
    monkeypatch.setattr(route, "call_route_tools", _fake_tools_fail)
    monkeypatch.setattr(route, "call_route_llm", _fake_llm_ok)

    # 关键断言 1：工具失败不能中断图（不抛异常）
    result = asyncio.run(route_agent_node(_base_state()))

    # 关键断言 2：致命失败不更新 segments / route_order（保持原状交 Supervisor 决策）
    assert isinstance(result, dict)
    assert "segments" not in result
    assert "route_order" not in result

    # 关键断言 3：错误被记录进 state["errors"]
    assert len(result["errors"]) >= 1
    assert "mcp down" in result["errors"][0]
