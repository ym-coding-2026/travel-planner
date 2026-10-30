"""supervisor 节点单元测试：正常路径 + 工具（LLM）失败路径。

说明：
- 采用 monkeypatch 替换 supervisor.call_supervisor_llm，避免真实调用 LLM（无 key / 离线可跑）。
- 测试函数本身为同步函数，内部用 asyncio.run 驱动 async 节点，因此不需要 pytest-asyncio。
"""
import asyncio
from datetime import date

from travel_planner.graph.nodes import supervisor
from travel_planner.graph.nodes.supervisor import supervisor_node, SupervisorError
from travel_planner.models.requests import UserRequest
from travel_planner.models.routing import NextStep, RoutingDecision
from travel_planner.state import GraphState


def _base_state() -> GraphState:
    req = UserRequest(
        destination="北京",
        start_date=date(2026, 10, 1),
        days=3,
        total_budget=8000.0,
    )
    return {
        "request": req,
        "pois": [],
        "itinerary": None,
        "budget_report": None,
        "review": None,
        "routing": None,
        "reselect_count": 0,
        "excluded_poi_ids": [],
        "trace": [],
        "errors": [],
    }


async def _fake_ok(state: GraphState) -> RoutingDecision:
    return RoutingDecision(next=NextStep.POI, reason_code="initial")


async def _fake_fail(state: GraphState) -> RoutingDecision:
    # 模拟真实链路：call_supervisor_llm 已把工具/解析失败统一包成 SupervisorError
    raise SupervisorError("llm down")


def test_supervisor_normal_path(monkeypatch):
    """正常路径：LLM 返回合法 RoutingDecision，节点原样写回 routing，且不写错误。"""
    monkeypatch.setattr(supervisor, "call_supervisor_llm", _fake_ok)
    result = asyncio.run(supervisor_node(_base_state()))

    assert isinstance(result, dict)
    assert isinstance(result["routing"], RoutingDecision)
    assert result["routing"].next == NextStep.POI
    assert result["routing"].reason_code == "initial"
    # 正常路径不应产生错误
    assert result["errors"] == []
    # 应写入可观测的结构化 trace
    assert any(s.agent == "supervisor" for s in result["trace"])


def test_supervisor_tool_failure_path(monkeypatch):
    """失败路径：LLM（工具）调用失败，节点不得抛异常，应走降级并写入 errors。"""
    monkeypatch.setattr(supervisor, "call_supervisor_llm", _fake_fail)

    # 关键断言 1：失败不能中断图（不抛异常）
    result = asyncio.run(supervisor_node(_base_state()))

    # 关键断言 2：仍返回合法路由（降级决策），且类型正确
    assert isinstance(result, dict)
    assert isinstance(result["routing"], RoutingDecision)
    # 降级默认在尚无 POI 时尝试推进到 poi_agent
    assert result["routing"].reason_code.startswith("supervisor_fallback")
    # 关键断言 3：错误被记录进 state["errors"]
    assert len(result["errors"]) >= 1
