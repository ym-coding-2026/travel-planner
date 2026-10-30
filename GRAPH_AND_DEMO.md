======================================================================
一、图的 mermaid 源码
======================================================================

---
config:
  flowchart:
    curve: linear
---
graph TD;
	__start__([<p>__start__</p>]):::first
	supervisor(supervisor)
	poi_agent(poi_agent)
	route_agent(route_agent)
	budget_agent(budget_agent)
	itinerary_agent(itinerary_agent)
	review_agent(review_agent)
	fallback(fallback)
	reselect_guard(reselect_guard)
	repair_guard(repair_guard)
	__end__([<p>__end__</p>]):::last
	__start__ --> supervisor;
	budget_agent -.-> reselect_guard;
	budget_agent -.-> supervisor;
	itinerary_agent --> supervisor;
	poi_agent --> supervisor;
	repair_guard -.-> fallback;
	repair_guard -.-> poi_agent;
	repair_guard -.-> route_agent;
	reselect_guard --> poi_agent;
	review_agent -. &nbsp;end&nbsp; .-> __end__;
	review_agent -.-> fallback;
	review_agent -.-> repair_guard;
	route_agent --> supervisor;
	supervisor -. &nbsp;end&nbsp; .-> __end__;
	supervisor -.-> budget_agent;
	supervisor -.-> itinerary_agent;
	supervisor -.-> poi_agent;
	supervisor -.-> review_agent;
	supervisor -.-> route_agent;
	fallback --> __end__;
	classDef default fill:#f2f0ff,line-height:1.2
	classDef first fill-opacity:0
	classDef last fill:#bfb6fc


======================================================================
二、端到端运行 trace：北京 3 天 / 预算 3000
======================================================================

执行序列（每一步都是结构化 StepRecord，不是散文）：
  00 [supervisor      ] route->poi_agent                         status=ok
  01 [poi_agent       ] select->10pois                           status=ok
  02 [supervisor      ] route->route_agent                       status=ok
  03 [route_agent     ] plan->3days/7segs                        status=ok
  04 [supervisor      ] route->budget_agent                      status=ok
  05 [budget_agent    ] total=2717.9/budget=3000.0/within=True/cuts=0/star=4 status=ok
  06 [supervisor      ] route->itinerary_agent                   status=ok
  07 [itinerary_agent ] build->3days/10slots                     status=ok
  08 [supervisor      ] route->review_agent                      status=ok
  09 [review_agent    ] checks=4/passed=4/score=100.0            status=pass

各节点调用次数：{'supervisor': 5, 'poi_agent': 1, 'route_agent': 1, 'budget_agent': 1, 'itinerary_agent': 1, 'review_agent': 1}

产出行程：3 天 / 10 个景点
  第1天 2026-11-01: 八达岭长城(09:00-13:00)
  第2天 2026-11-02: 故宫博物院(09:00-12:00) → 北海公园(13:03-15:03) → 南锣鼓巷(15:07-16:37) → 天安门广场(16:46-17:46) → 天坛公园(17:54-19:54)
  第3天 2026-11-03: 鸟巢(国家体育场)(09:00-11:00) → 798艺术区(11:25-13:55) → 圆明园(14:46-17:16) → 颐和园(17:22-20:22)

预算：估算 2717.9 / 预算 3000.0 / 超支 0.0 / within_budget=True
  - ticket           215.00  10个景点×1人
  - transport         24.90  7段当地交通
  - food             450.00  3天×1人×150元
  - accommodation   2028.00  2晚×1间×1014.0元[mcp:4star]

复盘：passed=True score=100.0
  - [PASS] poi_exists     pass: 10/10 个 POI 均存在于检索结果（候选 10 个）
  - [PASS] time_conflict  pass: 10 个时段无重复、无倒序、无重叠
  - [PASS] daily_commute  pass: 单日最长通勤 82 分钟（阈值 120 分钟）
  - [PASS] budget         pass: 估算 2717.9 ≤ 预算

partial=None status=None reselect_count=0 repair_count=0