"""budget_agent 专家节点：核算行程成本并判定是否超支（硬约束 3 回退边的触发器）。

职责边界：
- 本节点是「预算专家」。它**只**负责：① 从 state 里已有的结构化数据（POI 门票/人均、
  RouteSegment 交通费）做**确定性算术**，得出门票/交通/餐饮/住宿各项与总额；
  ② 通过 MCP get_hotel_price 取真实住宿均价；③ 算 within_budget / over_by /
  suggested_cuts，写入 state["budget_report"]（阶段 0 的 BudgetReport 模型）。
- 它**不**选 POI（poi_agent 的事）、不排路线（route_agent 的事）、不拼行程、不复盘。
  也**绝不**自己去改 pois —— 超支时它只输出 `suggested_cuts`（该砍哪些 POI），
  由 Supervisor 据此路由回 poi_agent，把砍点动作交给真正拥有 pois 写权限的专家。
  这是硬约束 1「专家之间不互相通信，只与 Supervisor 交换结构化数据」的直接体现。
- 输入：只读 state["request"]（预算/人数/天数）+ state["pois"] + state["segments"]。
- 输出：state["budget_report"]（BudgetReport）。

为什么算钱不让 LLM 做（关键设计判断）：
- 超支判定是**回退边的触发条件**。如果金额由 LLM 估，同一次请求两次运行可能给出不同
  数字，回退与否就会漂移，离线评测也无法复现（这违背项目"评测不依赖 LLM 打分"的原则）。
- 所以本节点把责任切成两半：**算术 100% 由代码保证**（可单测、可复现）；LLM 只负责
  「选酒店星级档位 + 一句取舍说明」这类主观取舍，且其结果会再经代码校验与兜底。

何时被 Supervisor 再次调用（重入）：
- poi_agent 首轮产出 POI 后、route_agent 算出交通费后 → 首次核算。
- 预算超支回退重选后（poi_agent 重选完）→ 必须重算，否则回退边永远看不到改善。
- route_agent 因复盘重排导致交通费变化 → 重算。
- 本节点自身工具失败，Supervisor 决定重试本节点。

失败处理（硬约束：绝不抛异常中断整图）：
- get_hotel_price 失败 → 用内置基准价目表降级估算（标注 source=fallback），仍产出完整
  BudgetReport；错误写入 state["errors"]。住宿费缺失会让整个预算失真，不能因此中断规划。
- LLM 选档失败 / 输出不在候选档位内 → 用确定性规则兜底（超支取最低档，否则取中间档）。
- 只有「state 中无任何 POI 且无交通段」这种无意义输入才整体降级：不写 budget_report，
  只记 trace + errors，交由 Supervisor 决定下一步。
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import List

from pydantic import BaseModel, Field, field_validator

from ...models.budget import BudgetCategory, BudgetItem, BudgetReport
from ...models.poi import POI, RouteSegment
from ...models.requests import UserRequest
from ...state import GraphState, StepRecord

PROMPT_PATH = Path(__file__).resolve().parents[2] / "prompts" / "budget.md"

# 三档可选酒店星级（高德 / 第三方都只提供这几档，见 MCP get_hotel_price 校验）
STAR_LEVELS: tuple[int, int, int] = (3, 4, 5)

# 工具失败时的降级基准价（元/晚，3 星基准），按城市系数粗略修正。
# 数值刻意保守（偏低），宁可让预算显得宽松也不误判"可省"从而多砍景点。
_FALLBACK_HOTEL_BASE = {3: 380.0, 4: 620.0, 5: 1100.0}
_FALLBACK_CITY_MULT = {"北京": 1.25, "上海": 1.30, "成都": 0.95, "广州": 1.10, "深圳": 1.15}

# 餐饮估算：每人每天基准（不含已含在 POI 内的餐费）
_MEAL_PER_PERSON_PER_DAY = 150.0


class BudgetPlan(BaseModel):
    """LLM 的意图层产出（只表达取舍，不含金额计算）。"""

    star_level: int = Field(description="选定的酒店星级，必须来自候选档位；0 表示不住宿")
    reason_code: str = Field(
        default="balanced",
        description="budget_first / balanced / comfort_first / no_hotel",
    )
    note: str = Field(default="", description="一句不超过 40 字的取舍说明")

    @field_validator("star_level")
    @classmethod
    def _non_negative(cls, v: int) -> int:
        if v < 0:
            raise ValueError("star_level 不能为负")
        return v


class BudgetToolError(RuntimeError):
    """MCP get_hotel_price 调用失败，供节点捕获后走降级路径。"""


class BudgetError(RuntimeError):
    """LLM 选档 / 结构化解析失败，供节点捕获后走降级路径。"""


def _load_prompt() -> str:
    return PROMPT_PATH.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# 确定性算术层（不依赖 LLM / 网络，可单测）
# --------------------------------------------------------------------------- #
def nights_for(request: UserRequest) -> int:
    """住宿晚数：N 天行程住 N-1 晚（当天返程不住）。最少 0。"""
    return max(0, request.days - 1)


def ticket_total(pois: List[POI], travelers: int) -> float:
    """门票小计 = Σ(单人门票) × 人数。ticket_price 为 None 视为免费。"""
    per_person = sum(max(0.0, p.ticket_price or 0.0) for p in pois)
    return round(per_person * max(1, travelers), 2)


def transport_total(segments: List[RouteSegment]) -> float:
    """当地交通小计 = Σ 各段费用（RouteSegment.cost 已按整车/每人一次算好）。"""
    return round(sum(max(0.0, s.cost) for s in segments), 2)


def food_total(days: int, travelers: int, pois: List[POI]) -> float:
    """餐饮小计：按人天基准估算，再扣除 POI 内已含餐的溢补。

    这里刻意保持简单可解释：人数 × 天数 × 基准，并可被 special_needs 调整（此处未用）。
    复杂化反而让面试追问时说不清口径。
    """
    return round(_MEAL_PER_PERSON_PER_DAY * max(1, travelers) * max(1, days), 2)


def accommodation_total(nights: int, price_per_night: float, rooms: int) -> float:
    """住宿小计 = 每晚单价 × 晚数 × 房间数。房间数按 2 人 1 间估算。"""
    if nights <= 0:
        return 0.0
    return round(max(0.0, price_per_night) * nights * max(1, rooms), 2)


def rooms_for(travelers: int) -> int:
    """房间数：按 2 人 1 间向上取整（标间假设，足够做量级估算）。"""
    return max(1, (max(1, travelers) + 1) // 2)


def pick_cuts(pois: List[POI], over_by: float) -> List[str]:
    """超支时给出该砍的 POI id（按「门票贵 → 门票便宜」的顺序，砍够为止）。

    只挑**有门票**的点（免费点砍了不省钱还损失内容），返回 id 供 poi_agent 消费。
    这是硬约束 3 回退边的输入：BudgetReport.suggested_cuts → excluded_poi_ids。
    """
    if over_by <= 0:
        return []
    paid = [p for p in pois if (p.ticket_price or 0.0) > 0]
    # 确定性排序：单价降序，同价按 poi_id 升序，保证评测可复现
    paid.sort(key=lambda p: (-(p.ticket_price or 0.0), p.poi_id))
    cuts: List[str] = []
    saved = 0.0
    for p in paid:
        # 至少保留 1 个景点，否则行程失去意义
        if len(pois) - len(cuts) <= 1:
            break
        cuts.append(p.poi_id)
        saved += p.ticket_price or 0.0
        if saved >= over_by:
            break
    return cuts


def build_report(
    request: UserRequest,
    pois: List[POI],
    segments: List[RouteSegment],
    price_per_night: float,
    hotel_source: str = "mcp",
) -> BudgetReport:
    """纯函数：用确定性算术拼出 BudgetReport。所有金额口径集中在这里，便于单测与解释。"""
    travelers = max(1, request.travelers)
    nights = nights_for(request)
    rooms = rooms_for(travelers)

    t_ticket = ticket_total(pois, travelers)
    t_transport = transport_total(segments)
    t_food = food_total(request.days, travelers, pois)
    t_hotel = accommodation_total(nights, price_per_night, rooms)
    total = round(t_ticket + t_transport + t_food + t_hotel, 2)

    budget = float(request.total_budget)
    within = total <= budget
    over_by = round(max(0.0, total - budget), 2)

    items = [
        BudgetItem(
            category=BudgetCategory.TICKET,
            amount=t_ticket,
            detail=f"{len(pois)}个景点×{travelers}人",
        ),
        BudgetItem(
            category=BudgetCategory.TRANSPORT,
            amount=t_transport,
            detail=f"{len(segments)}段当地交通",
        ),
        BudgetItem(
            category=BudgetCategory.FOOD,
            amount=t_food,
            detail=f"{request.days}天×{travelers}人×{int(_MEAL_PER_PERSON_PER_DAY)}元",
        ),
        BudgetItem(
            category=BudgetCategory.ACCOMMODATION,
            amount=t_hotel,
            detail=(
                f"{nights}晚×{rooms}间×{round(price_per_night, 2)}元[{hotel_source}]"
                if nights > 0
                else "无需住宿"
            ),
        ),
    ]

    return BudgetReport(
        total_estimated=total,
        items=items,
        currency=request.currency,
        within_budget=within,
        over_by=over_by,
        suggested_cuts=pick_cuts(pois, over_by),
    )


def fallback_star_level(options: List[dict], within_budget: bool) -> int:
    """LLM 不可用时的确定性选档规则（与 budget.md 的规则一致）。"""
    if not options:
        return 0
    levels = sorted({int(o["star_level"]) for o in options})
    return levels[0] if not within_budget else levels[len(levels) // 2]


# --------------------------------------------------------------------------- #
# 工具层 / LLM 层
# --------------------------------------------------------------------------- #
async def call_budget_tools(request: UserRequest, star_level: int) -> tuple[float, str]:
    """取某星级酒店均价，返回 (每晚单价, 数据来源)。

    失败时用内置价目表降级，**不抛异常**（住宿费缺了整个预算就失真，不能中断规划）。
    返回值第二项会写进 BudgetItem.detail，评测可据此断言是否走了降级。
    """
    nights = nights_for(request)
    if nights <= 0:
        return 0.0, "no_stay"
    try:
        from ...tools.amap_tools import get_client

        client = get_client()
        async with client:
            info = await client.get_hotel_price(
                request.destination,
                request.start_date.isoformat(),
                star_level,
            )
        return float(info.avg_price_per_night), f"mcp:{star_level}star"
    except Exception as e:  # noqa: BLE001 — 降级而非中断
        base = _FALLBACK_HOTEL_BASE.get(star_level, _FALLBACK_HOTEL_BASE[3])
        mult = _FALLBACK_CITY_MULT.get(request.destination.strip(), 1.0)
        _ = e  # 具体原因由节点写进 errors，这里静默降级
        return round(base * mult, 2), f"fallback:{star_level}star"


async def call_budget_llm(
    request: UserRequest,
    ticket_amount: float,
    transport_amount: float,
    options: List[dict],
    within_budget: bool,
) -> BudgetPlan:
    """调用 LLM 选酒店星级档位（只做取舍，不算钱）。失败转 BudgetError。"""
    try:
        from ...llm.client import structured_model

        system = _load_prompt()
        user = json.dumps(
            {
                "destination": request.destination,
                "days": request.days,
                "travelers": request.travelers,
                "total_budget": request.total_budget,
                "ticket_total": ticket_amount,
                "transport_total": transport_amount,
                "hotel_options": options,
                "nights": nights_for(request),
                "within_budget": within_budget,
            },
            ensure_ascii=False,
        )
        structured = structured_model(BudgetPlan)
        plan = await structured.ainvoke(
            [{"role": "system", "content": system}, {"role": "user", "content": user}]
        )
        if isinstance(plan, dict):
            plan = BudgetPlan(**plan)
        return plan
    except Exception as e:  # noqa: BLE001 — 统一降级，不让异常穿透到图
        raise BudgetError(f"budget LLM 选档/解析失败: {e}") from e


# --------------------------------------------------------------------------- #
# LangGraph 节点
# --------------------------------------------------------------------------- #
async def budget_agent_node(state: GraphState) -> dict:
    """LangGraph 节点：确定性算钱 → 选住宿档位 → 写回 BudgetReport。

    签名符合 LangGraph 规范（state: GraphState -> dict），只返回需要更新的字段。
    失败时写入 errors 并走降级路径，绝不抛异常中断整图。
    """
    req: UserRequest = state["request"]
    pois: List[POI] = state.get("pois") or []
    segments: List[RouteSegment] = state.get("segments") or []

    # 无意义的空输入：不产出预算报告，交由 Supervisor 决定（多半会路由回 poi_agent）
    if not pois:
        step = StepRecord(
            agent="budget_agent",
            action="precheck",
            status="no_pois",
            ts=datetime.now().isoformat(timespec="seconds"),
        )
        return {"trace": [step], "errors": ["budget_agent: 无 POI 可核算预算"]}

    errors: list[str] = []
    travelers = max(1, req.travelers)

    # 1) 与住宿无关的费用先用确定性算术算出来（供 LLM 参考与候选档位比较）
    t_ticket = ticket_total(pois, travelers)
    t_transport = transport_total(segments)
    t_food = food_total(req.days, travelers, pois)
    nights = nights_for(req)
    rooms = rooms_for(travelers)

    # 2) 逐档问价，得到候选（nights=0 时无候选，直接按无住宿处理）
    options: List[dict] = []
    prices: dict[int, float] = {}
    sources: dict[int, str] = {}
    if nights > 0:
        for lv in STAR_LEVELS:
            price, src = await call_budget_tools(req, lv)
            prices[lv] = price
            sources[lv] = src
            options.append({"star_level": lv, "avg_price_per_night": price})
            if src.startswith("fallback"):
                errors.append(f"budget_agent: get_hotel_price({lv}star) 降级估算 {price}")
        # 用最低档先做一次是否超支的预判，喂给 LLM 做取舍依据
        cheapest = min(prices.values())
        pre_total = (
            t_ticket + t_transport + t_food
            + accommodation_total(nights, cheapest, rooms)
        )
        pre_within = pre_total <= req.total_budget
    else:
        pre_within = True

    # 3) LLM 选档；失败或越界则用确定性规则兜底
    chosen_level: int
    note = ""
    try:
        plan = await call_budget_llm(
            req, t_ticket, t_transport, options, pre_within
        )
        valid = {int(o["star_level"]) for o in options}
        if nights == 0:
            chosen_level = 0
        elif plan.star_level in valid:
            chosen_level = plan.star_level
        else:
            # schema 合法但语义越界（如给了不存在的星级）→ 兜底，不算失败
            chosen_level = fallback_star_level(options, pre_within)
            errors.append(
                f"budget_agent: LLM 返回非法星级 {plan.star_level}，已兜底为 {chosen_level}"
            )
        note = plan.note
    except BudgetError as e:
        chosen_level = fallback_star_level(options, pre_within)
        errors.append(str(e))

    # 4) 用最终档位重建报告（这一遍算术是唯一可信数字来源）
    price = prices.get(chosen_level, 0.0)
    src = sources.get(chosen_level, "no_stay")
    report = build_report(req, pois, segments, price, hotel_source=src)

    step = StepRecord(
        agent="budget_agent",
        action=(
            f"total={report.total_estimated}/budget={req.total_budget}"
            f"/within={report.within_budget}/cuts={len(report.suggested_cuts)}"
            + (f"/star={chosen_level}" if nights > 0 else "")
        ),
        status="ok" if report.within_budget else "over_budget",
        ts=datetime.now().isoformat(timespec="seconds"),
    )
    # note 是 LLM 的一句话取舍说明，只作为可观测信息进 trace，不污染 BudgetReport 的费用结构
    if note:
        step.action = f"{step.action}/note={note[:40]}"
    return {"budget_report": report, "trace": [step], "errors": errors}
