"""fallback 节点：重试预算耗尽后的「部分满足」交付（硬约束 3 的兜底出口）。

职责边界：
- 本节点**不做任何规划工作**，也**不修改**已有的 pois / itinerary / budget_report。
  它只做一件事：把「已经跑到哪一步、哪些环节反复失败、为什么只能部分满足」写成结构化
  结论（partial=True + 具体原因列表），保证**用户永远拿到一份可读的结果，而不是一堆报错**。
  这正是硬约束 3「预算超支时回到 POI 重选，而不是直接失败」的最后一道兜底：
  重选 2 次仍超支 → 不崩溃，交付"超支但可用"的行程并明确告知。

为什么需要这个节点（设计判断）：
- 直接在条件边里 `END` 也能结束，但那样的输出是一个"沉默的失败"：调用方拿到 itinerary，
  却不知道它是否可信、是否被降级过。fallback 节点让"不完全满足"这件事**显式可见**，
  API 层可以据此返回 200 + partial=true（而不是 500），评测也能统计部分满足率。

何时被路由过来：
- reselect_count > MAX_RESELECT：预算回退已达上限仍超支；
- repair_count > MAX_REPAIR：review 反复不通过、修复路由已达上限；
- 专家连续产出错误（errors 堆积）导致 Supervisor 无法推进时。
"""
from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from ...models.budget import BudgetReport
from ...models.plan import Itinerary
from ...models.review import ReviewResult
from ...state import GraphState, StepRecord

MAX_RESELECT = 2
MAX_REPAIR = 2


def summarize_reasons(state: GraphState) -> List[str]:
    """从 State 里提取"为什么只能部分满足"的结构化原因（只读，不改状态）。"""
    reasons: List[str] = []

    budget: Optional[BudgetReport] = state.get("budget_report")
    if state.get("reselect_count", 0) > MAX_RESELECT or (
        budget is not None and not budget.within_budget
    ):
        if budget is not None and not budget.within_budget:
            reasons.append(
                f"budget_overrun: 重选 {state.get('reselect_count', 0)} 次后仍超支 "
                f"{budget.over_by} 元（估算 {budget.total_estimated}）"
            )

    review: Optional[ReviewResult] = state.get("review")
    if review is not None and not review.passed:
        failed = [c for c in review.checks if not c.passed]
        for c in failed:
            reasons.append(f"review_failed:{c.name}: {c.detail}")

    if state.get("repair_count", 0) > MAX_REPAIR:
        reasons.append(
            f"repair_exhausted: 修复路由已用满 {MAX_REPAIR} 次，停止继续重试"
        )

    itinerary: Optional[Itinerary] = state.get("itinerary")
    if itinerary is None:
        reasons.append("no_itinerary: 未能产出完整行程")
    elif not itinerary.ordered_poi_ids:
        reasons.append("empty_itinerary: 行程中没有可交付的景点")

    errors = state.get("errors") or []
    if errors:
        reasons.append(f"errors_recorded: 过程中记录 {len(errors)} 条错误（已降级处理）")

    if not reasons:
        reasons.append("unknown: 上游未给出具体原因，返回已有最优结果")
    return reasons


async def fallback_node(state: GraphState) -> dict:
    """LangGraph 节点：标记 partial 并汇总原因。绝不抛异常、绝不修改规划结果。"""
    reasons = summarize_reasons(state)
    notes = "; ".join(reasons)

    step = StepRecord(
        agent="fallback",
        action=f"partial->{len(reasons)}reasons",
        status="partial",
        ts=datetime.now().isoformat(timespec="seconds"),
    )
    return {
        "partial": True,
        "status": "partial",
        "trace": [step],
        # 用一条汇总 error 让上层 API 能直接展示"为什么是部分满足"
        "errors": [f"partial_result: {notes}"],
    }
