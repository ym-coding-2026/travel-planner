"""MCP 工具层返回的数据模型（坐标级，贴近真实高德 API 形状）。"""
from typing import List, Optional

from pydantic import BaseModel

from .common import GeoPoint, TransportMode


class RouteStep(BaseModel):
    """路线中的单段说明（真实高德会返回多步，这里至少保留聚合的一步）。"""

    instruction: str
    distance_km: float
    duration_min: int


class RouteInfo(BaseModel):
    """两点间路线结果。get_route 的返回结构。"""

    origin: GeoPoint
    destination: GeoPoint
    mode: TransportMode
    distance_km: float
    duration_min: int
    cost: float
    steps: List[RouteStep] = []


class WeatherInfo(BaseModel):
    """天气查询结果。get_weather 的返回结构。"""

    city: str
    date: str                      # YYYY-MM-DD
    day_weather: str
    night_weather: str
    temperature_high: float
    temperature_low: float
    wind: str


class PriceInfo(BaseModel):
    """酒店均价查询结果。get_hotel_price 的返回结构。"""

    city: str
    checkin: str                   # YYYY-MM-DD
    star_level: int                # 3 / 4 / 5
    avg_price_per_night: float
    currency: str = "CNY"
