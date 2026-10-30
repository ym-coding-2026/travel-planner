"""guard 节点：只做"记账"，不做规划。

为什么需要它们（而不是在条件边里写 state）：
- LangGraph 的条件边函数只负责返回路由键，它**不保证**写入的 state 会生效，也不保证
  只被调用一次。把 `reselect_count += 1` 写在边里，计数可能漂移，回退上限就形同虚设。
- 所以把副作用搬进独立的轻量节点：边决定"是否要回退"，guard 节点负责"记一笔账"，
  然后再由下一条边分派到真正的专家。这样控制流看得见、计数可单测、面试可讲清。

reselect_guard：预算超支回 POI 之前经过。
  - reselect_count += 1
  - 写 budget_pressure = "over_budget_reselect"（poi_agent 的 prompt 与硬过滤都读它）
  - 把 budget_report.suggested_cuts 追加进 excluded_poi_ids（回退边的核心燃料）

repair_guard：review 不通过、要回专家修复之前经过。
  - repair_count += 1
  - 按 review 的失败项补充 excluded_poi_ids（事实错误时排除编造/有问题的 POI）
"""
from __future__ import annotations

from datetime import datetime
from typing import List

from ...models.budget import BudgetReport
from ...models.review import IssueSeverity, ReviewResult
from ...state import GraphState, StepRecord
from ..edges import BUDGET_PRESSURE


def _dedupe(items: List[str]) -> List[str]:
    seen: set[str] = set()
    out: List[str] = []
    for x in items:
        if x and x not in seen:
            seen.add(x)
            out.append(x)
    return out


async def reselect_guard_node(state: GraphState) -> dict:
    """预算超支 → 回 POI 重选前的记账节点。"""
    used = int(state.get("reselect_count", 0)) + 1
    excluded = list(state.get("excluded_poi_ids") or [])

    report: BudgetReport | None = state.get("budget_report")
    cuts: List[str] = []
    if report is not None:
        cuts = list(report.suggested_cuts or [])

    merged = _dedupe(excluded + cuts)
    step = StepRecord(
        agent="reselect_guard",
        action=f"reselect#{used}/excluded+{len(merged) - len(excluded)}",
        status="ok",
        ts=datetime.now().isoformat(timespec="seconds"),
    )
    return {
        "reselect_count": used,
        "excluded_poi_ids": merged,
        "budget_pressure": BUDGET_PRESSURE,
        "trace": [step],
    }


async def repair_guard_node(state: GraphState) -> dict:
    """review 不通过 → 回专家修复前的记账节点。"""
    used = int(state.get("repair_count", 0)) + 1
    excluded = list(state.get("excluded_poi_ids") or [])

    review: ReviewResult | None = state.get("review")
    extra: List[str] = []
    if review is not None:
        for issue in review.issues:
            # 只排除"事实性错误"涉及的 POI：编造的点必须从候选池里彻底剔除，
            # 否则下一轮 poi_agent 可能又把它选回来（LLM 对同一批候选有偏好惯性）。
            if issue.severity == IssueSeverity.ERROR and issue.code in {
                "poi_not_in_candidates",
                "poi_name_mismatch",
                "poi_duplicated",
            }:
                extra.extend(issue.related_poi_ids or [])

    step = StepRecord(
        agent="repair_guard",
        action=f"repair#{used}/excluded+{len(extra)}",
        status="ok",
        ts=datetime.now().isoformat(timespec="seconds"),
    )
    return {
        "repair_count": used,
        "excluded_poi_ids": _dedupe(excluded + extra),
        "trace": [step],
    }
