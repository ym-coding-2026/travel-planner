"""itinerary_agent 专家节点：把「每日顺序 + 交通段」拼成带时刻表的 Itinerary。

职责边界：
- 本节点是「行程拼装专家」。它**只**负责把上游已有的结构化结果组装成最终行程：
  读 state["route_order"]（算法聚类排序结果）、state["segments"]（真实交通段）、
  state["pois"]（POI 明细），按「每日 09:00 开始、游览建议时长、加通勤耗时」串成
  DayPlan.schedule（具体时刻），汇总成 Itinerary。它**不**改 POI（poi 的事）、
  **不**排顺序（route 的事）、**不**算预算、**不**做校验。
- 输入：只读 state["request"] / ["pois"] / ["route_order"] / ["segments"] / ["route_notes"]。
- 输出：state["itinerary"]（阶段 0 的 Itinerary 模型）。

为什么排班时刻由代码算而不是 LLM 写：
- review_agent 要拿 schedule 的时刻做时间冲突校验（重叠、倒序）。如果时刻是 LLM 编的，
  同一份输入两次可能排出不同的钟点，甚至排出重叠时段 —— 那 review 实际上是在纠正
  自己上游的随机噪声，训练不出任何可信结论。时刻必须是确定性算术的输出。

排班规则（确定性、可解释）：
- 每天从 09:00 开始（DAY_START_MIN），按 route_order 的顺序逐个排；
- 每个 POI 占用其 suggested_duration_min（缺省 90 分钟）；
- 相邻 POI 之间插入该段 RouteSegment.duration_min；若该段跨了就餐时段（11:30–13:00），
  额外加 LUNCH_MIN 分钟并在 notes 里记一条结构化提示（不是散文）；
- 某天排到 DAY_END_LIMIT_MIN（21:00）仍排不完时，把剩余 POI 顺延到下一天；
  若最后一天也放不下，则截断并在 notes 标注 overflow（不硬塞，避免制造时间冲突）。

失败处理（硬约束：绝不抛异常中断整图）：
- 无 route_order / 无 pois → 不产出 itinerary，写 errors 交由 Supervisor 决定；
- 排程过程任何意外 → 捕获后保留已有字段不动，只写 trace + errors。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from ...models.plan import DayPlan, Itinerary, ScheduledPOI
from ...models.poi import POI, RouteSegment
from ...models.requests import UserRequest
from ...state import GraphState, StepRecord

PROMPT_PATH = Path(__file__).resolve().parents[2] / "prompts" / "itinerary.md"

DAY_START_MIN = 9 * 60          # 09:00 开始
DAY_END_LIMIT_MIN = 21 * 60     # 21:00 必须收工
LUNCH_START = 11 * 60 + 30      # 11:30
LUNCH_END = 13 * 60             # 13:00
LUNCH_MIN = 60                  # 午餐预留 60 分钟


def fmt_hhmm(minutes: int) -> str:
    """当日分钟数 → "HH:MM"。越界时按 24 小时取模，保证输出始终是合法时刻。"""
    minutes = max(0, minutes) % (24 * 60)
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _crosses_lunch(start: int, end: int) -> bool:
    """区间 [start, end) 是否与就餐窗口相交。"""
    return start < LUNCH_END and end > LUNCH_START


def build_itinerary(
    request: UserRequest,
    pois: List[POI],
    route_order: List[List[str]],
    segments: List[RouteSegment],
    route_notes: Optional[List[str]] = None,
) -> Itinerary:
    """纯函数：按确定规则排班，产出 Itinerary。便于单测与复现。"""
    poi_index: Dict[str, POI] = {p.poi_id: p for p in pois}
    seg_index: Dict[Tuple[str, str], RouteSegment] = {
        (s.from_poi_id, s.to_poi_id): s for s in segments
    }
    notes_all = route_notes or []

    day_plans: List[DayPlan] = []
    # 待排队列：(天, poi_id)。先把 route_order 摊平，便于"排不下顺延"
    pending: List[Tuple[int, str]] = [
        (d, pid) for d, ids in enumerate(route_order) for pid in ids
    ]

    total_cost = 0.0
    total_distance = 0.0
    total_duration = 0

    queue = list(pending)
    cursor = 0
    for day_idx in range(request.days):
        schedule: List[ScheduledPOI] = []
        day_segments: List[RouteSegment] = []
        notes: List[str] = []
        day_cost = 0.0
        clock = DAY_START_MIN
        prev_id: Optional[str] = None

        while cursor < len(queue):
            plan_day, pid = queue[cursor]
            poi = poi_index.get(pid)
            if poi is None:
                # 数据不一致（route_order 引用了不存在的 POI）→ 跳过并留痕，不崩
                notes.append(f"missing_poi:{pid}")
                cursor += 1
                continue

            # 该 POI 属于更靠后的天 且 当前天还没排满时，允许提前补位；否则留到它自己的天
            if plan_day > day_idx and schedule:
                break

            dwell = max(15, int(poi.suggested_duration_min or 90))
            travel = 0
            seg: Optional[RouteSegment] = None
            if prev_id is not None:
                seg = seg_index.get((prev_id, pid))
                if seg is not None:
                    travel = max(0, seg.duration_min)

            start = clock + travel
            lunch_bump = 0
            if travel and seg is not None and _crosses_lunch(clock, start):
                lunch_bump = LUNCH_MIN
                notes.append(f"lunch_break:{fmt_hhmm(clock + travel)}")
            start += lunch_bump
            end = start + dwell

            if end > DAY_END_LIMIT_MIN and schedule:
                # 当天排不下 → 顺延到下一天（若已是最后一天则截断）
                if day_idx < request.days - 1:
                    queue[cursor] = (day_idx + 1, pid)
                    break
                notes.append(f"overflow:{pid}")
                cursor += 1
                continue

            schedule.append(
                ScheduledPOI(poi_id=pid, start_time=fmt_hhmm(start), end_time=fmt_hhmm(end))
            )
            if seg is not None:
                day_segments.append(seg)
                day_cost += max(0.0, seg.cost)
                total_distance += max(0.0, seg.distance_km)
                total_duration += travel
            day_cost += max(0.0, poi.ticket_price or 0.0)
            clock = end
            prev_id = pid
            cursor += 1

        if day_idx < len(notes_all) and notes_all[day_idx]:
            notes.insert(0, f"commute_note:{notes_all[day_idx][:60]}")

        total_cost += day_cost
        day_plans.append(
            DayPlan(
                day_index=day_idx,
                date=request.start_date + timedelta(days=day_idx),
                schedule=schedule,
                segments=day_segments,
                day_cost=round(day_cost, 2),
                notes=notes,
            )
        )

    # 队列里还剩的（最后一天都排不下）记到末日的 notes，便于 review 与排查
    leftover = [pid for _, pid in queue[cursor:]]
    if leftover and day_plans:
        day_plans[-1].notes.append("dropped:" + ",".join(leftover))

    ordered = [s.poi_id for d in day_plans for s in d.schedule]
    return Itinerary(
        request_id=f"{request.destination}-{request.start_date.isoformat()}-{request.days}d",
        days=day_plans,
        ordered_poi_ids=ordered,
        total_cost=round(total_cost, 2),
        total_distance_km=round(total_distance, 3),
        total_duration_min=total_duration,
    )


async def itinerary_agent_node(state: GraphState) -> dict:
    """LangGraph 节点：拼装 Itinerary 并写回 state["itinerary"]。

    签名符合 LangGraph 规范（state: GraphState -> dict），只返回需更新的字段。
    本节点纯计算、不调 LLM / 工具，因此没有"外部失败"分支，只有输入不足与意外异常。
    """
    req: Optional[UserRequest] = state.get("request")
    pois: List[POI] = state.get("pois") or []
    route_order = state.get("route_order") or []

    if req is None or not pois or not route_order:
        step = StepRecord(
            agent="itinerary_agent",
            action="precheck",
            status="missing_input",
            ts=datetime.now().isoformat(timespec="seconds"),
        )
        return {
            "trace": [step],
            "errors": [
                "itinerary_agent: 缺少 request / pois / route_order，无法拼装行程"
            ],
        }

    try:
        itinerary = build_itinerary(
            req,
            pois,
            route_order,
            state.get("segments") or [],
            state.get("route_notes") or [],
        )
    except Exception as e:  # noqa: BLE001 — 最终兜底，不让拼装失败打挂图
        step = StepRecord(
            agent="itinerary_agent",
            action="build",
            status="error",
            ts=datetime.now().isoformat(timespec="seconds"),
        )
        return {"trace": [step], "errors": [f"itinerary_agent 异常: {e}"]}

    n_slots = sum(len(d.schedule) for d in itinerary.days)
    step = StepRecord(
        agent="itinerary_agent",
        action=f"build->{len(itinerary.days)}days/{n_slots}slots",
        status="ok",
        ts=datetime.now().isoformat(timespec="seconds"),
    )
    return {"itinerary": itinerary, "trace": [step], "errors": []}
