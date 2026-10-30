"""route_agent 专家节点：用地理算法规划每日路线，LLM 只做语言润色（硬约束1/3 承载者）。

职责边界：
- 本节点是「路线专家」。它**只**负责：① 用 geo_utils 的确定性算法（KMeans 聚类 + 贪心最近邻）
  把 POI 按天分配并使每日通勤最小；② 调用高德 MCP 的 get_route 把相邻 POI 间的真实距离/时间/费用
  算成 RouteSegment；③ 调用 LLM 对算法结果做**纯语言润色**（每日一句人类可读通勤说明），不依赖
  LLM 做任何地理/排序决策。它**不**选 POI（poi_agent 的事）、不算预算、不拼完整行程、不复盘。
- 输入：只读 state["request"]（天数/交通偏好）+ state["pois"]（poi_agent 产出）。
- 输出：state["segments"]（RouteSegment 列表，算法+工具核心产出）、state["route_order"]（每天 POI
  id 顺序，供 itinerary_agent 使用）、state["route_notes"]（LLM 润色的每日说明）。
- 算法亮点在 geo_utils.py，本文件不重复算法逻辑，便于单测与复用。

何时被 Supervisor 再次调用（重入）：
- poi_agent 产出/重选 POI 后（含预算超支回退重选）→ 需重新规划路线。
- route 自身工具/润色失败重试时。
- review_agent 标记路线不合理（如通勤过长）→ 路由回本节点重排。

失败处理（绝不抛异常中断整图）：
- 工具（MCP get_route）致命失败 → RouteToolError（节点降级：不更新 segments/route_order）。
- 单段 get_route 失败 → 节点用 haversine 近似降级该段，仍产出完整 segments（弹性）。
- LLM 润色失败 → RouteError（节点降级：route_notes 置空，但核心 segments/route_order 已产出，不影响下游）。
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import List

from pydantic import BaseModel, Field

from ...geo_utils import haversine_km, plan_days
from ...models.common import TransportMode
from ...models.poi import POI, RouteSegment
from ...models.requests import TransportPref, UserRequest
from ...state import GraphState, StepRecord

PROMPT_PATH = Path(__file__).resolve().parents[2] / "prompts" / "route.md"

# TransportPref（用户偏好）-> TransportMode（MCP 工具接受）映射；flexible 无对应，默认公共交通
_MODE_MAP = {
    TransportPref.PUBLIC: TransportMode.PUBLIC,
    TransportPref.DRIVE: TransportMode.DRIVE,
    TransportPref.WALK: TransportMode.WALK,
    TransportPref.FLEXIBLE: TransportMode.PUBLIC,
}


class RouteNarrative(BaseModel):
    """LLM 的纯语言润色产出（route_agent 内部使用）。"""

    day_notes: List[str] = Field(
        default_factory=list, description="每天一句人类可读通勤说明，与天数一一对应"
    )


class RouteToolError(RuntimeError):
    """MCP get_route 致命调用失败，供节点捕获后走降级路径。"""


class RouteError(RuntimeError):
    """LLM 润色 / 结构化解析失败，供节点捕获后走降级路径。"""


def _load_prompt() -> str:
    return PROMPT_PATH.read_text(encoding="utf-8")


def _resolve_mode(req: UserRequest) -> TransportMode:
    return _MODE_MAP.get(req.transport_pref, TransportMode.PUBLIC)


async def call_route_tools(state: GraphState, order: List[List[POI]]) -> List[RouteSegment]:
    """对每日有序 POI 的相邻对调用 MCP get_route，产出 RouteSegment。

    单段失败用 haversine 近似降级（仍产出完整 segments，弹性）；仅当客户端初始化等
    致命错误才抛 RouteToolError 交给节点降级。
    """
    try:
        from ...tools.amap_tools import get_client

        req: UserRequest = state["request"]
        mode = _resolve_mode(req)
        client = get_client()
        segments: List[RouteSegment] = []
        async with client:
            for day in order:
                for i in range(len(day) - 1):
                    a, b = day[i], day[i + 1]
                    try:
                        info = await client.get_route(
                            a.location.lat, a.location.lng,
                            b.location.lat, b.location.lng, mode.value,
                        )
                        segments.append(RouteSegment(
                            from_poi_id=a.poi_id, to_poi_id=b.poi_id,
                            mode=info.mode, distance_km=info.distance_km,
                            duration_min=info.duration_min, cost=info.cost,
                        ))
                    except Exception:
                        # 弹性降级：haversine 近似该段（不抛，保证 segments 完整）
                        d = haversine_km(a.location, b.location)
                        segments.append(RouteSegment(
                            from_poi_id=a.poi_id, to_poi_id=b.poi_id,
                            mode=mode, distance_km=round(d, 3),
                            duration_min=max(1, int(d / 4.0 * 60)),
                            cost=0.0,
                        ))
        return segments
    except RouteToolError:
        raise
    except Exception as e:  # noqa: BLE001 — 统一降级，不让异常穿透到图
        raise RouteToolError(f"MCP get_route 调用失败: {e}") from e


def _draft_day_summaries(order: List[List[POI]], segments: List[RouteSegment]) -> List[str]:
    """算法侧先生成每日通行草稿（含总通勤分钟），交由 LLM 润色措辞。"""
    # 建立 (from,to) -> duration 的查找表
    dur = {(s.from_poi_id, s.to_poi_id): s.duration_min for s in segments}
    summaries: List[str] = []
    for idx, day in enumerate(order):
        if len(day) < 2:
            summaries.append(f"第{idx + 1}天：仅 {len(day)} 个景点，无需转场")
            continue
        total = sum(
            dur.get((p.poi_id, q.poi_id), 0) for p, q in zip(day, day[1:])
        )
        summaries.append(
            f"第{idx + 1}天：{day[0].name}→...→{day[-1].name}，约 {total} 分钟通勤"
        )
    return summaries


async def call_route_llm(order: List[List[POI]], segments: List[RouteSegment], request: UserRequest) -> RouteNarrative:
    """调用 LLM 对算法结果做纯语言润色（每日一句通勤说明）。失败转 RouteError。"""
    try:
        from ...llm.client import structured_model

        system = _load_prompt()
        day_summaries = _draft_day_summaries(order, segments)
        user = json.dumps(
            {
                "transport_pref": request.transport_pref.value,
                "day_summaries": day_summaries,
            },
            ensure_ascii=False,
        )
        structured = structured_model(RouteNarrative)
        narrative = await structured.ainvoke(
            [{"role": "system", "content": system}, {"role": "user", "content": user}]
        )
        # 防御：部分 provider 可能返回 dict 而非模型实例
        if isinstance(narrative, dict):
            narrative = RouteNarrative(**narrative)
        return narrative
    except Exception as e:  # noqa: BLE001 — 统一降级，不让异常穿透到图
        raise RouteError(f"route LLM 润色失败: {e}") from e


async def route_agent_node(state: GraphState) -> dict:
    """LangGraph 节点：算法聚类排序 -> 算段 -> LLM 润色 -> 写回结构化字段。

    签名符合 LangGraph 规范（state: GraphState -> dict），只返回需更新的字段。
    失败时写入 errors 并走降级路径，绝不抛异常中断整图。
    """
    req: UserRequest = state["request"]
    pois: List[POI] = state.get("pois") or []

    # 1) 算法核心：聚类 + 每日内排序（确定性，不依赖 LLM/工具）
    order = plan_days(pois, req.days)

    # 2) 工具：算相邻段。致命失败则降级，不更新 segments / route_order
    try:
        segments = await call_route_tools(state, order)
    except RouteToolError as e:
        step = StepRecord(
            agent="route_agent",
            action="get_route",
            status="tool_error",
            ts=datetime.now().isoformat(timespec="seconds"),
        )
        return {"trace": [step], "errors": [str(e)]}

    # 3) LLM 只做语言润色（核心 segments 已就绪，润色失败不致命）
    try:
        narrative = await call_route_llm(order, segments, req)
        notes = narrative.day_notes
        errors: list[str] = []
    except RouteError as e:
        notes = []
        errors = [str(e)]

    route_order = [[p.poi_id for p in day] for day in order]
    step = StepRecord(
        agent="route_agent",
        action=f"plan->{len(order)}days/{len(segments)}segs",
        status="ok",
        ts=datetime.now().isoformat(timespec="seconds"),
    )
    return {
        "segments": segments,
        "route_order": route_order,
        "route_notes": notes,
        "trace": [step],
        "errors": errors,
    }
