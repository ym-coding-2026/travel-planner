"""geo_utils 算法单元测试：聚类 + 每日排序使通勤最小（项目算法亮点）。

全部用确定性的本地 POI 坐标，不依赖 MCP / LLM / 网络。
"""
from travel_planner.geo_utils import (
    cluster_pois_by_day,
    daily_commute_km,
    haversine_km,
    order_within_day,
    plan_days,
    total_commute_km,
)
from travel_planner.models.common import GeoPoint
from travel_planner.models.poi import POI


def _poi(pid: str, lat: float, lng: float) -> POI:
    return POI(
        poi_id=pid, name=pid, category="test",
        location=GeoPoint(lat=lat, lng=lng),
    )


# 三个明显分开的地理簇（每簇内点很近，簇间相距很远）
CLUSTER_A = [("a1", 39.900, 116.400), ("a2", 39.905, 116.405), ("a3", 39.902, 116.402)]
CLUSTER_B = [("b1", 39.990, 116.450), ("b2", 39.995, 116.455), ("b3", 39.992, 116.452)]
CLUSTER_C = [("c1", 39.850, 116.300), ("c2", 39.855, 116.305), ("c3", 39.852, 116.302)]


def _make_pois(order="abc"):
    """按给定簇顺序排列 POI（交错顺序用于对比朴素基线）。"""
    out = []
    for key in order:
        for spec in {"a": CLUSTER_A, "b": CLUSTER_B, "c": CLUSTER_C}[key]:
            out.append(_poi(*spec))
    return out


def test_haversine_basic():
    """haversine 与欧氏近似在小范围内数量级正确。"""
    d = haversine_km(GeoPoint(lat=39.90, lng=116.40), GeoPoint(lat=39.91, lng=116.40))
    assert 0.5 < d < 3.0  # 约 1.1km


def test_cluster_keeps_geographic_groups():
    """KMeans 把地理近的点放同一天（同簇内只有一个前缀）。"""
    pois = _make_pois("abcabcabc")  # a0 b0 c0 a1 b1 c1 a2 b2 c2
    days = cluster_pois_by_day(pois, 3)
    prefixes_per_day = [{p.poi_id[0] for p in d} for d in days if d]
    # 期望 3 个非空天，每天只含一个簇前缀
    assert len(prefixes_per_day) == 3
    assert all(len(s) == 1 for s in prefixes_per_day)


def test_plan_days_minimizes_commute_vs_naive():
    """算法每日通勤 < 朴素交错基线（A0B0C0/A1B1C1/A2B2C2 跨区通勤）。"""
    pois = _make_pois("abcabcabc")
    planned = plan_days(pois, 3)
    naive = [pois[0:3], pois[3:6], pois[6:9]]
    assert total_commute_km(planned) < total_commute_km(naive)


def test_order_within_day_returns_permutation():
    """每日内排序返回输入的一个排列（无遗漏、无重复）。"""
    day = [(_poi(*s)) for s in CLUSTER_A]
    ordered = order_within_day(day)
    assert {p.poi_id for p in ordered} == {p.poi_id for p in day}
    assert len(ordered) == len(day)


def test_order_within_day_single_and_pair():
    assert order_within_day([_poi("x", 1, 1)]) == [_poi("x", 1, 1)]
    pair = [_poi("p", 2, 2), _poi("q", 2.1, 2.1)]
    assert len(order_within_day(pair)) == 2


def test_daily_commute_zero_for_single():
    assert daily_commute_km([_poi("x", 1, 1)]) == 0.0


def test_edge_cases_empty_and_few():
    """空输入与点数不足天数时的边界。"""
    assert cluster_pois_by_day([], 3) == []
    assert plan_days([], 3) == []
    # 2 个点分 3 天：每天至多一个，第三天空
    few = [_poi("a", 39.9, 116.4), _poi("b", 39.99, 116.45)]
    days = plan_days(few, 3)
    assert len(days) == 3
    assert sum(len(d) for d in days) == 2
    assert any(len(d) == 0 for d in days)
