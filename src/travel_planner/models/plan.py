"""行程（Itinerary）相关的数据模型：单日计划 -> 整体行程。"""
from datetime import date
from typing import List

from pydantic import BaseModel

from .poi import RouteSegment


class ScheduledPOI(BaseModel):
    """某 POI 在某一天的到访时段。"""

    poi_id: str
    start_time: str                # "09:00"
    end_time: str                  # "10:30"


class DayPlan(BaseModel):
    """单日行程计划。itinerary_agent 的产物。"""

    day_index: int
    date: date
    schedule: List[ScheduledPOI]
    segments: List[RouteSegment]   # 当日 POI 之间的交通段
    day_cost: float
    notes: List[str] = []          # 短结构化提示（如"午餐安排在 A 附近"），非散文


class Itinerary(BaseModel):
    """完整行程。多日 DayPlan 的组合。"""

    request_id: str
    days: List[DayPlan]
    ordered_poi_ids: List[str]
    total_cost: float
    total_distance_km: float
    total_duration_min: int
