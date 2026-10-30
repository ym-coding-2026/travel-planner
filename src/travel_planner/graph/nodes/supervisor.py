"""Supervisor 节点：中心化路由的唯一决策者（硬约束 1）。

职责边界：
- 本节点是整张图的"大脑"/路由器。它**只读**共享 GraphState，产出唯一的结构化结果
  `RoutingDecision`（next 枚举 + reason_code），**不**亲自做 POI 挑选、路线计算、
  预算核算、行程拼装或复盘——那些是各专家节点的事。专家之间不互相通信，只通过
  读写 GraphState 交换数据；Supervisor 通过读 State 判断进度，写回 `routing` 指挥下一步。
- 条件边（edges.py）只是机械地把 `state["routing"].next` 映射到对应节点；真正的"思考"
  在 Supervisor 这里，符合"中心化编排"。

何时会被"再次调用"（重入）：
- 在「Supervisor 重入式」编排里，本节点在 START 运行一次，之后**每当任意一个专家节点
  执行完毕，条件边会把控制权交回本节点**，由它根据最新 State 决定下一个 next。
- 典型重入触发点：
  * poi_agent 选出候选后 → 回到 Supervisor 决定去 route / budget；
  * budget_agent 报超支且 reselect_count < MAX → 回到 Supervisor，它应路由回 poi_agent
    （带 excluded_poi_ids 重选），这正是硬约束 3 的回退边燃料；
  * 某专家把错误写进 state["errors"] → 回到 Supervisor，它据此决定重试该专家或降级到 end；
  * review_agent 通过 → 路由到 end。
- 注意：Supervisor 不会被"另一个 Supervisor"调用——它就是唯一的 Supervisor；所谓"再次调用"
  是图的条件边把控制流循环回本节点（re-entrant loop）。

失败处理（硬约束：绝不抛异常中断整图）：
- 工具（LLM）调用失败，或 LLM 返回不符合 RoutingDecision schema 时，捕获异常，
  写入 state["errors"]，并走降级路径：返回一份"安全"的 RoutingDecision
  （默认 end，仅在尚未选出任何 POI 时降级到 poi_agent 以尝试推进），保证图不崩溃、可观测。
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Optional

from pydantic import ValidationError

from ...models.routing import NextStep, RoutingDecision
from ...state import GraphState, StepRecord

PROMPT_PATH = Path(__file__).resolve().parents[2] / "prompts" / "supervisor.md"
# 与阶段 0 回退上限保持一致：budget 超支最多重选 2 次
_MAX_RESELECT = 2


class SupervisorError(RuntimeError):
    """Supervisor 的 LLM 调用 / 结构化解析失败，供节点捕获后走降级路径。"""


def _load_prompt() -> str:
    return PROMPT_PATH.read_text(encoding="utf-8")


def _state_summary(state: GraphState) -> dict:
    """把 GraphState 压缩成 LLM 能读懂的进度摘要（不含散文，便于确定性路由）。

    注意：判"某一步做没做过"必须用**该步骤专属的产出字段**，不能用"下游字段非空"来推断。
    踩过的坑：曾用 `segments` 非空判断"路线算过"，但当 POI 只有 1 个时路线段合法为空，
    supervisor 便一直认为路线没算，把 route_agent 反复调用了十几次直到撞递归上限。
    现在统一用 `route_order`（只要算过就一定非空）、`budget_report`、`itinerary`。
    """
    budget = state.get("budget_report")
    review = state.get("review")
    return {
        "has_request": state.get("request") is not None,
        "poi_count": len(state.get("pois") or []),
        # 路线是否算过：route_order 是 route_agent 的专属产物，算过必非空
        "has_route_order": bool(state.get("route_order")),
        "segment_count": len(state.get("segments") or []),
        "has_budget": budget is not None,
        "has_itinerary": state.get("itinerary") is not None,
        "budget_within": (budget.within_budget if budget else None),
        "reselect_count": state.get("reselect_count", 0),
        "repair_count": state.get("repair_count", 0),
        "excluded_poi_ids": state.get("excluded_poi_ids", []),
        "budget_pressure": state.get("budget_pressure"),
        "review_passed": (review.passed if review else None),
        "error_count": len(state.get("errors") or []),
    }


async def call_supervisor_llm(state: GraphState) -> RoutingDecision:
    """调用 LLM，产出结构化 RoutingDecision。

    任何失败（LLM 不可用 / 输出不符合 schema）都统一转为 SupervisorError，
    交给节点函数做降级处理——本函数本身不吞异常。
    """
    try:
        from ...llm.client import structured_model

        system = _load_prompt()
        user = "当前规划状态（JSON）：\n" + json.dumps(
            _state_summary(state), ensure_ascii=False
        )
        structured = structured_model(RoutingDecision)
        decision = await structured.ainvoke(
            [{"role": "system", "content": system}, {"role": "user", "content": user}]
        )
        # 防御：部分 provider 可能返回 dict 而非模型实例
        if isinstance(decision, dict):
            decision = RoutingDecision(**decision)
        return decision
    except Exception as e:  # noqa: BLE001 — 统一降级，不让异常穿透到图
        raise SupervisorError(f"supervisor LLM 调用/解析失败: {e}") from e


def _degraded_decision(state: GraphState) -> RoutingDecision:
    """降级路由：能推进就推进，否则安全结束，避免死循环。

    - 还没选出任何 POI：降级到 poi_agent 尝试推进；
    - 否则：直接 end（最安全，避免反复错误路由形成环）。

    **必须处理"LLM 整体不可用"这一情况**（实测踩到）：没有 API Key 时
    supervisor 和 poi_agent 会同时失败，降级规则说"没 POI 就去 poi_agent"，
    poi 又失败、不写 pois，于是 supervisor→poi 无限循环，直到撞上
    recursion_limit(80) 才以 failed 收场 —— 用户等了 80 步，只拿到一句
    "规划步数超限"，问不出真正原因。

    所以这里加一条"连续失败"判据：最近 4 次降级尝试里已经因为 LLM 失败走了 >= 2 次，
    就判定为"模型侧不可用"，直接 end（由 graph 收口成 partial/failed），
    把真实错误原样留在 errors 里。这样失败是**快速且可解释**的。
    """
    if not state.get("pois"):
        if _llm_seems_dead(state):
            return RoutingDecision(next=NextStep.END, reason_code="llm_unavailable_abort")
        return RoutingDecision(next=NextStep.POI, reason_code="supervisor_fallback_to_poi")
    return RoutingDecision(next=NextStep.END, reason_code="supervisor_fallback_end")


# LLM 不可用的判据：trace 末尾窗口里，LLM 类失败出现的次数上限
_LLM_FAILURE_WINDOW = 4
_LLM_FAILURE_ABORT_AT = 2
_LLM_FAILURE_STATUSES = {"llm_error", "degraded"}


def _llm_seems_dead(state: GraphState) -> bool:
    """从最近的 trace 判断 LLM 是否整体不可用（而不是某一次偶发失败）。"""
    recent = (state.get("trace") or [])[-_LLM_FAILURE_WINDOW:]
    fails = sum(1 for t in recent if getattr(t, "status", "") in _LLM_FAILURE_STATUSES)
    return fails >= _LLM_FAILURE_ABORT_AT


async def supervisor_node(state: GraphState) -> dict:
    """LangGraph 节点：读共享 State，产出 RoutingDecision。

    签名符合 LangGraph 规范（state: GraphState -> dict），只返回需要更新的字段。
    失败时写入 errors 并走降级路径，**绝不抛异常中断整图**。
    """
    try:
        decision = await call_supervisor_llm(state)
        status = "ok"
        errors: list[str] = []
    except SupervisorError as e:
        decision = _degraded_decision(state)
        status = "degraded"
        errors = [str(e)]

    step = StepRecord(
        agent="supervisor",
        action=f"route->{decision.next.value}",
        status=status,
        ts=datetime.now().isoformat(timespec="seconds"),
    )
    return {"routing": decision, "trace": [step], "errors": errors}
