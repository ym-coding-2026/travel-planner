"""geo_utils 的回归测试：锁住三个真实 bug，防止改动时回退。

三个 bug 都是「单测没覆盖到真实数据形状」导致的：
1. GeoPoint 被误用为簇中心类型（语义错位 + 越界风险）；
2. KMeans 可能产出空簇，导致某天完全没景点；
3. 空簇传到 order_within_day 时算质心直接 ZeroDivisionError。
"""
from __future__ import annotations

from travel_planner.geo_utils import (
    cluster_pois_by_day,
    daily_commute_km,
    order_within_day,
    plan_days,
    total_commute_km,
)
from travel_planner.models.common import GeoPoint
from travel_planner.models.poi import POI


def _poi(pid: str, lat: float, lng: float) -> POI:
    return POI(
        poi_id=pid,
        name=f"POI-{pid}",
        category="attraction",
        location=GeoPoint(lat=lat, lng=lng),
    )


def test_empty_cluster_does_not_crash() -> None:
    """bug 3：空列表进 order_within_day 必须安全返回空，而不是 ZeroDivisionError。"""
    assert order_within_day([]) == []


def test_plan_days_handles_fewer_pois_than_days() -> None:
    """点数少于天数时，多出来的天是空列表，且 plan_days 不崩。"""
    pois = [_poi("a", 39.90, 116.40), _poi("b", 39.91, 116.41)]
    days = plan_days(pois, 5)
    assert len(days) == 5
    assert sum(len(d) for d in days) == 2
    assert all(len(d) <= 1 for d in days)


def test_clusters_are_never_empty_when_enough_pois() -> None:
    """bug 2：POI 数量 >= 天数时，不允许出现空簇（否则那天没景点可玩）。"""
    # 构造两个明显分离的地理簇，中间插入大量同点重复，逼 KMeans 可能产出空簇
    north = [_poi(f"n{i}", 40.00 + i * 0.001, 116.40) for i in range(6)]
    south = [_poi(f"s{i}", 31.20 + i * 0.001, 121.40) for i in range(2)]
    clusters = cluster_pois_by_day(north + south, n_days=3)
    assert len(clusters) == 3
    assert all(len(c) > 0 for c in clusters), [len(c) for c in clusters]
    # 所有点都必须被分配到且只分配一次
    ids = [p.poi_id for c in clusters for p in c]
    assert len(ids) == len(set(ids)) == 8


def test_clustering_beats_naive_interleaving() -> None:
    """算法亮点：地理聚类后的总通勤距离必须显著小于"按原顺序交错"的朴素方案。"""
    # 北京核心区 6 个点 + 远郊 3 个点，原顺序刻意交错
    pois = [
        _poi("a1", 39.9087, 116.3975),   # 天安门
        _poi("f1", 40.4319, 116.5704),   # 慕田峪（远）
        _poi("a2", 39.9163, 116.3972),   # 故宫
        _poi("f2", 40.3620, 116.0200),   # 八达岭（远）
        _poi("a3", 39.9250, 116.4100),   # 景山
        _poi("f3", 40.5200, 116.2400),   # 龙庆峡（远）
        _poi("a4", 39.8800, 116.4100),   # 天坛
        _poi("a5", 39.9420, 116.4050),   # 什刹海
        _poi("a6", 39.9000, 116.3700),   # 前门
    ]
    clustered = plan_days(pois, n_days=3)
    naive = [pois[i::3] for i in range(3)]

    optimized_km = total_commute_km(clustered)
    naive_km = total_commute_km(naive)
    assert optimized_km < naive_km, f"聚类应更优: {optimized_km} vs {naive_km}"
    # 至少省 30%（真实数据上通常远优于这个阈值，这里只锁住"确实有显著优化"）
    assert optimized_km < naive_km * 0.7, f"优化幅度不足: {optimized_km} vs {naive_km}"


def test_order_within_day_is_locally_optimal() -> None:
    """贪心最近邻的性质：排序结果中任意一步的下一站，都是剩余点里最近的那个。"""
    from travel_planner.geo_utils import haversine_km

    pois = [
        _poi("p1", 39.900, 116.400),
        _poi("p2", 39.905, 116.405),
        _poi("p3", 39.950, 116.500),
        _poi("p4", 39.901, 116.399),
        _poi("p5", 39.910, 116.412),
    ]
    ordered = order_within_day(pois)
    remaining = list(ordered)
    while len(remaining) > 1:
        cur = remaining[0]
        nxt = remaining[1]
        best = min(remaining[1:], key=lambda p: haversine_km(cur.location, p.location))
        assert nxt.poi_id == best.poi_id, "下一站不是剩余最近点，贪心性质被破坏"
        remaining.pop(0)
    assert sorted(p.poi_id for p in ordered) == sorted(p.poi_id for p in pois)


def test_no_overnight_jump_within_a_day() -> None:
    """同一天内不应出现"从城北跳到城南再跳回来"的超长单段（聚类失效信号）。"""
    north = [_poi(f"n{i}", 40.00 + i * 0.002, 116.40) for i in range(5)]
    south = [_poi(f"s{i}", 31.20 + i * 0.002, 121.40) for i in range(5)]
    days = plan_days(north + south, n_days=2)
    for day in days:
        # 每段都不该跨城（北京到上海约 1000+ km）
        assert daily_commute_km(day) < 100, "同一天跨越了两座城市，聚类失败"


def _beijing_like() -> list[POI]:
    """北京真实坐标形状：1 个远郊（八达岭）+ 6 个市中心 + 3 个城北。

    这是从 mock 数据实测出来的失衡样本：KMeans 会把八达岭单独分一簇，
    剩下 6 个市中心点全挤在一天。均衡逻辑就是为这个输入写的，必须锁住。
    """
    return [
        _poi("bj-04", 40.3620, 116.0200),  # 八达岭长城（远郊）
        _poi("bj-01", 39.9163, 116.3972),  # 故宫
        _poi("bj-02", 39.9056, 116.3975),  # 天安门
        _poi("bj-05", 39.8822, 116.4066),  # 天坛
        _poi("bj-06", 39.9250, 116.3900),  # 北海
        _poi("bj-08", 39.9350, 116.4030),  # 南锣鼓巷
        _poi("bj-10", 39.9840, 116.4950),  # 798
        _poi("bj-03", 40.0000, 116.2750),  # 颐和园
        _poi("bj-07", 40.0080, 116.2980),  # 圆明园
        _poi("bj-09", 39.9930, 116.3960),  # 鸟巢
    ]


def test_load_balance_improves_imbalance() -> None:
    """负载均衡：10 个点分 3 天，最大簇从 6 降到 5（8 步改善不能退回 6）。

    注意 cap=4 是**尽力目标而非硬保证**：把第 5 个市区点再挪出去，唯一的空位是
    八达岭那天，代价 +57 km，被代价门槛否决。这个"没做到 cap"是刻意的取舍，
    见 `_balance_load` 注释。这里只锁住"确实降了一步"这个可复现的事实。
    """
    clusters = cluster_pois_by_day(_beijing_like(), n_days=3)
    sizes = sorted((len(c) for c in clusters), reverse=True)
    assert sum(sizes) == 10
    assert sizes[0] <= 5, f"失衡没有改善，最大簇还是 {sizes[0]} 个: {sizes}"
    assert sizes[0] - sizes[-1] <= 4, f"簇规模差距过大: {sizes}"


def test_load_balance_does_not_put_city_poi_into_remote_day() -> None:
    """均衡的取舍：绝不为了凑点数把市区点塞进八达岭那天（那会多背 100+ km）。"""
    clusters = cluster_pois_by_day(_beijing_like(), n_days=3)
    remote_day = next(c for c in clusters if any(p.poi_id == "bj-04" for p in c))
    assert len(remote_day) == 1, f"八达岭那天被塞了别的点: {[p.poi_id for p in remote_day]}"


def test_load_balance_keeps_total_commute_low() -> None:
    """均衡后总通勤仍在合理范围：远郊点单独成行时不该产生天文数字。"""
    days = plan_days(_beijing_like(), n_days=3)
    assert total_commute_km(days) < 80, "均衡把通勤搞崩了"
    # 且仍优于朴素按顺序切分
    pois = _beijing_like()
    naive = [pois[i::3] for i in range(3)]
    assert total_commute_km(days) < total_commute_km(naive)
