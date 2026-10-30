"""budget_agent 单元测试：正常路径 + 工具失败路径（同 supervisor / poi / route 模板）。

另外覆盖本节点的核心承诺：
- 算术确定性（同样输入恒得同样数字，回退边才可复现）；
- 超支时 suggested_cuts 真的能挑出贵的点（硬约束 3 回退边的燃料）；
- LLM 返回非法星级时兜底，不让脏数据进报告。
"""
from __future__ import annotations

import asyncio
from datetime import date

import pytest

from travel_planner.graph.nodes import budget as budget_mod
from travel_planner.graph.nodes.budget import (
    BudgetError,
    budget_agent_node,
    build_report,
    fallback_star_level,
    food_total,
    nights_for,
    pick_cuts,
    rooms_for,
    ticket_total,
    transport_total,
)
from travel_planner.models.budget import BudgetCategory
from travel_planner.models.common import GeoPoint, TransportMode
from travel_planner.models.poi import POI, RouteSegment
from travel_planner.models.requests import TransportPref, UserRequest
from travel_planner.state import GraphState


# --------------------------------------------------------------------------- #
# 测试数据
# --------------------------------------------------------------------------- #
def _poi(pid: str, ticket: float | None, *, name: str | None = None) -> POI:
    return POI(
        poi_id=pid,
        name=name or f"景点{pid}",
        category="attraction",
        location=GeoPoint(lat=39.9, lng=116.4),
        ticket_price=ticket,
    )


def _seg(a: str, b: str, cost: float) -> RouteSegment:
    return RouteSegment(
        from_poi_id=a,
        to_poi_id=b,
        mode=TransportMode.PUBLIC,
        distance_km=5.0,
        duration_min=20,
        cost=cost,
    )


def _request(budget_yuan: float = 3000.0, days: int = 3, travelers: int = 1) -> UserRequest:
    return UserRequest(
        destination="北京",
        start_date=date(2026, 11, 1),
        days=days,
        travelers=travelers,
        total_budget=budget_yuan,
        preferences=["历史"],
        transport_pref=TransportPref.PUBLIC,
    )


def _state(budget_yuan: float = 3000.0, days: int = 3, travelers: int = 1) -> GraphState:
    req = _request(budget_yuan, days, travelers)
    pois = [_poi("p1", 60.0), _poi("p2", 30.0), _poi("p3", 0.0)]
    segs = [_seg("p1", "p2", 4.0), _seg("p2", "p3", 3.0)]
    return {"request": req, "pois": pois, "segments": segs, "errors": [], "trace": []}


# --------------------------------------------------------------------------- #
# 确定性算术层（不涉及 LLM / 网络）
# --------------------------------------------------------------------------- #
def test_nights_is_days_minus_one() -> None:
    assert nights_for(_request(days=1)) == 0
    assert nights_for(_request(days=3)) == 2
    assert nights_for(_request(days=7)) == 6


def test_ticket_total_multiplies_by_travelers() -> None:
    pois = [_poi("a", 50.0), _poi("b", 30.0), _poi("c", None)]
    assert ticket_total(pois, 1) == 80.0
    assert ticket_total(pois, 3) == 240.0
    # None 视为免费，不能被当成 0 之外的东西
    assert ticket_total([_poi("d", None)], 4) == 0.0


def test_transport_and_food_are_deterministic() -> None:
    segs = [_seg("a", "b", 4.0), _seg("b", "c", 3.5)]
    assert transport_total(segs) == 7.5
    # 连续两次调用结果必须一致（回退边可复现的前提）
    assert food_total(3, 2, []) == food_total(3, 2, [])
    assert food_total(3, 2, []) == 150.0 * 2 * 3


def test_rooms_rounds_up_by_two_per_room() -> None:
    assert rooms_for(1) == 1
    assert rooms_for(2) == 1
    assert rooms_for(3) == 2
    assert rooms_for(5) == 3


def test_pick_cuts_targets_expensive_paid_pois_first() -> None:
    """硬约束 3：超支时先砍门票最贵的点，且绝不砍到只剩 0 个。"""
    pois = [_poi("cheap", 10.0), _poi("free", 0.0), _poi("pricey", 200.0)]
    cuts = pick_cuts(pois, over_by=150.0)
    assert cuts[0] == "pricey", "应优先砍门票最贵的点"
    assert "free" not in cuts, "免费点砍了不省钱"
    # 砍一个贵的（200）就够覆盖 150 的超支
    assert cuts == ["pricey"]


def test_pick_cuts_never_empties_the_plan() -> None:
    """即使超支巨大，也必须留下至少 1 个景点，否则行程失去意义。"""
    pois = [_poi("a", 500.0), _poi("b", 500.0)]
    cuts = pick_cuts(pois, over_by=10_000.0)
    assert len(cuts) == 1
    assert len(pois) - len(cuts) >= 1


def test_pick_cuts_empty_when_within_budget() -> None:
    assert pick_cuts([_poi("a", 100.0)], over_by=0.0) == []


def test_build_report_is_reproducible() -> None:
    """同样输入算两遍必须得到完全相同的报告——这是离线评测不漂移的前提。"""
    req = _request()
    pois = [_poi("p1", 60.0), _poi("p2", 30.0)]
    segs = [_seg("p1", "p2", 4.0)]
    r1 = build_report(req, pois, segs, price_per_night=500.0)
    r2 = build_report(req, pois, segs, price_per_night=500.0)
    assert r1.model_dump() == r2.model_dump()


def test_build_report_categories_sum_to_total() -> None:
    """各项金额之和必须等于总额（报告内部自洽，不能有"消失的钱"）。"""
    req = _request(budget_yuan=9999.0, days=3, travelers=2)
    pois = [_poi("p1", 60.0), _poi("p2", 0.0)]
    segs = [_seg("p1", "p2", 6.0)]
    report = build_report(req, pois, segs, price_per_night=400.0)
    item_sum = round(sum(i.amount for i in report.items), 2)
    assert item_sum == report.total_estimated
    assert {i.category for i in report.items} == {
        BudgetCategory.TICKET,
        BudgetCategory.TRANSPORT,
        BudgetCategory.FOOD,
        BudgetCategory.ACCOMMODATION,
    }


def test_build_report_flags_over_budget_and_suggests_cuts() -> None:
    req = _request(budget_yuan=100.0, days=1)
    pois = [_poi("p1", 300.0), _poi("p2", 50.0)]
    report = build_report(req, pois, [], price_per_night=0.0)
    assert report.within_budget is False
    assert report.over_by > 0
    assert report.suggested_cuts, "超支必须给出可砍的点，否则回退边没燃料"


def test_fallback_star_level_rules() -> None:
    options = [
        {"star_level": 3, "avg_price_per_night": 400.0},
        {"star_level": 4, "avg_price_per_night": 600.0},
        {"star_level": 5, "avg_price_per_night": 1000.0},
    ]
    assert fallback_star_level(options, within_budget=False) == 3, "超支取最低档"
    assert fallback_star_level(options, within_budget=True) == 4, "宽裕取中间档"
    assert fallback_star_level([], within_budget=True) == 0


# --------------------------------------------------------------------------- #
# 节点：正常路径
# --------------------------------------------------------------------------- #
def test_budget_normal_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """正常路径：工具成功 + LLM 选档合法 → 写出 BudgetReport，无错误。"""

    async def _fake_tools(request, star_level):
        return {3: 400.0, 4: 650.0, 5: 1200.0}[star_level], f"mcp:{star_level}star"

    async def _fake_llm(request, ticket, transport, options, within):
        from travel_planner.graph.nodes.budget import BudgetPlan

        return BudgetPlan(star_level=4, reason_code="balanced", note="选四星平衡")

    monkeypatch.setattr(budget_mod, "call_budget_tools", _fake_tools)
    monkeypatch.setattr(budget_mod, "call_budget_llm", _fake_llm)

    state = _state(budget_yuan=8000.0, days=3, travelers=1)
    out = asyncio.run(budget_agent_node(state))

    assert "budget_report" in out
    report = out["budget_report"]
    assert report.within_budget is True
    assert report.over_by == 0.0
    assert report.currency == "CNY"
    assert out["errors"] == []

    # 住宿条目必须反映 LLM 选的 4 星（650/晚 × 2 晚 × 1 间 = 1300）
    hotel = next(
        i for i in report.items if i.category == BudgetCategory.ACCOMMODATION
    )
    assert hotel.amount == 1300.0
    assert "4star" in hotel.detail

    # 报告内部自洽
    assert round(sum(i.amount for i in report.items), 2) == report.total_estimated
    assert out["trace"][0].status == "ok"


def test_budget_over_budget_produces_cuts(monkeypatch: pytest.MonkeyPatch) -> None:
    """超支场景：必须产出非空 suggested_cuts，供 Supervisor 触发回退边。"""

    async def _fake_tools(request, star_level):
        return {3: 400.0, 4: 650.0, 5: 1200.0}[star_level], f"mcp:{star_level}star"

    async def _fake_llm(request, ticket, transport, options, within):
        from travel_planner.graph.nodes.budget import BudgetPlan

        # 故意选最贵档，逼出超支
        return BudgetPlan(star_level=5, reason_code="comfort_first", note="想住好点")

    monkeypatch.setattr(budget_mod, "call_budget_tools", _fake_tools)
    monkeypatch.setattr(budget_mod, "call_budget_llm", _fake_llm)

    state = _state(budget_yuan=1000.0, days=3, travelers=1)
    out = asyncio.run(budget_agent_node(state))

    report = out["budget_report"]
    assert report.within_budget is False
    assert report.over_by > 0
    assert report.suggested_cuts, "超支必须给出可砍的点"
    assert out["trace"][0].status == "over_budget"


# --------------------------------------------------------------------------- #
# 节点：工具失败路径（模板要求的第二个测试）
# --------------------------------------------------------------------------- #
def test_real_call_budget_tools_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """真实 call_budget_tools：客户端抛错时必须降级返回内置价，不能把异常抛给节点。"""
    import travel_planner.tools.amap_tools as amap

    def _boom(transport=None):
        raise RuntimeError("cannot init client")

    monkeypatch.setattr(amap, "get_client", _boom)

    price, src = asyncio.run(
        budget_mod.call_budget_tools(_request(), 4)
    )
    assert src.startswith("fallback"), src
    assert price > 0, "降级价必须为正，否则住宿费被算成 0 会误判为预算宽松"


def test_budget_llm_failure_falls_back_to_rule(monkeypatch: pytest.MonkeyPatch) -> None:
    """LLM 挂掉 → 用确定性规则选档，报告仍然产出，错误写进 errors。"""

    async def _ok_tools(request, star_level):
        return {3: 400.0, 4: 650.0, 5: 1200.0}[star_level], f"mcp:{star_level}star"

    async def _fail_llm(request, ticket, transport, options, within):
        raise BudgetError("llm down")

    monkeypatch.setattr(budget_mod, "call_budget_tools", _ok_tools)
    monkeypatch.setattr(budget_mod, "call_budget_llm", _fail_llm)

    out = asyncio.run(budget_agent_node(_state(budget_yuan=8000.0)))

    assert "budget_report" in out, "LLM 失败不能导致没有预算报告"
    assert out["errors"], "LLM 失败必须留痕到 errors"
    assert any("llm down" in e for e in out["errors"]), out["errors"]
    # 预算宽裕时兜底规则取中间档 4 星
    hotel = next(
        i for i in out["budget_report"].items
        if i.category == BudgetCategory.ACCOMMODATION
    )
    assert "4star" in hotel.detail


def test_budget_llm_invalid_star_level_is_corrected(monkeypatch: pytest.MonkeyPatch) -> None:
    """LLM 返回候选外的星级（语义越界）→ 兜底到合法档位，并记录错误。"""

    async def _ok_tools(request, star_level):
        return {3: 400.0, 4: 650.0, 5: 1200.0}[star_level], f"mcp:{star_level}star"

    async def _bad_llm(request, ticket, transport, options, within):
        from travel_planner.graph.nodes.budget import BudgetPlan

        return BudgetPlan(star_level=7, reason_code="balanced", note="瞎选")

    monkeypatch.setattr(budget_mod, "call_budget_tools", _ok_tools)
    monkeypatch.setattr(budget_mod, "call_budget_llm", _bad_llm)

    out = asyncio.run(budget_agent_node(_state(budget_yuan=8000.0)))
    assert any("非法星级" in e for e in out["errors"]), out["errors"]
    hotel = next(
        i for i in out["budget_report"].items
        if i.category == BudgetCategory.ACCOMMODATION
    )
    assert "7star" not in hotel.detail


def test_budget_no_pois_degrades_without_report(monkeypatch: pytest.MonkeyPatch) -> None:
    """空输入：不产出报告，只写 trace + errors，把决定权交回 Supervisor。"""

    async def _ok_tools(request, star_level):
        return 400.0, "mcp:3star"

    async def _ok_llm(request, ticket, transport, options, within):
        from travel_planner.graph.nodes.budget import BudgetPlan

        return BudgetPlan(star_level=3)

    monkeypatch.setattr(budget_mod, "call_budget_tools", _ok_tools)
    monkeypatch.setattr(budget_mod, "call_budget_llm", _ok_llm)

    req = _request()
    state: GraphState = {"request": req, "pois": [], "errors": [], "trace": []}
    out = asyncio.run(budget_agent_node(state))

    assert "budget_report" not in out, "没有 POI 时不应凭空造一份报告"
    assert out["errors"], "降级必须留痕"
    assert out["trace"][0].status == "no_pois"


def test_budget_no_hotel_when_single_day(monkeypatch: pytest.MonkeyPatch) -> None:
    """一天行程不住宿：住宿费为 0，且不去调酒店工具。"""
    called = {"n": 0}

    async def _tools(request, star_level):
        called["n"] += 1
        return 500.0, "mcp:4star"

    async def _llm(request, ticket, transport, options, within):
        from travel_planner.graph.nodes.budget import BudgetPlan

        return BudgetPlan(star_level=0, reason_code="no_hotel")

    monkeypatch.setattr(budget_mod, "call_budget_tools", _tools)
    monkeypatch.setattr(budget_mod, "call_budget_llm", _llm)

    out = asyncio.run(budget_agent_node(_state(days=1, budget_yuan=500.0)))
    assert called["n"] == 0, "nights=0 时不应浪费工具调用"
    hotel = next(
        i for i in out["budget_report"].items
        if i.category == BudgetCategory.ACCOMMODATION
    )
    assert hotel.amount == 0.0
    assert hotel.detail == "无需住宿"
