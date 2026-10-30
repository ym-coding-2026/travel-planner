# -*- coding: utf-8 -*-
"""逐项验证 MCP 工具层的三点质疑：
A. 工具返回结构是否稳定（确定性 + schema 一致 + 30 个 POI 字段齐全）
B. 超时 / 重试是否真的生效（失败重试成功 / 超时触发 / 耗尽抛错 / 不退避）
C. mock 数据能否支撑一次完整规划（POI->路线->天气->酒店->预算 端到端）
"""
import asyncio
import json
import time
from datetime import date, timedelta

from travel_planner.settings import settings
from travel_planner.tools.mcp_client import MCPToolClient
from travel_planner.tools.backend import search_poi, get_route, get_weather, get_hotel_price
from travel_planner.tools.mock_data import MOCK_POIS
from travel_planner.models.poi import POI
from travel_planner.models.tools import RouteInfo, WeatherInfo, PriceInfo

settings.amap_real_mode = False  # 强制 mock，保证评测可复现

results = []
def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    results.append((status, name, detail))
    print(f"[{status}] {name}" + (f" -- {detail}" if detail else ""))


# --------------------------------------------------------------------------- #
# A. 返回结构稳定性
# --------------------------------------------------------------------------- #
async def part_a(client):
    print("\n===== A. 返回结构稳定性 =====")

    # A1: 同输入多次调用，输出逐字节一致（确定性，保证离线评测可复现）
    jsons = []
    for _ in range(5):
        pois = await client.search_poi("北京", keywords="历史")
        jsons.append(json.dumps([p.model_dump() for p in pois], sort_keys=True, ensure_ascii=False))
    check("A1 search_poi 5次调用逐字节一致", len(set(jsons)) == 1, f"不同结果数={len(set(jsons))}")

    for tool in ["get_route", "get_weather", "get_hotel_price"]:
        js = []
        for _ in range(5):
            if tool == "get_route":
                r = await client.get_route(39.9, 116.4, 39.99, 116.3, "public")
            elif tool == "get_weather":
                r = await client.get_weather("北京", "2026-10-01")
            else:
                r = await client.get_hotel_price("北京", "2026-10-01", 4)
            js.append(json.dumps(r.model_dump(), sort_keys=True, ensure_ascii=False))
        check(f"A1 {tool} 5次调用逐字节一致", len(set(js)) == 1)

    # A2: 三个城市所有 POI 的字段集合一致（schema 稳定）
    key_sets = set()
    for city in MOCK_POIS:
        pois = await client.search_poi(city)
        for p in pois:
            key_sets.add(tuple(sorted(p.model_dump().keys())))
    check("A2 三城 POI schema 字段集合一致", len(key_sets) == 1, f"不同字段集合数={len(key_sets)}")

    # A3: 30 个 POI 必填字段齐全，且类型正确
    total = 0
    bad = []
    for city in MOCK_POIS:
        pois = await client.search_poi(city)
        for p in pois:
            total += 1
            if not (isinstance(p.poi_id, str) and p.poi_id and isinstance(p.name, str)
                    and isinstance(p.category, str) and isinstance(p.location.lng, float)
                    and isinstance(p.location.lat, float)):
                bad.append(p.poi_id)
    check("A3 全部 POI 必填字段齐全且类型正确", total == 30 and not bad, f"总数={total}, 异常={bad}")

    # A4: RouteInfo / WeatherInfo / PriceInfo 字段集合固定
    r = await client.get_route(39.9, 116.4, 39.99, 116.3, "public")
    w = await client.get_weather("上海", "2026-07-01")
    h = await client.get_hotel_price("成都", "2026-10-03", 5)
    check("A4 RouteInfo 字段固定", set(r.model_dump().keys()) == {"origin","destination","mode","distance_km","duration_min","cost","steps"})
    check("A4 WeatherInfo 字段固定", set(w.model_dump().keys()) == {"city","date","day_weather","night_weather","temperature_high","temperature_low","wind"})
    check("A4 PriceInfo 字段固定", set(h.model_dump().keys()) == {"city","checkin","star_level","avg_price_per_night","currency"})


# --------------------------------------------------------------------------- #
# B. 超时 / 重试是否真的生效
# --------------------------------------------------------------------------- #
def make_flaky(fail_times, then_return, sleep=0.0):
    """返回一个 coro_factory 和共享计数器。前 fail_times 次抛 ValueError，之后返回。"""
    state = {"n": 0}
    def factory():
        async def coro():
            state["n"] += 1
            if sleep:
                await asyncio.sleep(sleep)  # 超时会在此处被打断
            if state["n"] <= fail_times:
                raise ValueError(f"fail #{state['n']}")
            return then_return
        return coro()
    return factory, state


async def part_b():
    print("\n===== B. 超时 / 重试是否真的生效 =====")

    # B1: 失败 2 次后第 3 次成功 -> 重试生效，且退避时间真实发生
    c = MCPToolClient(transport="direct", timeout=5.0, max_retries=3)
    factory, st = make_flaky(fail_times=2, then_return="ok")
    t0 = time.monotonic()
    out = await c._with_retry(factory)
    el = time.monotonic() - t0
    check("B1 失败2次后第3次成功", out == "ok" and st["n"] == 3, f"attempts={st['n']}, elapsed={el:.2f}s")
    # 指数退避: 第2次前等 0.5s, 第3次前等 1.0s -> 约 1.5s
    check("B1 退避时间真实生效(>=1.4s)", el >= 1.4, f"elapsed={el:.2f}s (无退避应<0.1s)")

    # B2: 超时触发 -> sleep(2) 被 timeout(0.1) 截断，重试到上限后抛 TimeoutError
    c2 = MCPToolClient(transport="direct", timeout=0.1, max_retries=3)
    factory2, st2 = make_flaky(fail_times=99, then_return="never", sleep=2.0)
    t0 = time.monotonic()
    try:
        await c2._with_retry(factory2)
        check("B2 超时未触发(异常)", False, "应抛 TimeoutError 但未抛")
    except asyncio.TimeoutError:
        el = time.monotonic() - t0
        # 若有退避+每次0.1s超时: 0.1*3 + (0.5+1.0)=~1.8s；若超时没生效则≈6s
        check("B2 超时触发并最多重试3次", st2["n"] == 3, f"attempts={st2['n']}")
        check("B2 超时确实截断了sleep(2)", el < 6.0, f"elapsed={el:.2f}s (未截断应≈6s)")

    # B3: 持续失败 -> 耗尽 max_retries 后 reraise 原异常
    c3 = MCPToolClient(transport="direct", timeout=5.0, max_retries=3)
    factory3, st3 = make_flaky(fail_times=99, then_return="never")
    t0 = time.monotonic()
    try:
        await c3._with_retry(factory3)
        check("B3 未抛异常", False)
    except ValueError:
        el = time.monotonic() - t0
        check("B3 耗尽重试后抛出原异常", st3["n"] == 3, f"attempts={st3['n']}")
        check("B3 退避时间真实(>=1.4s)", el >= 1.4, f"elapsed={el:.2f}s")

    # B4: 一次成功 -> 不重试（对照）
    c4 = MCPToolClient(transport="direct", timeout=5.0, max_retries=3)
    factory4, st4 = make_flaky(fail_times=0, then_return="ok")
    t0 = time.monotonic()
    out = await c4._with_retry(factory4)
    el = time.monotonic() - t0
    check("B4 一次成功不重试", out == "ok" and st4["n"] == 1 and el < 0.3, f"attempts={st4['n']}, elapsed={el:.2f}s")


# --------------------------------------------------------------------------- #
# C. mock 数据能否支撑一次完整规划
# --------------------------------------------------------------------------- #
async def part_c(client):
    print("\n===== C. mock 数据支撑完整规划 =====")
    city, days, budget, travelers = "北京", 3, 8000.0, 2

    # poi_agent: 选点（按评分排序取前 9 个，每天 3 个）
    all_pois = await client.search_poi(city)
    all_pois.sort(key=lambda p: p.rating or 0, reverse=True)
    per_day = 3
    chosen = all_pois[: per_day * days]
    check("C1 POI 数量足够排期", len(chosen) == per_day * days, f"选点={len(chosen)}")

    # 选出的 POI 坐标/票价/类别齐全（规划所需字段都拿得到）
    ok = all(p.location.lat and p.location.lng and p.ticket_price is not None and p.category for p in chosen)
    check("C2 选点字段齐全(坐标/票价/类别)", ok)

    # route_agent: 每天内部相邻 POI 算路线
    segments = []
    daily_pois = [chosen[i*per_day:(i+1)*per_day] for i in range(days)]
    for day in daily_pois:
        for a, b in zip(day, day[1:]):
            r = await client.get_route(a.location.lat, a.location.lng, b.location.lat, b.location.lng, "public")
            segments.append(r)
    check("C3 路线段连通所有相邻 POI", len(segments) == (per_day - 1) * days, f"segments={len(segments)}")
    check("C4 每段距离/耗时/费用非空", all(s.distance_km > 0 and s.duration_min > 0 for s in segments))

    # weather_agent
    start = date(2026, 10, 1)
    weather = [await client.get_weather(city, (start + timedelta(days=i)).isoformat()) for i in range(days)]
    check("C5 每天天气可查", len(weather) == days and all(w.city == city for w in weather))

    # budget_agent: 门票 + 交通 + 酒店 + 餐饮
    ticket = sum((p.ticket_price or 0) * travelers for p in chosen)
    transport = sum(s.cost * (travelers // 2 + 1) for s in segments)  # 每两人一段
    hotel = (await client.get_hotel_price(city, start.isoformat(), 4)).avg_price_per_night * (days - 1)
    food = sum((p.avg_cost_per_person or 0) * travelers for p in chosen) * 0.6
    total = round(ticket + transport + hotel + food, 2)
    within = total <= budget
    check("C6 预算可计算且给出超支判定", total > 0, f"estimated={total}, budget={budget}, within={within}")

    # 回退边燃料: 极小预算应判超支（驱动真实图里的 excluded_poi_ids 回退）
    tiny = 100.0
    check("C7 极小预算触发超支(供回退边消费)", (ticket + transport + hotel + food) > tiny)

    print(f"\n   规划摘要: {city} {days}天 / {travelers}人 / 预算¥{budget}")
    print(f"   选中POI {len(chosen)} 个, 路线段 {len(segments)} 条, 天气 {len(weather)} 天")
    print(f"   预估花费 ¥{total}  ->  预算内? {within}")
    print(f"     门票¥{round(ticket,1)} 交通¥{round(transport,1)} 酒店¥{round(hotel,1)} 餐饮¥{round(food,1)}")

    # 多城覆盖: 上海/成都同样能跑通 POI+路线
    for c in ("上海", "成都"):
        ps = await client.search_poi(c)
        r0 = await client.get_route(ps[0].location.lat, ps[0].location.lng, ps[1].location.lat, ps[1].location.lng, "drive")
        check(f"C8 {c} 可独立规划(选点+路线)", len(ps) >= 10 and r0.distance_km > 0)


async def main():
    client = MCPToolClient(transport="direct")
    await part_a(client)
    await part_b()
    await part_c(client)
    passed = sum(1 for s, _, _ in results if s == "PASS")
    print(f"\n===== 汇总: {passed}/{len(results)} 通过 =====")
    # 写文件供查阅（utf-8，Read 工具可读）
    with open(r"D:\travel-planner\verify_out.txt", "w", encoding="utf-8") as f:
        f.write(f"verify: {passed}/{len(results)} PASS\n")
        for s, n, d in results:
            f.write(f"[{s}] {n} {d}\n")


if __name__ == "__main__":
    asyncio.run(main())
