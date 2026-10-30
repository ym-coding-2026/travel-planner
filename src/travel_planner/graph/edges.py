"""条件边：把 GraphState 的当前内容映射成"下一个节点名"。

设计原则（重要，决定了图的形状）：
- 条件边**只做机械映射**，不做业务判断。真正的"思考"是 supervisor 产出的
  RoutingDecision；edges 只负责把 `routing.next` 翻译成节点名。
- **条件边必须是纯函数**：LangGraph 不保证边函数能安全地写 state（它只拿返回值当路由键），
  所以所有"计数 / 打标记"的副作用都放进 `nodes/guards.py` 的轻量 guard 节点里。
  这样"决策"和"记账"分离，回退计数不会因为边被重复求值而漂移。

图里有三条条件边：
1. `route_from_supervisor`：routing.next → 节点名。纯映射。
2. `route_after_budget`：超支且未超限 → reselect_guard（递增计数 + 打降预算标记）→ poi_agent；
   超支出尽 → fallback 的入口由 supervisor 决定（这里先回 supervisor）；
   不超支 → supervisor。
3. `route_after_review`：通过 → END（复盘是最后一关，通过即交付完成，无需再回大脑）；
   不通过且未超限 → repair_guard（递增计数）→ 按失败项分派（时间冲突 → route_agent；
   事实错误 → poi_agent）；超限 → fallback。
"""
from __future__ import annotations

from typing import Dict, List

from ..models.review import ReviewResult
from ..state import GraphState

# 节点名常量：避免各处硬编码字符串拼错
SUPERVISOR = "supervisor"
POI = "poi_agent"
ROUTE = "route_agent"
BUDGET = "budget_agent"
ITINERARY = "itinerary_agent"
REVIEW = "review_agent"
FALLBACK = "fallback"
RESELECT_GUARD = "reselect_guard"
REPAIR_GUARD = "repair_guard"
END = "end"

# 预算超支时写进 state 的"降低预算"约束标记（poi_agent 的 prompt 与硬过滤都读它）
BUDGET_PRESSURE = "over_budget_reselect"

# 预算回退上限：最多重选 2 次
MAX_RESELECT = 2
# review 修复路由上限：最多 2 次
MAX_REPAIR = 2

_ALL_NODES = {
    SUPERVISOR, POI, ROUTE, BUDGET, ITINERARY, REVIEW,
    FALLBACK, RESELECT_GUARD, REPAIR_GUARD, END,
}

# review 失败项 → 负责修的专家。顺序即优先级：事实错误最该先修。
_REVIEW_ROUTE: Dict[str, str] = {
    "poi_exists": POI,          # 事实错误（编造景点 / 名称被改）→ 回 poi 重选
    "budget": POI,              # 预算超支 → 回 poi 砍点（budget 自身没有 pois 写权限）
    "time_conflict": ROUTE,     # 时间冲突本质是顺序/通勤问题 → 回 route 重排
    "daily_commute": ROUTE,     # 通勤超阈值 → 回 route 重排
    "review_internal": ITINERARY,
}


def route_from_supervisor(state: GraphState) -> str:
    """把 supervisor 的 RoutingDecision.next 映射为节点名。routing 缺失时安全结束。"""
    routing = state.get("routing")
    if routing is None:
        return END
    name = routing.next.value  # NextStep 的值本身就是节点名（"poi_agent" 等）
    return name if name in _ALL_NODES else END


def budget_is_over(state: GraphState) -> bool:
    """预算是否超支。只看 budget_agent 的确定性结论，不重算。"""
    report = state.get("budget_report")
    return report is not None and not report.within_budget


def route_after_budget(state: GraphState) -> str:
    """预算节点之后：超支且还有重选额度 → 经 guard 回 poi；否则交给 supervisor。

    - 超支 + 有额度 → RESELECT_GUARD（该 guard 负责 ++reselect_count 并写 budget_pressure）
    - 超支 + 额度用尽 → SUPERVISOR（由它推进到 itinerary/review，最终 fallback 标记 partial）
    - 未超支 → SUPERVISOR
    """
    if budget_is_over(state):
        if state.get("reselect_count", 0) < MAX_RESELECT:
            return RESELECT_GUARD
        return SUPERVISOR
    return SUPERVISOR


def route_after_review(state: GraphState) -> str:
    """review 之后：通过 → 直接 END；不通过 → 按失败项回对应专家；超限 → fallback。

    为什么通过后直接 END 而不回 supervisor：supervisor 的规则里"review_passed==true → end"
    本来就是一条纯转发（它没有任何信息增量），回一趟只是多跑一次 LLM、多记一条 trace。
    而 review 是整个链路的最后一道关，通过即交付完成 —— 直接收口更清晰，
    也让"复盘通过"这件事在图拓扑上可见（review --> END 是一条实线）。
    """
    review: ReviewResult | None = state.get("review")
    if review is None:
        return SUPERVISOR
    if review.passed:
        return END

    if state.get("repair_count", 0) >= MAX_REPAIR:
        return FALLBACK

    target = _pick_repair_target(review)
    if target == FALLBACK:
        return FALLBACK
    return REPAIR_GUARD


def route_after_repair_guard(state: GraphState) -> str:
    """repair_guard 之后：真正分派到负责修的那个专家。"""
    review: ReviewResult | None = state.get("review")
    if review is None:
        return FALLBACK
    return _pick_repair_target(review)


def _pick_repair_target(review: ReviewResult) -> str:
    """按失败项在 _REVIEW_ROUTE 里的优先级选一个修复者。

    只挑第一个命中的：多目标同时失败时，一次只修最上游的那个，
    避免"同一轮里既回 poi 又回 route"造成控制流分叉混乱。
    """
    failed = {c.name for c in review.checks if not c.passed}
    for check_name, node in _REVIEW_ROUTE.items():
        if check_name in failed:
            return node
    return FALLBACK


def failed_check_names(state: GraphState) -> List[str]:
    """便利函数：当前 review 未通过的检查项名（供测试与日志断言）。"""
    review = state.get("review")
    if review is None:
        return []
    return [c.name for c in review.checks if not c.passed]
