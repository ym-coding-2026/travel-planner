"""POI 专家节点：调用高德 MCP 选点并产出 POI 列表（硬约束 1/3 的承载者）。

职责边界：
- 本节点是「选点专家」。它**只**负责两件事：① 调用高德 MCP 的 search_poi 获取候选；
  ② 由 LLM 从这些候选中挑选出符合用户偏好的 POI，最终写入 state["pois"]（list[POI]，
  阶段 0 定义的模型）。它**不**算路线、不算预算、不拼行程、不复盘——那些是其它专家的事。
  专家之间不互相通信，只通过读写 GraphState 交换数据。
- 输入约束来自只读锚点 state["request"]（目的地 / 偏好 / 天数 / 每人预算等）；
  回退重选时消费 state["excluded_poi_ids"]（硬约束 3 的回退边：排除上次导致超支的点）。
- 输出：state["pois"]（结构化 POI 列表），绝不写散文。

何时会被 Supervisor 再次调用（重入）：
- 首次（尚无任何 POI）：Supervisor 路由 initial → poi_agent 产出第一批点。
- 预算超支回退（硬约束 3）：budget_agent 报超支且 reselect_count < MAX 时，Supervisor 路由
  回 poi_agent，并携带更新后的 excluded_poi_ids；本节点据此排除贵的点重选。
- 复盘不达标：review_agent 标记 POI 覆盖不足 / 质量不达标时，Supervisor 可路由回本节点重选。

失败处理（硬约束：绝不抛异常中断整图）：
- 工具（MCP search_poi）调用失败 → 包成 PoiToolError；
- LLM 选点决策失败或输出不符合 PoiSelection schema → 包成 PoiError。
- 任一失败都被节点捕获，写入 state["errors"] 并走降级：不更新 pois（保持原状），让 Supervisor
  下一轮看到 pois 未推进 + error，决定重试或降级 end。绝不抛异常。
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field

from ...models.poi import POI
from ...models.requests import UserRequest
from ...state import GraphState, StepRecord

PROMPT_PATH = Path(__file__).resolve().parents[2] / "prompts" / "poi.md"


class PoiSelection(BaseModel):
    """LLM 的结构化选点决策（poi_agent 内部使用，不是跨 Agent 传递模型）。"""

    selected_poi_ids: list[str] = Field(
        description="从候选中选中的 POI id 列表，必须全部来自输入候选"
    )
    reason: str = Field(
        default="", description="简短结构化理由，下划线短词，非散文"
    )


class PoiToolError(RuntimeError):
    """MCP search_poi 调用失败，供节点捕获后走降级路径。"""


class PoiError(RuntimeError):
    """LLM 选点决策 / 结构化解析失败，供节点捕获后走降级路径。"""


def _load_prompt() -> str:
    return PROMPT_PATH.read_text(encoding="utf-8")


def _dedupe_preserve_order(items: list[POI]) -> list[POI]:
    seen: set[str] = set()
    out: list[POI] = []
    for p in items:
        if p.poi_id not in seen:
            seen.add(p.poi_id)
            out.append(p)
    return out


async def call_poi_tools(state: GraphState) -> list[POI]:
    """调用高德 MCP 的 search_poi 获取候选 POI。

    检索策略（重要，曾经踩坑）：
    - **偏好是加权信号，不是硬过滤条件**。若只按偏好关键词检索，命中会非常少
      （实测北京"历史"只命中 1 个、"美食"只命中 1 个；上海"历史"命中 0 个），
      候选池过窄会导致后续规划不出足够的景点、也排不出路线段。
    - 因此这里做 **偏好检索 ∪ 全量检索**：偏好命中的点排在前面（让 LLM 优先看到），
      再把全量检索结果补充在后面，保证候选池足够大、规划有得选。
    - 全量检索失败但偏好检索成功时，仍返回偏好结果（不因补充失败而丢掉已有候选）。

    任何失败（超时 / 重试耗尽 / 网络 / 配置错误）统一转为 PoiToolError，交给节点降级。
    """
    try:
        from ...tools.amap_tools import get_client

        req: UserRequest = state["request"]
        client = get_client()
        preferred: list[POI] = []
        async with client:
            # 1) 偏好检索（每个偏好一个关键词）
            for kw in req.preferences or []:
                found = await client.search_poi(
                    city=req.destination, keywords=kw, category=""
                )
                preferred.extend(found)
            preferred = _dedupe_preserve_order(preferred)

            # 2) 全量检索兜底，保证候选池够大（偏好点保持在前面）
            try:
                everything = await client.search_poi(
                    city=req.destination, keywords="", category=""
                )
            except Exception:  # noqa: BLE001 — 补充失败不应丢掉已有候选
                everything = []

        merged = _dedupe_preserve_order(preferred + list(everything))
        if not merged:
            return []
        return merged
    except PoiToolError:
        raise
    except Exception as e:  # noqa: BLE001 — 统一降级，不让异常穿透到图
        raise PoiToolError(f"MCP search_poi 调用失败: {e}") from e


async def call_poi_llm(
    candidates: list[POI], request: UserRequest, budget_pressure: str | None = None
) -> PoiSelection:
    """调用 LLM，从候选 POI 中结构化选出符合偏好的点。

    `budget_pressure` 是回退边写入的"降低预算"约束标记（见 prompts/poi.md 的 budget_pressure 段），
    非空时要求模型优先避开高价门票项。失败转为 PoiError，交给节点降级。
    """
    try:
        from ...llm.client import structured_model

        system = _load_prompt()
        user = json.dumps(
            {
                "preferences": request.preferences,
                "days": request.days,
                "travelers": request.travelers,
                "max_poi_per_day": request.max_poi_per_day,
                "excluded_poi_ids": request.excluded_poi_ids,
                "budget_pressure": budget_pressure or "",
                "candidate_count": len(candidates),
                "candidates": [c.model_dump(mode="json") for c in candidates],
            },
            ensure_ascii=False,
        )
        structured = structured_model(PoiSelection)
        selection = await structured.ainvoke(
            [{"role": "system", "content": system}, {"role": "user", "content": user}]
        )
        # 防御：部分 provider 可能返回 dict 而非模型实例
        if isinstance(selection, dict):
            selection = PoiSelection(**selection)
        return selection
    except Exception as e:  # noqa: BLE001 — 统一降级，不让异常穿透到图
        raise PoiError(f"poi LLM 选点/解析失败: {e}") from e


async def poi_agent_node(state: GraphState) -> dict:
    """LangGraph 节点：调工具拿候选 → LLM 选点 → 写回 state["pois"]。

    签名符合 LangGraph 规范（state: GraphState -> dict），只返回需要更新的字段。
    失败时写入 errors 并走降级路径（不更新 pois），绝不抛异常中断整图。
    """
    # 1) 检索候选：失败则降级，不更新 pois
    try:
        candidates = await call_poi_tools(state)
    except PoiToolError as e:
        step = StepRecord(
            agent="poi_agent",
            action="search_poi",
            status="tool_error",
            ts=datetime.now().isoformat(timespec="seconds"),
        )
        return {"trace": [step], "errors": [str(e)]}

    # 回退边消费：排除上次因超支被剔除的点（硬约束 3）
    excluded = set(state.get("excluded_poi_ids") or [])
    candidates = [c for c in candidates if c.poi_id not in excluded]

    pressure = state.get("budget_pressure")
    if pressure and excluded:
        # 兜底硬约束：被回退重选时，把候选里门票偏高的尾巴直接砍掉，
        # 不完全依赖 LLM 自觉——否则"降低预算"可能只是口头上答应。
        prices = sorted((c.ticket_price or 0.0) for c in candidates)
        if prices:
            median = prices[len(prices) // 2]
            kept = [c for c in candidates if (c.ticket_price or 0.0) <= median]
            if len(kept) >= 2:
                candidates = kept

    # 2) LLM 选点：失败则降级，不更新 pois
    try:
        selection = await call_poi_llm(candidates, state["request"], pressure)
    except PoiError as e:
        step = StepRecord(
            agent="poi_agent",
            action="select",
            status="llm_error",
            ts=datetime.now().isoformat(timespec="seconds"),
        )
        return {"trace": [step], "errors": [str(e)]}

    # 3) 仅保留 LLM 选中的 POI；若选中为空（异常决策），降级保留全部候选并告警
    chosen_ids = set(selection.selected_poi_ids)
    chosen = [p for p in candidates if p.poi_id in chosen_ids]
    if not chosen:
        chosen = candidates
        note = "poi_selection_empty_fallback"
    else:
        note = "ok"

    step = StepRecord(
        agent="poi_agent",
        action=f"select->{len(chosen)}pois",
        status=note,
        ts=datetime.now().isoformat(timespec="seconds"),
    )
    # 同时写回**原始候选集**：review_agent 要用它做"景点是否真实存在于检索结果"的事实校验。
    # 若只写 pois，review 拿 pois 校验 pois 会恒通过，等于没校验。
    return {
        "pois": chosen,
        "poi_candidates": candidates,
        "trace": [step],
        "errors": [],
    }
