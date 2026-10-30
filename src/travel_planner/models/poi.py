"""POI 与图内路线段（POI 级）的数据模型。

注意区分两个层次：
- POI / RouteSegment：图内 Agent 之间传递的结构化数据。
- RouteInfo（见 tools.py）：MCP 工具 search/get_route 的返回，坐标级，由 route_agent 转换后写入 RouteSegment。
"""
from typing import Optional

from pydantic import BaseModel

from .common import GeoPoint, TransportMode


class POI(BaseModel):
    """一个兴趣点。search_poi 工具返回的核心结构，字段贴近真实高德 POI。"""

    poi_id: str
    name: str
    category: str
    location: GeoPoint
    address: Optional[str] = None
    rating: Optional[float] = None
    avg_cost_per_person: Optional[float] = None
    suggested_duration_min: int = 90
    open_time: Optional[str] = None          # 如 "08:30-17:00" 或 "全天"
    ticket_price: Optional[float] = None     # 门票，0 表示免费
    tags: list[str] = []
    source: str = "amap"                     # 数据来源，便于评测断言


class RouteSegment(BaseModel):
    """两个 POI 之间的交通段（图内结构化数据，区别于 MCP 的 RouteInfo）。"""

    from_poi_id: str
    to_poi_id: str
    mode: TransportMode
    distance_km: float
    duration_min: int
    cost: float
