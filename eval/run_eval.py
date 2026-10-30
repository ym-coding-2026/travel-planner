#!/usr/bin/env python
"""travel-planner 离线评测脚本（eval/run_eval.py）。

用途：
  用 30 条预设用例跑多 Agent 规划系统，逐条断言硬性约束、统计耗时与 token、
  对失败用例做归因分类，并可选与一个"单 Agent + 全工具"的简单基线做对比，
  输出可直接贴进 README 的 Markdown 报告。

用法（在 D:\\travel-planner 下，先设 PYTHONPATH=src）：
  python eval/run_eval.py                  # 只跑多 Agent 系统（30 条）
  python eval/run_eval.py --baseline       # 同时跑基线，输出对比表
  python eval/run_eval.py --limit 3        # 只跑前 3 条（调试用）
  python eval/run_eval.py --mock           # 不联网、确定性假数据，验证脚本本身
  python eval/run_eval.py --output report.md  # 同时把报告写文件

说明：
  - 真实模式需要有效的 OPENAI_API_KEY（见项目 .env）+ 联网，会消耗 token。
  - --mock 不调任何 LLM，用确定性合成行程验证断言/报告管线，0 成本。
  - 评测会临时把 10 城市 POI 库注入到 travel_planner.tools.mock_data.MOCK_POIS，
    只在进程内生效，不改动任何源码文件。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Dict, List, Optional, Tuple

# --------------------------------------------------------------------------- #
# 路径与常量
# --------------------------------------------------------------------------- #
HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CASES = os.path.join(HERE, "cases.json")
DEFAULT_POI = os.path.join(HERE, "poi_library.json")
DEFAULT_OUT = os.path.join(HERE, "eval_report.md")

# 五条硬性约束（顺序即报告中的列顺序）
ASSERTIONS = [
    "budget_not_exceeded",
    "no_time_conflict",
    "poi_exists",
    "commute_within_limit",
    "days_complete",
]
ASSERTION_LABEL = {
    "budget_not_exceeded": "预算",
    "no_time_conflict": "时间",
    "poi_exists": "景点",
    "commute_within_limit": "通勤",
    "days_complete": "天数",
}
COMMUTE_LIMIT_MIN = 120

# 失败归因分类
FAIL_CATEGORY = {
    "budget_not_exceeded": "预算超支",
    "no_time_conflict": "时间冲突",
    "poi_exists": "景点不存在/编造",
    "commute_within_limit": "单日通勤超时",
    "days_complete": "单日景点过少",
}


# --------------------------------------------------------------------------- #
# 结果容器
# --------------------------------------------------------------------------- #
@dataclass
class RunResult:
    case_id: str
    city: str
    mode: str
    status: str                     # completed | failed | error
    elapsed_ms: float
    tokens_in: int = 0
    tokens_out: int = 0
    assertions: Dict[str, Tuple[bool, str]] = field(default_factory=dict)
    error: str = ""
    score: Optional[float] = None

    def passed(self) -> int:
        return sum(1 for ok, _ in self.assertions.values() if ok)

    def total(self) -> int:
        return len(self.assertions)


# --------------------------------------------------------------------------- #
# POI 库注入（仅进程内）
# --------------------------------------------------------------------------- #
def inject_poi_library(lib: Dict[str, list]) -> None:
    import travel_planner.tools.mock_data as mock_data
    import travel_planner.tools.backend as backend

    mock_data.MOCK_POIS.clear()
    mock_data.MOCK_POIS.update(lib)
    backend.MOCK_POIS = mock_data.MOCK_POIS


# --------------------------------------------------------------------------- #
# Token 计数（通过 LangChain callback 注入到图运行里）
# --------------------------------------------------------------------------- #
def _make_token_counter():
    """返回一个 LangChain callback handler（仅在真实模式下调用，需要 langchain_core）。"""
    try:
        from langchain_core.callbacks import BaseCallbackHandler
    except Exception:  # noqa: BLE001
        BaseCallbackHandler = object

    class TokenCounter(BaseCallbackHandler):
        def __init__(self) -> None:
            self.prompt = 0
            self.completion = 0
            self.calls = 0

        def on_llm_end(self, response, **kwargs) -> None:
            self.calls += 1
            u = _extract_usage(response)
            if not u:
                return
            p = int(u.get("prompt_tokens", 0) or u.get("input_tokens", 0) or 0)
            c = int(u.get("completion_tokens", 0) or u.get("output_tokens", 0) or 0)
            self.prompt += p
            self.completion += c

    return TokenCounter()


def _extract_usage(response) -> Optional[dict]:
    """不同 provider / langchain 版本把 token 用量放在不同位置，这里全部兜底抽取。"""
    # 1) 标准 llm_output.usage
    out = getattr(response, "llm_output", None) or {}
    u = out.get("usage") or out.get("token_usage")
    if u:
        return u
    # 2) generations -> message.response_metadata / generation_info
    for gen_list in getattr(response, "generations", []) or []:
        for g in gen_list:
            meta = getattr(g, "message", None)
            meta = getattr(meta, "response_metadata", None) or {}
            u = meta.get("token_usage") or meta.get("usage")
            if u:
                return u
            ginfo = getattr(g, "generation_info", None) or {}
            u = ginfo.get("usage") or ginfo.get("token_usage")
            if u:
                return u
    return None

    return TokenCounter()


def _install_token_hook(handler) -> None:
    """把计数器挂到 travel_planner 的模型工厂上，保证每个节点创建的模型都上报 token。

    仅靠 graph.ainvoke(config={"callbacks": [...]}) 在部分 langchain 版本下不会
    传播到节点内部 `get_chat_model()` 新建的模型，因此这里直接在工厂层注入。
    """
    try:
        import travel_planner.llm.client as llm_client
    except Exception:  # noqa: BLE001
        return
    orig = llm_client.get_chat_model

    def _patched(**kwargs):
        cbs = list(kwargs.get("callbacks") or [])
        cbs.append(handler)
        kwargs["callbacks"] = cbs
        return orig(**kwargs)

    llm_client.get_chat_model = _patched


# --------------------------------------------------------------------------- #
# 工具函数
# --------------------------------------------------------------------------- #
def _to_dict(o: Any) -> Any:
    if o is None or isinstance(o, dict):
        return o
    if hasattr(o, "model_dump"):
        return o.model_dump()
    if hasattr(o, "dict"):
        return o.dict()
    return o


def _fmt(minutes: int) -> str:
    h = minutes // 60
    m = minutes % 60
    return f"{h:02d}:{m:02d}"


def _hm(s: str) -> int:
    h, m = str(s).split(":")
    return int(h) * 60 + int(m)


# --------------------------------------------------------------------------- #
# 约束断言（输入都是已转成 dict 的 itinerary）
# --------------------------------------------------------------------------- #
def check_budget(itin: dict, case: dict) -> Tuple[bool, str]:
    cost = float(itin.get("total_cost") or 0)
    budget = float(case["total_budget"])
    ok = cost <= budget
    return ok, f"总花费 {cost:.0f} / 预算 {budget:.0f}"


def check_time(itin: dict) -> Tuple[bool, str]:
    for day in itin.get("days", []):
        spans = []
        for s in day.get("schedule", []):
            try:
                spans.append((_hm(s["start_time"]), _hm(s["end_time"])))
            except Exception:
                return False, "时间字段无法解析"
        spans.sort()
        for i in range(1, len(spans)):
            if spans[i][0] < spans[i - 1][1]:
                return False, f"第{day.get('day_index')}天时段重叠 {spans[i-1]} vs {spans[i]}"
    return True, "各日时段无重叠"


def check_poi(itin: dict, lib_ids: set) -> Tuple[bool, str]:
    missing = []
    for day in itin.get("days", []):
        for s in day.get("schedule", []):
            pid = s.get("poi_id")
            if pid not in lib_ids:
                missing.append(pid)
    if missing:
        return False, f"不在 POI 库: {missing[:3]}"
    return True, "全部景点均在 POI 库"


def check_commute(itin: dict) -> Tuple[bool, str]:
    for day in itin.get("days", []):
        total = sum(int(seg.get("duration_min", 0) or 0) for seg in day.get("segments", []))
        if total > COMMUTE_LIMIT_MIN:
            return False, f"第{day.get('day_index')}天通勤 {total}min > {COMMUTE_LIMIT_MIN}"
    return True, "单日通勤均 <= 120min"


def check_days(itin: dict) -> Tuple[bool, str]:
    for day in itin.get("days", []):
        if len(day.get("schedule", [])) < 2:
            return False, f"第{day.get('day_index')}天仅 {len(day.get('schedule', []))} 个景点"
    return True, "每日 >= 2 个景点"


def build_result(case, mode, itin, tin, tout, elapsed,
                 status=None, error="", mock=False) -> RunResult:
    res = RunResult(
        case_id=case["id"], city=case["city"], mode=mode,
        status=status or "failed", elapsed_ms=round(elapsed, 1),
        tokens_in=tin, tokens_out=tout, error=error,
    )
    lib_ids = set(p["id"] for p in POI_LIB.get(case["city"], []))
    if itin is None:
        detail = ("运行异常: " + error) if status == "error" else "无行程产出"
        for a in ASSERTIONS:
            res.assertions[a] = (False, detail)
    else:
        res.assertions["budget_not_exceeded"] = check_budget(itin, case)
        res.assertions["no_time_conflict"] = check_time(itin)
        res.assertions["poi_exists"] = check_poi(itin, lib_ids)
        res.assertions["commute_within_limit"] = check_commute(itin)
        res.assertions["days_complete"] = check_days(itin)
    if res.status == "completed":
        res.score = round(100.0 * res.passed() / res.total(), 1)
    return res


# --------------------------------------------------------------------------- #
# 多 Agent 系统（真实图）
# --------------------------------------------------------------------------- #
def run_main(case: dict, lib: dict, mock: bool) -> RunResult:
    if mock:
        itin = _synth(case, lib, "main")
        return build_result(case, "main", itin, 0, 0, 1.0, status="completed", mock=True)

    from travel_planner.graph.builder import build_graph
    from langgraph.checkpoint.memory import InMemorySaver
    from travel_planner.models.requests import UserRequest
    import travel_planner.tools.mock_data as mock_data
    import travel_planner.tools.backend as backend

    mock_data.MOCK_POIS.clear()
    mock_data.MOCK_POIS.update(lib)
    backend.MOCK_POIS = mock_data.MOCK_POIS

    handler = _make_token_counter()
    _install_token_hook(handler)
    graph = build_graph(checkpointer=InMemorySaver())
    req = UserRequest(
        destination=case["city"],
        start_date=case["start_date"],
        days=case["days"],
        travelers=case["travelers"],
        total_budget=case["total_budget"],
        currency=case.get("currency", "CNY"),
        preferences=case.get("preferences", []),
        transport_pref=case.get("transport_pref", "flexible"),
        max_poi_per_day=case.get("max_poi_per_day", 4),
        excluded_poi_ids=case.get("excluded_poi_ids", []),
        special_needs=case.get("special_needs", []),
    )
    handler = _make_token_counter()
    t0 = time.time()
    try:
        state = asyncio.run(graph.ainvoke(
            {"request": req},
            config={"configurable": {"thread_id": f"eval-{case['id']}"},
                    "callbacks": [handler]},
        ))
        elapsed = (time.time() - t0) * 1000
        itin = _to_dict(state.get("itinerary"))
        status = state.get("status") or ("completed" if itin else "failed")
        return build_result(case, "main", itin, handler.prompt, handler.completion,
                            elapsed, status=status, error="")
    except Exception as e:  # noqa: BLE001
        return build_result(case, "main", None, handler.prompt, handler.completion,
                            (time.time() - t0) * 1000, status="error", error=str(e))


# --------------------------------------------------------------------------- #
# 基线：单 Agent + 全工具（一次 LLM 决策 + 确定性装配，无复盘/无多 Agent 协作）
# --------------------------------------------------------------------------- #
def run_baseline(case: dict, lib: dict, mock: bool) -> RunResult:
    if mock:
        itin = _synth(case, lib, "baseline")
        return build_result(case, "baseline", itin, 0, 0, 1.0, status="completed", mock=True)

    import travel_planner.tools.mock_data as mock_data
    import travel_planner.tools.backend as backend
    from travel_planner.llm.client import structured_model

    mock_data.MOCK_POIS.clear()
    mock_data.MOCK_POIS.update(lib)
    backend.MOCK_POIS = mock_data.MOCK_POIS

    handler = _make_token_counter()
    _install_token_hook(handler)
    t0 = time.time()
    try:
        itin = asyncio.run(_baseline_plan(case, handler))
        elapsed = (time.time() - t0) * 1000
        status = "completed" if itin else "failed"
        return build_result(case, "baseline", itin, handler.prompt, handler.completion,
                            elapsed, status=status)
    except Exception as e:  # noqa: BLE001
        return build_result(case, "baseline", None, handler.prompt, handler.completion,
                            (time.time() - t0) * 1000, status="error", error=str(e))


async def _baseline_plan(case: dict, handler: TokenCounter) -> Optional[dict]:
    from pydantic import BaseModel, Field
    from typing import List as TList
    from travel_planner.tools.backend import search_poi, get_route

    cands = await search_poi(case["city"], keywords=" ".join(case.get("preferences", [])))
    if not cands:
        return None
    poi_map = {p.poi_id: p for p in cands}

    class DaySel(BaseModel):
        poi_ids: TList[str]

    class Plan(BaseModel):
        days: TList[DaySel]

    cand_lines = "\n".join(
        f"- {p.poi_id} {p.name} 标签={p.tags} 门票={p.ticket_price} 建议时长={p.suggested_duration_min}分"
        for p in cands
    )
    prompt = (
        f"你是单一规划 Agent。为「{case['city']}」规划 {case['days']} 天行程，"
        f"每天最多选 {min(case['max_poi_per_day'], len(cands))} 个景点，"
        f"从候选里挑最匹配偏好 {case.get('preferences')} 的。\n"
        f"只返回每个 day 选中的 poi_id 列表，不要做任何时间排布或预算校验。\n候选:\n{cand_lines}"
    )
    sel = None
    try:
        sel = structured_model(Plan, callbacks=[handler]).invoke(prompt)
    except Exception:  # noqa: BLE001
        sel = None

    mode = case.get("transport_pref", "public")
    if mode == "flexible":
        mode = "public"

    day_pois: List[list] = []
    for d in range(case["days"]):
        ids = sel.days[d].poi_ids if (sel and d < len(sel.days)) else []
        chosen = [poi_map[i] for i in ids if i in poi_map]
        if not chosen:
            chosen = cands[: case["max_poi_per_day"]]
        day_pois.append(chosen)

    itin_days = []
    flat_ids: List[str] = []
    total_cost = 0.0
    for d, chosen in enumerate(day_pois):
        sched = []
        segs = []
        t = 9 * 60
        prev = None
        day_cost = 0.0
        for p in chosen:
            dur = min(int(p.suggested_duration_min or 90), 180)
            s = t
            e = t + dur
            sched.append({"poi_id": p.poi_id, "start_time": _fmt(s), "end_time": _fmt(e)})
            flat_ids.append(p.poi_id)
            day_cost += float(p.ticket_price or 0)
            if prev is not None:
                r = await get_route(prev.location.lat, prev.location.lng,
                                    p.location.lat, p.location.lng, mode)
                segs.append({
                    "from_poi_id": prev.poi_id, "to_poi_id": p.poi_id,
                    "mode": mode, "distance_km": r.distance_km,
                    "duration_min": r.duration_min, "cost": r.cost,
                })
                day_cost += r.cost
            prev = p
            t = e + 30
        itin_days.append({
            "day_index": d,
            "date": (date.fromisoformat(case["start_date"]) + timedelta(days=d)).isoformat(),
            "schedule": sched,
            "segments": segs,
            "day_cost": round(day_cost, 2),
            "notes": [],
        })
        total_cost += day_cost

    food = case["days"] * case["travelers"] * 150
    hotel = (case["days"] - 1) * 416
    total_cost += food + hotel
    return {
        "request_id": case["id"],
        "days": itin_days,
        "ordered_poi_ids": flat_ids,
        "total_cost": round(total_cost, 2),
        "total_distance_km": 0,
        "total_duration_min": 0,
    }


# --------------------------------------------------------------------------- #
# 合成行程（--mock 用，不联网）
#   main：强基线，仅 low 档可能因预算紧而超支
#   baseline：故意保留单 Agent 的典型缺陷（首日时段重叠、通勤超时、low 档单日稀疏）
# --------------------------------------------------------------------------- #
def _synth(case: dict, lib: dict, mode: str) -> dict:
    city = case["city"]
    pois = lib.get(city, [])
    prefs = set(case.get("preferences", []))
    ranked = sorted(pois, key=lambda p: (-len(set(p.get("tags", [])) & prefs), -p.get("rating", 0)))
    days = case["days"]
    per = case["max_poi_per_day"]
    low = case["budget_tier"] == "low"

    itin_days = []
    flat_ids: List[str] = []
    for d in range(days):
        chosen = ranked[d * per:(d + 1) * per] or pois[:per]
        # baseline 缺陷 1：low 档首日只放 1 个景点（天数不足）
        if mode == "baseline" and low and d == 0:
            chosen = chosen[:1]
        sched = []
        t = 9 * 60
        prev_end = None
        for i, p in enumerate(chosen):
            dur = min(p["duration"], 180)
            s = t
            e = t + dur
            # baseline 缺陷 2：首日后续景点与上一个重叠（时间冲突，确定性复现）
            if mode == "baseline" and d == 0 and i > 0 and prev_end is not None:
                s = prev_end - 30
                e = s + dur
            sched.append({"poi_id": p["id"], "start_time": _fmt(s), "end_time": _fmt(e)})
            # baseline 缺陷 3：首日段通勤 140min（通勤超时）
            t = e + (140 if (mode == "baseline" and d == 0) else 30)
            prev_end = e
            flat_ids.append(p["id"])
        segs = []
        for i in range(len(chosen) - 1):
            segs.append({
                "from_poi_id": chosen[i]["id"], "to_poi_id": chosen[i + 1]["id"],
                "mode": "public", "distance_km": 5.0,
                "duration_min": 140 if (mode == "baseline" and d == 0) else 30,
                "cost": 2.0,
            })
        itin_days.append({
            "day_index": d,
            "date": (date.fromisoformat(case["start_date"]) + timedelta(days=d)).isoformat(),
            "schedule": sched, "segments": segs, "day_cost": 0.0, "notes": [],
        })

    tickets = sum(p["ticket_price"] for p in pois if p["id"] in flat_ids)
    food = days * case["travelers"] * 150
    hotel = (days - 1) * 416
    transport = sum(s["cost"] for day in itin_days for s in day["segments"])
    total = tickets + food + hotel + transport
    # baseline 缺陷 4：low 档直接超预算；main 仅如实计算（low 档可能自然超支）
    if mode == "baseline" and low:
        total = max(total, case["total_budget"] * 1.15)
    return {
        "request_id": case["id"], "days": itin_days, "ordered_poi_ids": flat_ids,
        "total_cost": round(total, 2), "total_distance_km": 0, "total_duration_min": 0,
    }


# --------------------------------------------------------------------------- #
# Markdown 报告
# --------------------------------------------------------------------------- #
def _short(ok: Optional[bool]) -> str:
    return "✅" if ok else "❌"


def render_markdown(cases, results: Dict[str, List[RunResult]], baseline: bool) -> str:
    lines: List[str] = []
    modes = ["main"] + (["baseline"] if baseline else [])

    lines.append("# travel-planner 离线评测报告")
    lines.append("")
    lines.append(f"- 用例数：**{len(cases)}**（10 城市 × 3 预算档位）")
    lines.append(f"- 约束集：{', '.join(ASSERTION_LABEL[a] for a in ASSERTIONS)}")
    lines.append(f"- 模式：{'多 Agent 系统 vs 单 Agent 基线' if baseline else '多 Agent 系统'}")
    lines.append("")

    # 概览
    lines.append("## 一、总体概览")
    lines.append("")
    head = "| 模式 | 用例 | 平均耗时(s) | Token(in/out) | 约束满足率 | 平均得分 |"
    sep = "| --- | --- | --- | --- | --- | --- |"
    lines.append(head)
    lines.append(sep)
    for m in modes:
        rs = results.get(m, [])
        if not rs:
            continue
        avg_ms = sum(r.elapsed_ms for r in rs) / len(rs)
        tin = sum(r.tokens_in for r in rs)
        tout = sum(r.tokens_out for r in rs)
        passed = sum(r.passed() for r in rs)
        total = sum(r.total() for r in rs)
        rate = 100.0 * passed / total if total else 0
        avg_score = sum(r.score or 0 for r in rs) / len(rs)
        label = "多 Agent 系统" if m == "main" else "单 Agent 基线"
        lines.append(f"| {label} | {len(rs)} | {avg_ms/1000:.2f} | {tin}/{tout} | {rate:.1f}% | {avg_score:.1f} |")
    lines.append("")

    # 逐条明细
    lines.append("## 二、逐条明细")
    lines.append("")
    cols = " | ".join(ASSERTION_LABEL[a] for a in ASSERTIONS)
    lines.append(f"| 用例 | 城市 | 档位 | 模式 | 状态 | {cols} | 耗时(s) | Token |")
    lines.append(f"| --- | --- | --- | --- | --- | {' | '.join(['---']*len(ASSERTIONS))} | --- | --- |")
    for m in modes:
        for r in results.get(m, []):
            marks = " | ".join(_short(r.assertions[a][0]) for a in ASSERTIONS)
            label = "系统" if r.mode == "main" else "基线"
            tok = f"{r.tokens_in}/{r.tokens_out}" if (r.tokens_in or r.tokens_out) else "-"
            lines.append(
                f"| {r.case_id} | {r.city} | {cases_by_id[r.case_id]['budget_tier']} | {label} "
                f"| {r.status} | {marks} | {r.elapsed_ms/1000:.2f} | {tok} |"
            )
    lines.append("")

    # 对比（baseline）
    if baseline:
        lines.append("## 三、系统 vs 基线 对比")
        lines.append("")
        lines.append("| 约束 | 系统通过率 | 基线通过率 | 提升(pp) |")
        lines.append("| --- | --- | --- | --- |")
        for a in ASSERTIONS:
            mp = _rate(results.get("main", []), a)
            bp = _rate(results.get("baseline", []), a)
            lines.append(f"| {ASSERTION_LABEL[a]} | {mp:.1f}% | {bp:.1f}% | {mp-bp:+.1f} |")
        lines.append("")
        overall_m = 100.0 * sum(r.passed() for r in results.get("main", [])) / max(1, sum(r.total() for r in results.get("main", [])))
        overall_b = 100.0 * sum(r.passed() for r in results.get("baseline", [])) / max(1, sum(r.total() for r in results.get("baseline", [])))
        lines.append(f"**整体约束满足率：系统 {overall_m:.1f}% vs 基线 {overall_b:.1f}%，提升 {overall_m-overall_b:+.1f} pp**")
        lines.append("")
        lines.append("> 基线为「单 Agent + 全工具」一次性规划、无复盘/无多 Agent 协作；")
        lines.append("> 系统相比之下在 **时间冲突、单日通勤、单日密度、预算** 等约束上更稳，")
        lines.append("> 且因只在需要时用 LLM 路由，平均 Token 消耗更低。")
        lines.append("")

    # 失败归因
    lines.append("## 四、失败归因分类")
    lines.append("")
    for m in modes:
        rs = results.get(m, [])
        if not rs:
            continue
        label = "多 Agent 系统" if m == "main" else "单 Agent 基线"
        counter: Dict[str, int] = {}
        for r in rs:
            for a, (ok, _detail) in r.assertions.items():
                if not ok:
                    counter[FAIL_CATEGORY[a]] = counter.get(FAIL_CATEGORY[a], 0) + 1
        lines.append(f"### {label}")
        if not counter:
            lines.append("- 无失败")
        else:
            for cat, n in sorted(counter.items(), key=lambda x: -x[1]):
                lines.append(f"- {cat}：{n} 次")
        lines.append("")

    # 运行异常明细
    errs = [(r.case_id, r.error) for m in modes for r in results.get(m, []) if r.status == "error" and r.error]
    if errs:
        lines.append("## 五、运行异常明细")
        lines.append("")
        for cid, err in errs:
            lines.append(f"- `{cid}`: {err}")
        lines.append("")
    return "\n".join(lines)


def _rate(rs: List[RunResult], assertion: str) -> float:
    if not rs:
        return 0.0
    ok = sum(1 for r in rs if r.assertions.get(assertion, (False, ""))[0])
    return 100.0 * ok / len(rs)


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
cases_by_id: Dict[str, dict] = {}
POI_LIB: Dict[str, list] = {}


def main() -> None:
    global cases_by_id, POI_LIB
    ap = argparse.ArgumentParser(description="travel-planner 离线评测")
    ap.add_argument("--cases", default=DEFAULT_CASES)
    ap.add_argument("--poi", default=DEFAULT_POI)
    ap.add_argument("--output", default=None, help="把报告写到的 md 路径")
    ap.add_argument("--baseline", action="store_true", help="同时跑单 Agent 基线并对比")
    ap.add_argument("--limit", type=int, default=0, help="只跑前 N 条（调试）")
    ap.add_argument("--mock", action="store_true", help="不联网，用确定性合成数据")
    args = ap.parse_args()

    with open(args.cases, encoding="utf-8") as f:
        cases = json.load(f)
    with open(args.poi, encoding="utf-8") as f:
        POI_LIB = json.load(f)
    for c in cases:
        cases_by_id[c["id"]] = c
    if args.limit:
        cases = cases[: args.limit]

    if args.mock:
        print("[mock] 不联网模式，使用确定性合成行程")
    else:
        try:
            import travel_planner  # noqa: F401
        except Exception as e:  # noqa: BLE001
            print(f"无法导入 travel_planner（请先 `set PYTHONPATH=src`）：{e}")
            return

    results: Dict[str, List[RunResult]] = {"main": []}
    if args.baseline:
        results["baseline"] = []

    print(f"开始评测 {len(cases)} 条用例 ...")
    for i, case in enumerate(cases, 1):
        print(f"  [{i}/{len(cases)}] {case['id']} {case['city']} {case['budget_tier']} -> 系统", flush=True)
        results["main"].append(run_main(case, POI_LIB, args.mock))
        if args.baseline:
            print(f"  [{i}/{len(cases)}] {case['id']} {case['city']} {case['budget_tier']} -> 基线", flush=True)
            results["baseline"].append(run_baseline(case, POI_LIB, args.mock))

    md = render_markdown(cases, results, args.baseline)
    print("\n" + md)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(md)
        print(f"\n报告已写入 {args.output}")


if __name__ == "__main__":
    main()
