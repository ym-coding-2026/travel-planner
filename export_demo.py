"""导出图的 mermaid 源码 + 一次真实运行的 trace，作为交付物。"""
from __future__ import annotations

import asyncio
import sys
import threading
from collections import Counter
from datetime import date
from pathlib import Path

ROOT = Path(r"D:\travel-planner")
sys.path.insert(0, str(ROOT / "src"))

from travel_planner.graph.builder import build_graph, make_async_checkpointer, mermaid
from travel_planner.models.requests import TransportPref, UserRequest

lines = []


def run() -> None:
    lines.append("=" * 70)
    lines.append("一、图的 mermaid 源码")
    lines.append("=" * 70)
    lines.append("")
    lines.append(mermaid())
    lines.append("")

    lines.append("=" * 70)
    lines.append("二、端到端运行 trace：北京 3 天 / 预算 3000")
    lines.append("=" * 70)
    lines.append("")

    from travel_planner.graph.nodes import budget as budget_mod
    from travel_planner.graph.nodes import poi as poi_mod
    from travel_planner.graph.nodes import route as route_mod
    from travel_planner.graph.nodes import supervisor as sup_mod
    from travel_planner.models.routing import NextStep, RoutingDecision

    async def _sup(state):
        # 严格按 prompts/supervisor.md 的规则顺序实现（无 Key 时的确定性替身）
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
        from travel_planner.graph.nodes.poi import PoiSelection

        pool = sorted(candidates, key=lambda p: (p.ticket_price or 0.0, p.poi_id))
        want = max(1, request.max_poi_per_day) * request.days
        return PoiSelection(selected_poi_ids=[p.poi_id for p in pool[:want]], reason="x")

    async def _route_llm(order, segments, request):
        from travel_planner.graph.nodes.route import RouteNarrative

        return RouteNarrative(day_notes=[f"第{i+1}天通勤约 {sum(s.duration_min for s in segments)//max(1,len(order))} 分钟" for i in range(len(order))])

    async def _budget_llm(request, ticket, transport, options, within):
        from travel_planner.graph.nodes.budget import BudgetPlan

        if not options:
            return BudgetPlan(star_level=0, reason_code="no_hotel")
        levels = sorted(int(o["star_level"]) for o in options)
        lv = levels[0] if not within else levels[len(levels) // 2]
        return BudgetPlan(star_level=lv, reason_code="x", note="")

    sup_mod.call_supervisor_llm = _sup
    poi_mod.call_poi_llm = _poi
    route_mod.call_route_llm = _route_llm
    budget_mod.call_budget_llm = _budget_llm

    req = UserRequest(
        destination="北京", start_date=date(2026, 11, 1), days=3, travelers=1,
        total_budget=3000.0, preferences=["历史", "美食"], transport_pref=TransportPref.PUBLIC,
    )
    init = {
        "request": req, "pois": [], "segments": [], "reselect_count": 0,
        "repair_count": 0, "excluded_poi_ids": [], "errors": [], "trace": [],
    }

    async def _main():
        db = ROOT / "_demo.sqlite"
        # 必须清理旧 checkpoint：同一个 thread_id 重入会从断点恢复，
        # 上次调试留下的中间态会污染这次的 trace（踩过这个坑，trace 里凭空多出几轮）。
        db.unlink(missing_ok=True)
        saver = await make_async_checkpointer(str(db))
        try:
            g = build_graph(checkpointer=saver)
            return await g.ainvoke(
                init,
                {"configurable": {"thread_id": "demo-bj"}, "recursion_limit": 80},
            )
        finally:
            await saver.conn.close()

    final = asyncio.run(_main())
    tr = final.get("trace", [])
    lines.append("执行序列（每一步都是结构化 StepRecord，不是散文）：")
    for i, s in enumerate(tr):
        lines.append(f"  {i:02d} [{s.agent:<16}] {s.action:<40} status={s.status}")

    lines.append("")
    lines.append("各节点调用次数：" + str(dict(Counter(s.agent for s in tr))))
    lines.append("")

    it = final["itinerary"]
    rep = final["budget_report"]
    rev = final["review"]
    lines.append("产出行程：" + f"{len(it.days)} 天 / {len(it.ordered_poi_ids)} 个景点")
    for d in it.days:
        seg = "  →  ".join(f"{s.start_time}-{s.end_time}" for s in d.schedule)
        names = []
        idx = {p.poi_id: p.name for p in final["pois"]}
        for s in d.schedule:
            names.append(f"{idx.get(s.poi_id, s.poi_id)}({s.start_time}-{s.end_time})")
        lines.append(f"  第{d.day_index+1}天 {d.date}: " + " → ".join(names))

    lines.append("")
    lines.append(f"预算：估算 {rep.total_estimated} / 预算 {req.total_budget} / "
                 f"超支 {rep.over_by} / within_budget={rep.within_budget}")
    for item in rep.items:
        lines.append(f"  - {item.category.value:<14} {item.amount:>8.2f}  {item.detail}")
    lines.append("")
    lines.append(f"复盘：passed={rev.passed} score={rev.score}")
    for c in rev.checks:
        lines.append(f"  - [{ 'PASS' if c.passed else 'FAIL' }] {c.name:<14} {c.detail}")
    lines.append("")
    lines.append(f"partial={final.get('partial')} status={final.get('status')} "
                 f"reselect_count={final.get('reselect_count')} repair_count={final.get('repair_count')}")


def main():
    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join()
    (ROOT / "GRAPH_AND_DEMO.md").write_text("\n".join(lines), encoding="utf-8")
    print("written")


main()
