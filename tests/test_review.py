"""review_agent 单元测试。

测试重点不是"能通过"，而是**每项校验真的能抓到它负责的那类错误**——
一个永远返回 pass 的校验器等于没有校验器。所以每项都有"构造错误 → 必须 fail"的用例。

另外锁死本节点的核心承诺：**全程不调用 LLM**。
"""
from __future__ import annotations

import asyncio
from datetime import date

import pytest

from travel_planner.graph.nodes import review as review_mod
from travel_planner.graph.nodes.review import (
    aggregate,
    check_budget,
    check_daily_commute,
    check_poi_exists,
    check_time_conflict,
    compute_score,
    parse_hhmm,
    review_agent_node,
)
from travel_planner.models.budget import BudgetCategory, BudgetItem, BudgetReport
from travel_planner.models.common import GeoPoint, TransportMode
from travel_planner.models.plan import DayPlan, Itinerary, ScheduledPOI
from travel_planner.models.poi import POI, RouteSegment
from travel_planner.models.requests import TransportPref, UserRequest
from travel_planner.models.review import CheckResult, IssueSeverity
from travel_planner.state import GraphState


# --------------------------------------------------------------------------- #
# 造数据
# --------------------------------------------------------------------------- #
def _poi(pid: str, name: str | None = None) -> POI:
    return POI(
        poi_id=pid,
        name=name or f"景点{pid}",
        category="attraction",
        location=GeoPoint(lat=39.9, lng=116.4),
        ticket_price=50.0,
    )


def _seg(a: str, b: str, minutes: int) -> RouteSegment:
    return RouteSegment(
        from_poi_id=a, to_poi_id=b, mode=TransportMode.PUBLIC,
        distance_km=3.0, duration_min=minutes, cost=2.0,
    )


def _day(idx: int, slots: list[tuple[str, str, str]]) -> DayPlan:
    return DayPlan(
        day_index=idx,
        date=date(2026, 11, 1 + idx),
        schedule=[ScheduledPOI(poi_id=p, start_time=s, end_time=e) for p, s, e in slots],
        segments=[],
        day_cost=100.0,
    )


def _itinerary(days: list[DayPlan]) -> Itinerary:
    ids = [s.poi_id for d in days for s in d.schedule]
    return Itinerary(
        request_id="req-1",
        days=days,
        ordered_poi_ids=ids,
        total_cost=sum(d.day_cost for d in days),
        total_distance_km=10.0,
        total_duration_min=300,
    )


def _budget(within: bool, over: float = 0.0, cuts: list[str] | None = None) -> BudgetReport:
    total = 1000.0 if within else 1000.0 + over
    return BudgetReport(
        total_estimated=total,
        items=[BudgetItem(category=BudgetCategory.TICKET, amount=total)],
        within_budget=within,
        over_by=over,
        suggested_cuts=cuts or [],
    )


def _request() -> UserRequest:
    return UserRequest(
        destination="北京", start_date=date(2026, 11, 1), days=1,
        travelers=1, total_budget=5000.0, transport_pref=TransportPref.PUBLIC,
    )


def _healthy_state() -> GraphState:
    """一个四项全过的健康状态。各用例在此基础上注入单个错误。"""
    pois = [_poi("p1"), _poi("p2")]
    days = [
        _day(0, [("p1", "09:00", "10:30"), ("p2", "11:00", "12:00")]),
    ]
    return {
        "request": _request(),
        "pois": pois,
        "poi_candidates": [_poi("p1"), _poi("p2"), _poi("p3")],
        "segments": [_seg("p1", "p2", 20)],
        "itinerary": _itinerary(days),
        "budget_report": _budget(True),
        "errors": [],
        "trace": [],
    }


# --------------------------------------------------------------------------- #
# 时间解析
# --------------------------------------------------------------------------- #
def test_parse_hhmm() -> None:
    assert parse_hhmm("09:00") == 540
    assert parse_hhmm("9:05") == 545
    assert parse_hhmm("23:59") == 1439
    # 非法输入返回 None，不抛异常（异常会让整个复盘崩）
    assert parse_hhmm("25:00") is None
    assert parse_hhmm("9点") is None
    assert parse_hhmm("") is None
    assert parse_hhmm(None) is None  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# 校验 1：景点是否存在（事实校验 —— 本节点最重要的能力）
# --------------------------------------------------------------------------- #
def test_poi_exists_passes_when_all_from_candidates() -> None:
    pois = [_poi("p1"), _poi("p2")]
    c, issues = check_poi_exists(pois, [_poi("p1"), _poi("p2"), _poi("p3")])
    assert c.passed is True
    assert issues == []


def test_poi_exists_catches_hallucinated_poi() -> None:
    """LLM 编造一个不在检索结果里的景点 → 必须报 ERROR。"""
    pois = [_poi("p1"), _poi("FAKE_NEW_ATTRACTION")]
    c, issues = check_poi_exists(pois, [_poi("p1"), _poi("p2")])
    assert c.passed is False
    assert "fail" in c.detail
    errs = [i for i in issues if i.severity == IssueSeverity.ERROR]
    assert any(i.code == "poi_not_in_candidates" for i in errs)
    assert errs[0].related_poi_ids == ["FAKE_NEW_ATTRACTION"]


def test_poi_exists_catches_renamed_poi() -> None:
    """id 对但名称被改写 → 也算事实不一致，必须 fail（名称是最容易被人肉发现的部分）。"""
    pois = [_poi("p1", name="故宫博物院（皇家禁地）")]
    c, issues = check_poi_exists(pois, [_poi("p1", name="故宫博物院")])
    assert c.passed is False
    assert any(i.code == "poi_name_mismatch" for i in issues)


def test_poi_exists_without_baseline_warns_not_passes() -> None:
    """没有候选集时无法核实 —— 必须明确 warn，而不能假装通过（假阳性是通过率作弊）。"""
    c, issues = check_poi_exists([_poi("p1")], None)
    assert c.detail.startswith("warn:")
    assert any(i.code == "no_candidate_baseline" for i in issues)


def test_poi_exists_empty_plan_fails() -> None:
    c, issues = check_poi_exists([], [_poi("p1")])
    assert c.passed is False
    assert any(i.code == "no_poi" for i in issues)


# --------------------------------------------------------------------------- #
# 校验 2：时间冲突
# --------------------------------------------------------------------------- #
def test_time_conflict_passes_for_clean_schedule() -> None:
    it = _itinerary([_day(0, [("p1", "09:00", "10:30"), ("p2", "11:00", "12:00")])])
    c, issues = check_time_conflict(it)
    assert c.passed is True
    assert [i for i in issues if i.severity == IssueSeverity.ERROR] == []


def test_time_conflict_catches_overlap() -> None:
    """两个时段重叠（10:00-12:00 与 11:00-13:00）→ 必须 fail。"""
    it = _itinerary([_day(0, [("p1", "10:00", "12:00"), ("p2", "11:00", "13:00")])])
    c, issues = check_time_conflict(it)
    assert c.passed is False
    assert any(i.code == "time_overlap" for i in issues)


def test_time_conflict_catches_reversed_time() -> None:
    """结束时间早于开始时间（手滑或 LLM 编造）→ 必须 fail。"""
    it = _itinerary([_day(0, [("p1", "15:00", "14:00")])])
    c, issues = check_time_conflict(it)
    assert c.passed is False
    assert any(i.code == "time_order_invalid" for i in issues)


def test_time_conflict_catches_duplicate_poi_across_days() -> None:
    """同一个景点被安排在两天 → fail（重复付费且违反常识）。"""
    it = _itinerary([
        _day(0, [("p1", "09:00", "10:00")]),
        _day(1, [("p1", "09:00", "10:00")]),
    ])
    c, issues = check_time_conflict(it)
    assert c.passed is False
    assert any(i.code == "poi_duplicated" for i in issues)


def test_time_conflict_adjacent_slots_are_not_overlap() -> None:
    """边界：上一段结束时间 == 下一段开始时间，不算重叠（不然误报率太高）。"""
    it = _itinerary([_day(0, [("p1", "09:00", "10:00"), ("p2", "10:00", "11:00")])])
    c, _ = check_time_conflict(it)
    assert c.passed is True


def test_time_conflict_without_itinerary_warns() -> None:
    c, issues = check_time_conflict(None)
    assert c.detail.startswith("warn:")
    assert any(i.code == "no_itinerary" for i in issues)


# --------------------------------------------------------------------------- #
# 校验 3：每日通勤
# --------------------------------------------------------------------------- #
def test_daily_commute_passes_under_limit() -> None:
    days = [_day(0, [("p1", "09:00", "10:00"), ("p2", "11:00", "12:00")])]
    segs = [_seg("p1", "p2", 30)]
    c, issues = check_daily_commute(segs, days)
    assert c.passed is True
    assert [i for i in issues if i.severity == IssueSeverity.ERROR] == []


def test_daily_commute_catches_over_limit() -> None:
    """单日通勤 165 分钟 > 阈值 120 → fail，且 detail 要能指出是哪一天。"""
    days = [_day(0, [("p1", "09:00", "10:00"), ("p2", "11:00", "12:00")])]
    segs = [_seg("p1", "p2", 165)]
    c, issues = check_daily_commute(segs, days)
    assert c.passed is False
    assert "第1天" in c.detail
    assert any(i.code == "daily_commute_exceeded" for i in issues)


def test_daily_commute_aggregates_by_day() -> None:
    """多天时按天分别判定：某一天超了就是 fail，不能因为另一天很轻松就平均掉。"""
    days = [
        _day(0, [("p1", "09:00", "10:00"), ("p2", "10:30", "11:00")]),
        _day(1, [("p3", "09:00", "10:00"), ("p4", "10:30", "11:00")]),
    ]
    segs = [_seg("p1", "p2", 30), _seg("p3", "p4", 200)]
    c, _ = check_daily_commute(segs, days)
    assert c.passed is False
    assert "第2天" in c.detail


def test_daily_commute_without_segments_warns() -> None:
    c, issues = check_daily_commute([], [])
    assert c.detail.startswith("warn:")
    assert any(i.code == "no_segments" for i in issues)


# --------------------------------------------------------------------------- #
# 校验 4：预算
# --------------------------------------------------------------------------- #
def test_budget_passes_when_within() -> None:
    c, issues = check_budget(_budget(True))
    assert c.passed is True
    assert issues == []


def test_budget_catches_overrun_and_links_cuts() -> None:
    c, issues = check_budget(_budget(False, over=320.0, cuts=["p2"]))
    assert c.passed is False
    assert "320" in c.detail
    err = next(i for i in issues if i.severity == IssueSeverity.ERROR)
    assert err.code == "budget_exceeded"
    # 把 suggested_cuts 透传到 Issue，Supervisor 可据此直接路由回 poi_agent
    assert err.related_poi_ids == ["p2"]


def test_budget_missing_warns() -> None:
    c, issues = check_budget(None)
    assert c.detail.startswith("warn:")
    assert any(i.code == "no_budget_report" for i in issues)


# --------------------------------------------------------------------------- #
# 汇总逻辑
# --------------------------------------------------------------------------- #
def test_score_is_100_when_all_pass() -> None:
    checks = [CheckResult(name=n, passed=True, detail="pass: ok") for n in review_mod._WEIGHTS]
    assert compute_score(checks) == 100.0


def test_score_drops_on_failure() -> None:
    checks = [
        CheckResult(name="poi_exists", passed=True, detail="pass: ok"),
        CheckResult(name="time_conflict", passed=True, detail="pass: ok"),
        CheckResult(name="daily_commute", passed=False, detail="fail: 超了"),
        CheckResult(name="budget", passed=True, detail="pass: ok"),
    ]
    assert compute_score(checks) == 80.0


def test_warn_counts_half_score() -> None:
    """未执行（warn）按半分算 —— 不能因为"没法校验"就白拿满分。"""
    checks = [CheckResult(name="poi_exists", passed=True, detail="warn: 无候选")]
    assert compute_score(checks) == 50.0


def test_aggregate_any_failure_means_not_passed() -> None:
    """事实校验不做分数折中：只要有一项 fail，整体就是不通过。"""
    checks = [
        CheckResult(name="poi_exists", passed=True, detail="pass: ok"),
        CheckResult(name="budget", passed=False, detail="fail: 超支"),
    ]
    result = aggregate(checks, [])
    assert result.passed is False
    assert result.score > 0


# --------------------------------------------------------------------------- #
# 节点级：正常路径 / 各单项失败 / 异常兜底
# --------------------------------------------------------------------------- #
def test_review_node_healthy_path() -> None:
    out = asyncio.run(review_agent_node(_healthy_state()))
    result = out["review"]
    assert result.passed is True
    assert result.score == 100.0
    assert len(result.checks) == 4
    assert {c.name for c in result.checks} == {
        "poi_exists", "time_conflict", "daily_commute", "budget"
    }
    assert all(c.detail for c in result.checks), "每项都必须给出原因，不能只有 bool"
    assert out["errors"] == []
    assert out["trace"][0].status == "pass"


def test_review_node_catches_hallucinated_poi_end_to_end() -> None:
    """端到端：塞一个检索结果里没有的景点 → 整体不通过，且能追到具体 poi_id。"""
    state = _healthy_state()
    state["pois"] = [_poi("p1"), _poi("p2"), _poi("HALLUCINATED")]
    out = asyncio.run(review_agent_node(state))
    result = out["review"]
    assert result.passed is False
    assert result.score < 100.0
    assert any("HALLUCINATED" in i.related_poi_ids for i in result.issues)
    assert out["trace"][0].status == "fail"


def test_review_node_catches_budget_overrun() -> None:
    state = _healthy_state()
    state["budget_report"] = _budget(False, over=800.0, cuts=["p2"])
    out = asyncio.run(review_agent_node(state))
    assert out["review"].passed is False
    budget_check = next(c for c in out["review"].checks if c.name == "budget")
    assert budget_check.passed is False


def test_review_node_catches_time_conflict() -> None:
    state = _healthy_state()
    state["itinerary"] = _itinerary(
        [_day(0, [("p1", "10:00", "12:00"), ("p2", "11:00", "13:00")])]
    )
    out = asyncio.run(review_agent_node(state))
    assert out["review"].passed is False
    time_check = next(c for c in out["review"].checks if c.name == "time_conflict")
    assert time_check.passed is False


def test_review_node_is_pure_function_of_state() -> None:
    """同样输入跑两次结论必须完全一致（可复现是事实校验的底线）。"""
    state = _healthy_state()
    r1 = asyncio.run(review_agent_node(state))["review"]
    r2 = asyncio.run(review_agent_node(state))["review"]
    assert r1.model_dump() == r2.model_dump()


def test_review_node_never_calls_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    """铁证：把 LLM 工厂换成"一调用就炸"，review 节点仍然正常工作。"""
    import travel_planner.llm.client as llm_client

    def _boom(*a, **kw):
        raise AssertionError("review_agent 不应调用 LLM")

    monkeypatch.setattr(llm_client, "get_chat_model", _boom, raising=False)
    out = asyncio.run(review_agent_node(_healthy_state()))
    assert out["review"].passed is True
    assert out["errors"] == []


def test_review_node_survives_broken_check(monkeypatch: pytest.MonkeyPatch) -> None:
    """单项校验崩了 → 记 warn + 不中断，其它检查照常出结论。"""

    def _crash(*a, **kw):
        raise RuntimeError("check blew up")

    monkeypatch.setattr(review_mod, "check_time_conflict", _crash)
    out = asyncio.run(review_agent_node(_healthy_state()))
    result = out["review"]
    assert len(result.checks) == 4, "崩溃项也要占位，保证四项结构完整"
    crashed = next(c for c in result.checks if c.name == "time_conflict")
    assert crashed.detail.startswith("warn:")
    assert any(i.code == "check_crashed" for i in result.issues)


def test_review_node_empty_state_is_safe() -> None:
    """极端空 state：不抛异常，给出结构化结论（空行程必然不通过）。"""
    out = asyncio.run(review_agent_node({"errors": [], "trace": []}))
    result = out["review"]
    assert result.passed is False
    assert any(c.name == "poi_exists" and not c.passed for c in result.checks)
