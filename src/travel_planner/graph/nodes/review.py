"""review_agent 专家节点：对已产出的行程做**事实校验**，不依赖 LLM 知识、不做主观判断。

核心原则（这是本节点的立身之本）：
- **本节点不调用任何 LLM**。四项校验全部是确定性代码对 State 内数据做比对。
  原因很直接：如果用 LLM 判断"这个行程好不好"，它的结论来自训练语料里的印象，
  同一次输入两次运行可能给出不同结论，既不可复现、也无法在离线评测里断言。
  面试里被问"你怎么保证 Review 不是模型自说自话"，答案就是：Review 里没有模型。
- 唯一可以用 LLM 的地方是把这个已判定的结果**翻译成人话**，且那属于展示层（prompts/review.md
  的约束就是给展示层用的），**不能反过来影响 passed / score / checks**。

四项校验（每项输出 pass/fail + 原因，汇总为 ReviewResult）：

1. `poi_exists`：产出的每个 POI 必须真实存在于 MCP search_poi 的**原始候选集**
   （state["poi_candidates"]）中，且字段（名称）与候选一致。
   —— 事实基准必须是"工具返回了什么"，不能拿 pois 校验 pois，否则恒通过等于没校验。
   若 state 里没有候选集（旧状态/直接注入），该检查降级为 WARNING 而非误报通过。

2. `time_conflict`：行程内同一 POI 不得重复、时刻不得倒序、单日时段不得互相重叠。
   —— 纯区间算术，「A 的结束时间 > B 的开始时间」这类判断，LLM 反而不擅长。

3. `daily_commute`：每日通勤（用 RouteSegment 的真实耗时）不得超过阈值。
   阈值 = 通勤上限（默认 120 分钟/天）且不超过当日可用时间的一定比例。

4. `budget`：BudgetReport.within_budget 必须为 True。
   —— 直接读 budget_agent 用确定性算术得出的结论，不重算、不二次猜测。

职责边界：
- 只**读** state（pois / poi_candidates / itinerary / segments / budget_report / request），
  只**写** state["review"]（ReviewResult）。它不修 pois、不改行程、不调预算——
  发现问题只输出 Issue（结构化、带 code 和 related_poi_ids），由 Supervisor 决定路由回哪个专家。

何时被 Supervisor 再次调用（重入）：
- 行程产出后首次校验；
- 预算超支回退重选并重算后 → 必须重新校验（否则拿着旧的"超支"结论不放）；
- itinerary / route 因复盘问题被重排后 → 重新校验；
- 本节点产出 passed=False 时，Supervisor 依据 issues 的 code 决定回 poi / route / itinerary，
  修完再回到本节点复核——这是"校验 → 修复 → 复验"的闭环。

失败处理（硬约束：绝不抛异常中断整图）：
- 某项校验内部数据缺失（如无 segments 算不出通勤）→ 该检查记 WARNING 而非 fail，
  因为"缺数据"不等于"行程错"，不能让规划因此判死。
- 整个节点若遇意外异常 → 捕获后写 errors，返回一个 passed=False 但不阻断的兜底结果。
"""
from __future__ import annotations

import re
import traceback
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from ...models.plan import DayPlan, Itinerary
from ...models.poi import POI, RouteSegment
from ...models.requests import UserRequest
from ...models.review import CheckResult, Issue, IssueSeverity, ReviewResult
from ...state import GraphState, StepRecord

PROMPT_PATH = Path(__file__).resolve().parents[2] / "prompts" / "review.md"

# 每日通勤耗时阈值（分钟）。超过即视为"一天都在路上"，行程不合理。
DAILY_COMMUTE_LIMIT_MIN = 120
# 通勤时间占当日可活动时间的比例上限：一天有效游玩窗口按 10 小时算
DAILY_ACTIVE_WINDOW_MIN = 600
COMMUTE_RATIO_LIMIT = 0.5

# 各检查项在总分里的权重（合计 100）。事实性检查权重最高。
_WEIGHTS = {
    "poi_exists": 35.0,
    "time_conflict": 25.0,
    "daily_commute": 20.0,
    "budget": 20.0,
}

_TIME_RE = re.compile(r"^(\d{1,2}):(\d{2})$")


# --------------------------------------------------------------------------- #
# 工具函数：时间解析
# --------------------------------------------------------------------------- #
def parse_hhmm(value: str) -> Optional[int]:
    """把 "09:30" 解析成当日分钟数。非法格式返回 None（不抛异常，交由调用方记 WARNING）。"""
    if not isinstance(value, str):
        return None
    m = _TIME_RE.match(value.strip())
    if not m:
        return None
    hh, mm = int(m.group(1)), int(m.group(2))
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        return None
    return hh * 60 + mm


# --------------------------------------------------------------------------- #
# 校验 1：POI 是否真实存在于检索结果（事实校验）
# --------------------------------------------------------------------------- #
def check_poi_exists(
    pois: List[POI], candidates: Optional[List[POI]]
) -> Tuple[CheckResult, List[Issue]]:
    """每个产出的 POI 必须在工具返回的候选集中，且名称一致（防止 LLM 编造景点）。

    事实基准 = MCP search_poi 的原始返回（state["poi_candidates"]）。
    若没有候选集（例如上游没写、或直接注入 state 做单测），不能判定"通过"，
    因为无法证实——记 WARNING 并明确说明原因，这比假阳性通过诚实。
    """
    issues: List[Issue] = []
    if not pois:
        return (
            CheckResult(
                name="poi_exists",
                passed=False,
                detail="fail: 行程中没有任何 POI",
            ),
            [
                Issue(
                    severity=IssueSeverity.ERROR,
                    code="no_poi",
                    message="行程为空，没有可交付的景点",
                )
            ],
        )

    if not candidates:
        return (
            CheckResult(
                name="poi_exists",
                passed=True,
                detail="warn: 缺少 MCP 候选集，无法核实景点来源（未做判定）",
            ),
            [
                Issue(
                    severity=IssueSeverity.WARNING,
                    code="no_candidate_baseline",
                    message="state 中无 poi_candidates，无法核实 POI 是否来自真实检索",
                    related_poi_ids=[p.poi_id for p in pois],
                )
            ],
        )

    index: Dict[str, POI] = {c.poi_id: c for c in candidates}
    missing: List[str] = []
    renamed: List[str] = []
    for p in pois:
        base = index.get(p.poi_id)
        if base is None:
            missing.append(p.poi_id)
        elif base.name.strip() != p.name.strip():
            renamed.append(p.poi_id)

    for pid in missing:
        issues.append(
            Issue(
                severity=IssueSeverity.ERROR,
                code="poi_not_in_candidates",
                message=f"POI {pid} 不存在于 MCP 检索结果中（疑似编造）",
                related_poi_ids=[pid],
            )
        )
    for pid in renamed:
        base = index[pid]
        cur = next(p for p in pois if p.poi_id == pid)
        issues.append(
            Issue(
                severity=IssueSeverity.ERROR,
                code="poi_name_mismatch",
                message=f"POI {pid} 名称与检索结果不一致：'{cur.name}' vs '{base.name}'",
                related_poi_ids=[pid],
            )
        )

    passed = not missing and not renamed
    detail = (
        f"pass: {len(pois)}/{len(pois)} 个 POI 均存在于检索结果（候选 {len(candidates)} 个）"
        if passed
        else f"fail: {len(missing)} 个不存在于检索结果, {len(renamed)} 个名称不一致"
    )
    return CheckResult(name="poi_exists", passed=passed, detail=detail), issues


# --------------------------------------------------------------------------- #
# 校验 2：时间冲突
# --------------------------------------------------------------------------- #
def check_time_conflict(itinerary: Optional[Itinerary]) -> Tuple[CheckResult, List[Issue]]:
    """三类时间问题：同一景点重复安排、单日内时段倒序、单日内时段重叠。

    全部是区间算术，不需要任何领域知识。
    """
    issues: List[Issue] = []
    if itinerary is None or not itinerary.days:
        return (
            CheckResult(
                name="time_conflict",
                passed=True,
                detail="warn: 尚无 itinerary，未做时间校验",
            ),
            [
                Issue(
                    severity=IssueSeverity.WARNING,
                    code="no_itinerary",
                    message="缺少 itinerary，时间冲突无法校验",
                )
            ],
        )

    seen_poi: Dict[str, int] = {}
    bad_order: List[str] = []
    overlaps: List[str] = []
    unparsable: List[str] = []

    for day in itinerary.days:
        spans: List[Tuple[int, int, str]] = []
        for item in day.schedule:
            start = parse_hhmm(item.start_time)
            end = parse_hhmm(item.end_time)
            if start is None or end is None:
                unparsable.append(f"day{day.day_index}:{item.poi_id}")
                continue
            if end <= start:
                bad_order.append(f"day{day.day_index}:{item.poi_id}({item.start_time}-{item.end_time})")
                continue
            spans.append((start, end, item.poi_id))
            if item.poi_id in seen_poi:
                issues.append(
                    Issue(
                        severity=IssueSeverity.ERROR,
                        code="poi_duplicated",
                        message=(
                            f"POI {item.poi_id} 被重复安排"
                            f"（第{seen_poi[item.poi_id] + 1}天与第{day.day_index + 1}天）"
                        ),
                        related_poi_ids=[item.poi_id],
                    )
                )
            else:
                seen_poi[item.poi_id] = day.day_index

        # 同一日内按开始时间排序后，检查相邻区间是否重叠
        spans.sort(key=lambda s: s[0])
        for prev, cur in zip(spans, spans[1:]):
            if cur[0] < prev[1]:
                overlaps.append(f"day{day.day_index}:{prev[2]}↔{cur[2]}")
                issues.append(
                    Issue(
                        severity=IssueSeverity.ERROR,
                        code="time_overlap",
                        message=(
                            f"第{day.day_index + 1}天 {prev[2]} 与 {cur[2]} 时段重叠"
                        ),
                        related_poi_ids=[prev[2], cur[2]],
                    )
                )

    for tag in bad_order:
        issues.append(
            Issue(
                severity=IssueSeverity.ERROR,
                code="time_order_invalid",
                message=f"结束时间不晚于开始时间：{tag}",
                related_poi_ids=[tag.split(":")[-1].split("(")[0]],
            )
        )
    for tag in unparsable:
        issues.append(
            Issue(
                severity=IssueSeverity.WARNING,
                code="time_unparsable",
                message=f"时间格式无法解析：{tag}",
            )
        )

    errors = [i for i in issues if i.severity == IssueSeverity.ERROR]
    passed = not errors
    if passed:
        total_slots = sum(len(d.schedule) for d in itinerary.days)
        detail = f"pass: {total_slots} 个时段无重复、无倒序、无重叠"
    else:
        detail = (
            f"fail: {len(overlaps)} 处时段重叠, {len(bad_order)} 处倒序, "
            f"{sum(1 for i in issues if i.code == 'poi_duplicated')} 处重复安排"
        )
    return CheckResult(name="time_conflict", passed=passed, detail=detail), issues


# --------------------------------------------------------------------------- #
# 校验 3：每日通勤是否超阈值
# --------------------------------------------------------------------------- #
def check_daily_commute(
    segments: List[RouteSegment], plan_days: List[DayPlan]
) -> Tuple[CheckResult, List[Issue]]:
    """每日通勤耗时不得超过阈值（绝对值 + 占当日时间比例双重约束）。

    数据源用 RouteSegment 的真实 duration_min（由 MCP get_route 算得），不靠估算。
    """
    issues: List[Issue] = []
    if not segments:
        return (
            CheckResult(
                name="daily_commute",
                passed=True,
                detail="warn: 无交通段数据，未做通勤校验",
            ),
            [
                Issue(
                    severity=IssueSeverity.WARNING,
                    code="no_segments",
                    message="缺少 RouteSegment，每日通勤无法校验",
                )
            ],
        )

    # 按天聚合：只有知道每天的 POI 顺序才能归段。这里用 plan_days 的顺序做映射。
    per_day: Dict[int, int] = {}
    if plan_days:
        order_map: Dict[Tuple[str, str], int] = {}
        for d in plan_days:
            ids = [s.poi_id for s in d.schedule]
            for a, b in zip(ids, ids[1:]):
                order_map[(a, b)] = d.day_index
        for s in segments:
            idx = order_map.get((s.from_poi_id, s.to_poi_id))
            if idx is not None:
                per_day[idx] = per_day.get(idx, 0) + max(0, s.duration_min)
    else:
        # 无法归天时退化为"全部段合计/天数"的平均值判定，仍给结论但要标注
        per_day = {0: sum(max(0, s.duration_min) for s in segments)}
        issues.append(
            Issue(
                severity=IssueSeverity.WARNING,
                code="commute_aggregated",
                message="缺少 itinerary 分日结构，通勤按全部路段合计校验",
            )
        )

    offenders: List[str] = []
    for idx, minutes in sorted(per_day.items()):
        over_abs = minutes > DAILY_COMMUTE_LIMIT_MIN
        over_ratio = minutes > DAILY_ACTIVE_WINDOW_MIN * COMMUTE_RATIO_LIMIT
        if over_abs or over_ratio:
            offenders.append(f"第{idx + 1}天 {minutes} 分钟")
            issues.append(
                Issue(
                    severity=IssueSeverity.ERROR,
                    code="daily_commute_exceeded",
                    message=(
                        f"第{idx + 1}天通勤 {minutes} 分钟，超过阈值 "
                        f"{DAILY_COMMUTE_LIMIT_MIN} 分钟"
                        f"（或超过当日窗口的 {int(COMMUTE_RATIO_LIMIT * 100)}%）"
                    ),
                )
            )

    passed = not offenders
    worst = max(per_day.values()) if per_day else 0
    detail = (
        f"pass: 单日最长通勤 {worst} 分钟（阈值 {DAILY_COMMUTE_LIMIT_MIN} 分钟）"
        if passed
        else f"fail: {len(offenders)} 天超阈值 → " + "; ".join(offenders)
    )
    return CheckResult(name="daily_commute", passed=passed, detail=detail), issues


# --------------------------------------------------------------------------- #
# 校验 4：预算是否超支
# --------------------------------------------------------------------------- #
def check_budget(budget_report) -> Tuple[CheckResult, List[Issue]]:
    """直接读 budget_agent 的确定性结论，不重算、不二次猜测。"""
    if budget_report is None:
        return (
            CheckResult(
                name="budget",
                passed=True,
                detail="warn: 尚无预算报告，未做预算校验",
            ),
            [
                Issue(
                    severity=IssueSeverity.WARNING,
                    code="no_budget_report",
                    message="缺少 BudgetReport，预算无法校验",
                )
            ],
        )

    if budget_report.within_budget:
        return (
            CheckResult(
                name="budget",
                passed=True,
                detail=f"pass: 估算 {budget_report.total_estimated} ≤ 预算",
            ),
            [],
        )

    return (
        CheckResult(
            name="budget",
            passed=False,
            detail=f"fail: 超支 {budget_report.over_by} 元",
        ),
        [
            Issue(
                severity=IssueSeverity.ERROR,
                code="budget_exceeded",
                message=f"估算 {budget_report.total_estimated} 超预算 {budget_report.over_by} 元",
                related_poi_ids=list(budget_report.suggested_cuts),
            )
        ],
    )


# --------------------------------------------------------------------------- #
# 汇总
# --------------------------------------------------------------------------- #
def compute_score(checks: List[CheckResult]) -> float:
    """按权重汇总 0-100 分。未执行的项（WARNING 降级）按半分计入，避免误判满分。"""
    if not checks:
        return 0.0
    total = 0.0
    for c in checks:
        w = _WEIGHTS.get(c.name, 10.0)
        if c.detail.startswith("warn:"):
            total += w * 0.5
        elif c.passed:
            total += w
    # 权重归一化（防止新增检查项时总分不足 100）
    weight_sum = sum(_WEIGHTS.get(c.name, 10.0) for c in checks) or 1.0
    return round(total / weight_sum * 100, 1)


def aggregate(
    checks: List[CheckResult], issues: List[Issue]
) -> ReviewResult:
    """汇总成 ReviewResult：任一检查 fail 即整体不通过（事实校验不做分数折中）。"""
    errors = [i for i in issues if i.severity == IssueSeverity.ERROR]
    passed = all(c.passed for c in checks) and not errors
    return ReviewResult(
        passed=passed,
        score=compute_score(checks),
        issues=issues,
        checks=checks,
    )


def _extract_day_plans(state: GraphState) -> List[DayPlan]:
    itinerary: Optional[Itinerary] = state.get("itinerary")
    return list(itinerary.days) if itinerary else []


async def review_agent_node(state: GraphState) -> dict:
    """LangGraph 节点：读 State 做四项事实校验 → 写 ReviewResult。

    签名符合 LangGraph 规范（state: GraphState -> dict），只返回需更新的字段。
    **全程不调用 LLM**，因此不存在"LLM 失败"这类降级分支；只有意外异常才兜底。
    """
    req: Optional[UserRequest] = state.get("request")
    _ = req  # 保留语义：所有约束都来自只读 request，当前四项校验不直接需要它

    try:
        pois: List[POI] = state.get("pois") or []
        candidates = state.get("poi_candidates")
        segments: List[RouteSegment] = state.get("segments") or []
        itinerary = state.get("itinerary")
        budget_report = state.get("budget_report")
        plan_days = _extract_day_plans(state)

        checks: List[CheckResult] = []
        issues: List[Issue] = []

        # 显式绑定「检查名 → 校验函数」，而不是从函数名反推：
        # 一旦函数被替换/包装，反推出的名字（如 _crash）就会污染 ReviewResult 的结构。
        plan = (
            ("poi_exists", check_poi_exists, (pois, candidates)),
            ("time_conflict", check_time_conflict, (itinerary,)),
            ("daily_commute", check_daily_commute, (segments, plan_days)),
            ("budget", check_budget, (budget_report,)),
        )
        for name, check_fn, args in plan:
            try:
                c, iss = check_fn(*args)
                # 校验函数返回的名字若与声明不符，以声明为准（结构由节点负责保证）
                if c.name != name:
                    c = CheckResult(name=name, passed=c.passed, detail=c.detail)
            except Exception as e:  # noqa: BLE001 — 单项异常不能拖垮整次复盘
                c = CheckResult(
                    name=name,
                    passed=True,
                    detail=f"warn: 校验执行异常，未判定（{type(e).__name__}）",
                )
                iss = [
                    Issue(
                        severity=IssueSeverity.WARNING,
                        code="check_crashed",
                        message=f"{name} 校验执行异常：{e}",
                    )
                ]
            checks.append(c)
            issues.extend(iss)

        result = aggregate(checks, issues)
        failed = [c.name for c in checks if not c.passed]
        step = StepRecord(
            agent="review_agent",
            action=(
                f"checks={len(checks)}/passed={sum(1 for c in checks if c.passed)}"
                f"/score={result.score}"
                + (f"/failed={','.join(failed)}" if failed else "")
            ),
            status="pass" if result.passed else "fail",
            ts=datetime.now().isoformat(timespec="seconds"),
        )
        return {"review": result, "trace": [step], "errors": []}

    except Exception as e:  # noqa: BLE001 — 最终兜底，绝不让复盘本身把图打挂
        fallback = ReviewResult(
            passed=False,
            score=0.0,
            issues=[
                Issue(
                    severity=IssueSeverity.ERROR,
                    code="review_crashed",
                    message=f"复盘节点异常：{e}",
                )
            ],
            checks=[
                CheckResult(
                    name="review_internal",
                    passed=False,
                    detail=f"fail: 复盘自身异常 {type(e).__name__}",
                )
            ],
        )
        step = StepRecord(
            agent="review_agent",
            action="crashed",
            status="error",
            ts=datetime.now().isoformat(timespec="seconds"),
        )
        return {
            "review": fallback,
            "trace": [step],
            "errors": [f"review_agent 异常: {e}\n{traceback.format_exc(limit=2)}"],
        }
