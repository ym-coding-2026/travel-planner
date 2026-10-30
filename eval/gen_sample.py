"""生成「mock 模式真实运行」样例行程，作为 README 里「能跑」的直观证据。

完全不调真实 LLM：把 4 个 LLM 调用点（supervisor/poi/route/budget）替换成
确定性的替身，跑真实的 LangGraph 管线（含条件回退、状态持久化、复核）。
用法：
    set PYTHONPATH=src
    .venv\\Scripts\\python.exe eval\\gen_sample.py
产物：
    eval\\sample_plan.md
"""
from __future__ import annotations

import asyncio
import sys
from collections import Counter
from datetime import date
from pathlib import Path

ROOT = Path(r"D:\travel-planner")
sys.path.insert(0, str(ROOT / "src"))

from travel_planner.graph.builder import build_graph, make_async_checkpointer
from travel_planner.graph.nodes import budget as budget_mod
from travel_planner.graph.nodes import poi as poi_mod
from travel_planner.graph.nodes import route as route_mod
from travel_planner.graph.nodes import supervisor as sup_mod
from travel_planner.graph.nodes.poi import PoiSelection
from travel_planner.graph.nodes.route import RouteNarrative
from travel_planner.graph.nodes.budget import BudgetPlan
from travel_planner.models.requests import TransportPref, UserRequest
from travel_planner.models.routing import NextStep, RoutingDecision


# ---- 确定性替身（无 Key） ----
async def _sup(state):
    if len(state.get("pois") or []) == 0:
        return RoutingDecision(next=NextStep.POI, reason_code="initial")
    review = state.get("review")
    if review is not None and review.passed:
        return RoutingDecision(next=NextStep.END, reason_code="done")
    if not state.get("route_order"):
        return RoutingDecision(next=NextStep.ROUTE, reason_code="need_route")
    if state.get("budget_report") is None:
        return RoutingDecision(next=NextStep.BUDGET, reason_code="need_budget")
    if state.get("itinerary") is None:
        return RoutingDecision(next=NextStep.ITINERARY, reason_code="need_itinerary")
    return RoutingDecision(next=NextStep.REVIEW, reason_code="need_review")


async def _poi(candidates, request, budget_pressure=None):
    pool = sorted(candidates, key=lambda p: (p.ticket_price or 0.0, p.poi_id))
    want = max(1, request.max_poi_per_day) * request.days
    return PoiSelection(selected_poi_ids=[p.poi_id for p in pool[:want]], reason="mock: 按票价升序选前 N 个")


async def _route_llm(order, segments, request):
    # 确定性叙事：用真实 segments 的总通勤时长做近似（mock 模式不调 LLM 生成文案）
    total = sum(s.duration_min for s in segments) if segments else 0
    return RouteNarrative(
        day_notes=[f"第{i+1}天通勤约 {total // max(1, len(order))} 分钟"
                   for i in range(len(order))]
    )


async def _budget_llm(request, ticket, transport, options, within):
    if not options:
        return BudgetPlan(star_level=0, reason_code="no_hotel")
    levels = sorted(int(o["star_level"]) for o in options)
    lv = levels[0] if not within else levels[len(levels) // 2]
    return BudgetPlan(star_level=lv, reason_code="mock", note="mock: 预算内取中档")


sup_mod.call_supervisor_llm = _sup
poi_mod.call_poi_llm = _poi
route_mod.call_route_llm = _route_llm
budget_mod.call_budget_llm = _budget_llm


def render(req, final) -> str:
    it = final["itinerary"]
    rep = final["budget_report"]
    rev = final["review"]
    pois = {p.poi_id: p for p in final["pois"]}

    L = []
    L.append(f"# 行程样例 · {req.destination} {req.days}天 / 预算 ¥{req.total_budget:.0f}")
    L.append("")
    L.append(f"> 生成方式：**mock 模式**（不调真实 LLM，4 个 LLM 调用点替换为确定性替身），"
             f"但跑的是**真实 LangGraph 管线**，含条件回退 / 状态持久化 / 复核。")
    L.append("")
    L.append("## 请求")
    L.append(f"- 目的地：{req.destination}　天数：{req.days}　人数：{req.travelers}")
    L.append(f"- 预算：¥{req.total_budget:.0f} {req.currency}　偏好：{', '.join(req.preferences)}　交通：{req.transport_pref.value}")
    L.append("")
    L.append("## 行程")
    for d in it.days:
        L.append(f"### 第 {d.day_index + 1} 天 · {d.date}")
        if d.schedule:
            L.append("| 时间 | 景点 | 类别 |")
            L.append("| --- | --- | --- |")
            for s in d.schedule:
                p = pois.get(s.poi_id)
                name = p.name if p else s.poi_id
                cat = p.category if p else "-"
                L.append(f"| {s.start_time}-{s.end_time} | {name} | {cat} |")
        else:
            L.append("（当天无景点）")
        if d.segments:
            tot = sum(seg.duration_min for seg in d.segments)
            L.append(f"- 通勤：约 {tot} 分钟")
        L.append("")
    L.append("## 预算复核")
    L.append(f"- 估算总额：¥{rep.total_estimated:.2f}　预算：¥{req.total_budget:.0f}　"
             f"超出：¥{rep.over_by:.2f}　within_budget={rep.within_budget}")
    for item in rep.items:
        L.append(f"  - {item.category.value}: ¥{item.amount:.2f} （{item.detail}）")
    L.append("")
    L.append("## 质量复核（review_agent）")
    L.append(f"- passed={rev.passed}　score={rev.score}")
    for c in rev.checks:
        L.append(f"  - [{'PASS' if c.passed else 'FAIL'}] {c.name} — {c.detail}")
    L.append("")
    L.append(f"> status={final.get('status')}　partial={final.get('partial')}　"
             f"reselect={final.get('reselect_count')}　repair={final.get('repair_count')}")
    return "\n".join(L)


async def run_one(city, days, budget, prefs, transport) -> str:
    req = UserRequest(
        destination=city, start_date=date(2026, 11, 1), days=days, travelers=2,
        total_budget=float(budget), preferences=prefs, transport_pref=transport,
    )
    init = {"request": req, "pois": [], "segments": [], "reselect_count": 0,
            "repair_count": 0, "excluded_poi_ids": [], "errors": [], "trace": []}
    db = ROOT / f"_sample_{city}.sqlite"
    db.unlink(missing_ok=True)
    saver = await make_async_checkpointer(str(db))
    try:
        g = build_graph(checkpointer=saver)
        final = await g.ainvoke(init, {"configurable": {"thread_id": f"sample-{city}"}, "recursion_limit": 80})
    finally:
        await saver.conn.close()
    return render(req, final)


async def main():
    blocks = []
    # 杭州：新增城市，证明 10 城集成在真实 app 里可用
    blocks.append(await run_one("杭州", 3, 3500, ["历史", "美食"], TransportPref.PUBLIC))
    # 西安：新增城市
    blocks.append(await run_one("西安", 2, 2000, ["历史", "小吃"], TransportPref.PUBLIC))
    out = "\n\n".join(blocks)
    (ROOT / "eval" / "sample_plan.md").write_text(out, encoding="utf-8")
    print("written eval/sample_plan.md")


if __name__ == "__main__":
    asyncio.run(main())
