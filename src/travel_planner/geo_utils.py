"""地理聚类与每日行程排序（route_agent 的算法核心，不依赖 LLM）。

算法亮点（硬约束：最小化每日通勤时间）：
1. cluster_pois_by_day：用 KMeans（Lloyd 算法，haversine 距离，k-means++ 确定性初始化）
   把 POI 按地理坐标聚成 n_days 个簇，使同一天内的景点地理上紧凑——从根源上减少跨区通勤。
2. order_within_day：簇内用贪心最近邻（从离质心最近的点出发，每次去最近的未访问点），
   生成近似最优（TSP 启发式）的访问顺序，使每日内部通勤距离最小。
3. plan_days：聚类 + 每日内排序的组合入口。

所有函数纯算法、无外部依赖、确定性，便于单元测试与离线评测复现。
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import List, Optional, Tuple

from .models.common import GeoPoint
from .models.poi import POI

EARTH_RADIUS_KM = 6371.0088


@dataclass(frozen=True)
class _Centroid:
    """簇中心（坐标均值）。

    刻意不复用 models.common.GeoPoint：GeoPoint 表达的是"真实存在的 POI 坐标"，
    而簇中心是数学平均量，不保证落在地理约束内。用独立轻量类型避免语义污染。
    """

    lng: float
    lat: float


def haversine_km(a, b) -> float:
    """两点间大圆距离（公里）。接受 GeoPoint 或 _Centroid（只要求有 lng / lat）。"""
    lat1, lng1 = math.radians(a.lat), math.radians(a.lng)
    lat2, lng2 = math.radians(b.lat), math.radians(b.lng)
    dlat = lat2 - lat1
    dlng = lng2 - lng1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlng / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(min(1.0, math.sqrt(h)))


def _centroid(pois: List[POI]) -> _Centroid:
    n = len(pois)
    lat = sum(p.location.lat for p in pois) / n
    lng = sum(p.location.lng for p in pois) / n
    return _Centroid(lng=lng, lat=lat)


def _kmeans_labels(pois: List[POI], k: int, max_iter: int = 50) -> List[int]:
    """确定性 KMeans（Lloyd），返回每个 POI 的簇标号。

    初始化用 k-means++ 的确定性变体：第一个中心取纬度最高者，后续逐个取离已有
    中心集合最远的点——避免随机性，保证评测可复现。
    """
    n = len(pois)
    if n == 0:
        return []
    k = min(k, n)
    first = max(pois, key=lambda p: p.location.lat)
    centers = [_Centroid(lng=first.location.lng, lat=first.location.lat)]
    while len(centers) < k:

        def _far(p: POI) -> float:
            return min(haversine_km(p.location, c) for c in centers)

        nxt = max(pois, key=_far)
        centers.append(_Centroid(lng=nxt.location.lng, lat=nxt.location.lat))
    labels = [0] * n
    for _ in range(max_iter):
        changed = False
        for i, p in enumerate(pois):
            best = min(
                range(len(centers)),
                key=lambda c: haversine_km(p.location, centers[c]),
            )
            if labels[i] != best:
                labels[i] = best
                changed = True
        for c in range(len(centers)):
            members = [p for i, p in enumerate(pois) if labels[i] == c]
            if members:
                centers[c] = _centroid(members)
        if not changed:
            break
    return labels


def cluster_pois_by_day(pois: List[POI], n_days: int) -> List[List[POI]]:
    """把 POI 聚成 n_days 个地理簇（每天一个），并做负载均衡。

    三步：
    1. KMeans（Lloyd，确定性初始化）按地理坐标聚成 n_days 簇 —— 让同一天的点靠在一起；
    2. `_rebalance_clusters` 补空簇（避免"某天没景点"）；
    3. `_balance_load` 做**负载均衡**：KMeans 只优化距离、不关心每天能玩几个，
       实测北京 10 个点分 3 天会产出 1/6/3 —— 八达岭长城（离市区 60km）独占一簇，
       其余 6 个市区点挤在同一天，那天根本走不完。

    注意这里**不设硬性每日点数上限**：真给八达岭那天硬塞 3 个点，它就得先开 60km
    回来，通勤直接翻倍，反而违背"最小化每日通勤"这个项目主目标。均衡只在
    「不显著伤害通勤」的前提下做，取舍写在下面 `_balance_load` 的注释里。
    """
    n = len(pois)
    if n == 0 or n_days < 1:
        return []
    if n <= n_days:
        return [[p] for p in pois] + [[] for _ in range(n_days - n)]
    labels = _kmeans_labels(pois, n_days)
    clusters: List[List[POI]] = [[] for _ in range(n_days)]
    for p, lab in zip(pois, labels):
        clusters[lab % n_days].append(p)

    clusters = _rebalance_clusters(clusters)
    return _balance_load(clusters)


def _cost_of(clusters: List[List[POI]]) -> float:
    """当前分配的总通勤（簇内贪心最近邻排序后的距离和）。"""
    return sum(daily_commute_km(order_within_day(c)) for c in clusters)


def _balance_load(clusters: List[List[POI]], max_extra_km_per_move: float = 5.0) -> List[List[POI]]:
    """在「不显著增加总通勤」的前提下，把过大的簇拆细。

    cap = ceil(总点数 / 天数)，某簇点数 > cap 才算失衡。每次从上一次挪一个点出去，
    **只有当总通勤增量 ≤ `max_extra_km_per_move`（默认 5 公里）时才接受**，
    并且每挪一次就更新基线，后续挪动的增量要在此基础上继续算。

    为什么是「每次挪动的绝对增量」而不是「总增量的百分比」：北京实测下来的数据是——
    - 把 798 艺术区从市区簇挪到城北簇：+3.65 km  → 值得（市区簇从 6 个降到 5 个）
    - 把任意市区点挪到八达岭那天：+57 km        → 绝不值得（那天要开 60 km 直达）
    用 5 km 作门槛正好落在这两组数据中间，判定干净；用百分比（1%）则会把 798
    那 3.65 km 也判成"太贵"，退化成完全不均衡。

    取舍要写清楚：宁可某天只安排 1 个远郊点（八达岭），也不为了"点数好看"让那天
    通勤翻倍 —— 项目主目标是**最小化每日通勤**，均衡是次要目标，只在便宜时做。

    整个过程确定性（候选按 (增量, poi_id) 排序），保证离线评测可复现。
    """
    n_clusters = len(clusters)
    total = sum(len(c) for c in clusters)
    if n_clusters <= 1 or total == 0:
        return clusters

    cap = math.ceil(total / n_clusters)
    if all(len(c) <= cap for c in clusters):
        return clusters

    base = _cost_of(clusters)

    guard = 0
    while guard < 1000:
        guard += 1
        over = [i for i in range(n_clusters) if len(clusters[i]) > cap]
        if not over:
            break

        best: Optional[Tuple[float, str, int, int, POI]] = None
        for src in over:
            for p in clusters[src]:
                for dst in range(n_clusters):
                    if dst == src:
                        continue
                    trial = [list(c) for c in clusters]
                    trial[src] = [q for q in trial[src] if q.poi_id != p.poi_id]
                    trial[dst].append(p)
                    delta = _cost_of(trial) - base
                    cand = (delta, p.poi_id, src, dst, p)
                    if best is None or cand[:2] < best[:2]:
                        best = cand
        if best is None or best[0] > max_extra_km_per_move:
            break  # 没有哪一步便宜到能接受 —— 放弃，保留地理最优解

        delta, pid, src, dst, moved = best
        clusters[src] = [q for q in clusters[src] if q.poi_id != pid]
        clusters[dst].append(moved)
        base += delta

    return clusters


def _rebalance_clusters(clusters: List[List[POI]]) -> List[List[POI]]:
    """把空簇补上：从最大的簇里借一个「离整体质心最远」的点，保持确定性。

    只在 KMeans 真的产出空簇时触发；若所有簇都只剩 1 个点则无法借，保留空簇
    （下游 itinerary_agent 会跳过该天）。
    """
    flat = [p for c in clusters for p in c]
    if not flat:
        return clusters
    center = _centroid(flat)
    for idx in range(len(clusters)):
        if clusters[idx]:
            continue
        donor_idx = max(range(len(clusters)), key=lambda i: (len(clusters[i]), -i))
        donor = clusters[donor_idx]
        if len(donor) <= 1:
            continue
        moved = max(donor, key=lambda p: haversine_km(p.location, center))
        clusters[donor_idx] = [p for p in donor if p.poi_id != moved.poi_id]
        clusters[idx] = [moved]
    return clusters


def order_within_day(pois: List[POI]) -> List[POI]:
    """簇内贪心最近邻排序，生成近似最优访问顺序（每日内部通勤最小）。

    空簇直接返回空列表（不能在空列表上算质心，否则 ZeroDivisionError）。
    """
    if len(pois) <= 1:
        return list(pois)
    remaining = list(pois)
    center = _centroid(remaining)
    start = min(remaining, key=lambda p: haversine_km(p.location, center))
    remaining.remove(start)
    ordered = [start]
    while remaining:
        cur = ordered[-1]
        nxt = min(remaining, key=lambda p: haversine_km(p.location, cur.location))
        remaining.remove(nxt)
        ordered.append(nxt)
    return ordered


def plan_days(pois: List[POI], n_days: int) -> List[List[POI]]:
    """完整算法：聚类 + 每日内排序，返回每天的有序 POI 列表。"""
    clusters = cluster_pois_by_day(pois, n_days)
    return [order_within_day(c) for c in clusters]


def daily_commute_km(order: List[POI]) -> float:
    """给定一天的有序 POI，返回总通勤距离（haversine 近似），用于评估/测试。"""
    if len(order) < 2:
        return 0.0
    return sum(haversine_km(order[i].location, order[i + 1].location) for i in range(len(order) - 1))


def total_commute_km(days: List[List[POI]]) -> float:
    """多日总通勤距离。"""
    return sum(daily_commute_km(d) for d in days)
