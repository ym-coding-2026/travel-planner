"""工具实现层（与 MCP 协议解耦）。

设计要点：
- 这一层不含任何 MCP 代码，MCP server 只是它的"协议包装"。
- 每个工具两个分支：mock（默认，离线可跑、可评测）与 real（真实高德，仅改配置即可启用）。
- 所有工具都是 async，方便真实模式下的 httpx 异步请求，也统一被 client 的 tenacity 重试包裹。
- 输入校验集中在这里，报错信息对 LLM 友好（明确告诉它哪里错了、合法取值是什么）。
"""
from __future__ import annotations

import math
from datetime import datetime

from ..models.common import GeoPoint, TransportMode
from ..models.poi import POI
from ..models.tools import PriceInfo, RouteInfo, RouteStep, WeatherInfo
from ..settings import settings
from .mock_data import MOCK_POIS


def _require(cond: bool, msg: str) -> None:
    if not cond:
        raise ValueError(msg)


def _is_valid_date(s: str) -> bool:
    try:
        datetime.strptime(s, "%Y-%m-%d")
        return True
    except (ValueError, TypeError):
        return False


# --------------------------------------------------------------------------- #
# search_poi
# --------------------------------------------------------------------------- #
async def search_poi(city: str, keywords: str = "", category: str = "") -> list[POI]:
    _require(isinstance(city, str) and city.strip(), "city 必须为非空字符串")
    if settings.amap_real_mode and settings.amap_api_key:
        return await _search_poi_real(city, keywords, category)
    return _search_poi_mock(city, keywords, category)


def _search_poi_mock(city: str, keywords: str, category: str) -> list[POI]:
    city = city.strip()
    kw = (keywords or "").strip()
    cat = (category or "").strip()
    out: list[POI] = []
    for d in MOCK_POIS.get(city, []):
        name_hit = kw and kw in d["name"]
        tag_hit = kw and any(kw in t for t in d.get("tags", []))
        if kw and not (name_hit or tag_hit):
            continue
        if cat and cat != d.get("category"):
            continue
        out.append(
            POI(
                poi_id=d["id"],
                name=d["name"],
                category=d["category"],
                location=GeoPoint(lng=d["lng"], lat=d["lat"]),
                address=d.get("address"),
                rating=d.get("rating"),
                avg_cost_per_person=d.get("avg_cost"),
                suggested_duration_min=d.get("duration", 90),
                open_time=d.get("open_time"),
                ticket_price=d.get("ticket_price"),
                tags=d.get("tags", []),
                source="mock",
            )
        )
    return out


async def _search_poi_real(city: str, keywords: str, category: str) -> list[POI]:
    import httpx

    params = {
        "key": settings.amap_api_key,
        "city": city,
        "keywords": keywords or "",
        "types": category or "",
        "offset": 25,
        "page": 1,
    }
    async with httpx.AsyncClient(timeout=settings.request_timeout) as client:
        r = await client.get(f"{settings.amap_base_url}/place/text", params=params)
        r.raise_for_status()
        return _parse_amap_poi(r.json())


def _parse_amap_poi(payload: dict) -> list[POI]:
    # 真实高德返回结构：payload["pois"] 为列表，每项含 id/name/location( lng,lat )/address/...
    # 这里只做最小映射，字段缺失时给默认值，保证下游不崩。
    out: list[POI] = []
    for p in payload.get("pois", []):
        lng, lat = (p.get("location") or "0,0").split(",")
        out.append(
            POI(
                poi_id=p.get("id", ""),
                name=p.get("name", ""),
                category=p.get("type", ""),
                location=GeoPoint(lng=float(lng), lat=float(lat)),
                address=p.get("address"),
                rating=None,
                avg_cost_per_person=None,
                suggested_duration_min=90,
                open_time=None,
                ticket_price=None,
                tags=[],
                source="amap",
            )
        )
    return out


# --------------------------------------------------------------------------- #
# get_route
# --------------------------------------------------------------------------- #
_SPEED_KMH = {
    TransportMode.WALK: 4.5,
    TransportMode.PUBLIC: 20.0,
    TransportMode.DRIVE: 40.0,
    TransportMode.TAXI: 35.0,
}
_COST_PER_KM = {
    TransportMode.WALK: 0.0,
    TransportMode.PUBLIC: 0.30,
    TransportMode.DRIVE: 0.60,
    TransportMode.TAXI: 2.50,
}
_BASE_FARE = {TransportMode.PUBLIC: 2.0, TransportMode.TAXI: 10.0}


async def get_route(
    origin_lat: float,
    origin_lng: float,
    dest_lat: float,
    dest_lng: float,
    mode: str,
) -> RouteInfo:
    _require(isinstance(origin_lat, (int, float)), "origin_lat 必须为数字")
    _require(isinstance(origin_lng, (int, float)), "origin_lng 必须为数字")
    _require(isinstance(dest_lat, (int, float)), "dest_lat 必须为数字")
    _require(isinstance(dest_lng, (int, float)), "dest_lng 必须为数字")
    _require(-90 <= origin_lat <= 90 and -90 <= dest_lat <= 90, "纬度需在 -90~90 之间")
    _require(-180 <= origin_lng <= 180 and -180 <= dest_lng <= 180, "经度需在 -180~180 之间")
    _require(
        mode in [m.value for m in TransportMode],
        f"mode 必须为 {[m.value for m in TransportMode]} 之一",
    )
    if settings.amap_real_mode and settings.amap_api_key:
        return await _get_route_real(origin_lat, origin_lng, dest_lat, dest_lng, mode)
    return _get_route_mock(origin_lat, origin_lng, dest_lat, dest_lng, mode)


def _haversine(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _get_route_mock(o_lat, o_lng, d_lat, d_lng, mode) -> RouteInfo:
    dist = _haversine(o_lat, o_lng, d_lat, d_lng)
    m = TransportMode(mode)
    speed = _SPEED_KMH[m]
    dur = max(1, int(dist / speed * 60))
    cost = round(_BASE_FARE.get(m, 0.0) + _COST_PER_KM[m] * dist, 2)
    step = RouteStep(
        instruction=f"从起点以 {m.value} 方式前往终点，约 {dist:.1f} 公里",
        distance_km=round(dist, 2),
        duration_min=dur,
    )
    return RouteInfo(
        origin=GeoPoint(lat=o_lat, lng=o_lng),
        destination=GeoPoint(lat=d_lat, lng=d_lng),
        mode=m,
        distance_km=round(dist, 2),
        duration_min=dur,
        cost=cost,
        steps=[step],
    )


async def _get_route_real(o_lat, o_lng, d_lat, d_lng, mode) -> RouteInfo:
    import httpx

    api_mode = {"walk": "walking", "public": "transit", "drive": "driving", "taxi": "driving"}[mode]
    params = {
        "key": settings.amap_api_key,
        "origin": f"{o_lng},{o_lat}",
        "destination": f"{d_lng},{d_lat}",
    }
    async with httpx.AsyncClient(timeout=settings.request_timeout) as client:
        r = await client.get(f"{settings.amap_base_url}/direction/{api_mode}", params=params)
        r.raise_for_status()
        return _parse_amap_route(r.json(), mode)


def _parse_amap_route(payload: dict, mode: str) -> RouteInfo:
    # 真实高德返回 paths[0] 含 distance(米) / duration(秒) / cost / steps
    path = payload.get("route", {}).get("paths", [{}])[0]
    dist_m = float(path.get("distance", 0))
    dur_s = float(path.get("duration", 0))
    cost = float(path.get("cost", {}).get("transit_fee", 0) or 0)
    steps = [
        RouteStep(
            instruction=s.get("instruction", ""),
            distance_km=round(float(s.get("distance", 0)) / 1000, 2),
            duration_min=round(float(s.get("duration", 0)) / 60),
        )
        for s in path.get("steps", [])
    ]
    return RouteInfo(
        origin=GeoPoint(lat=0, lng=0),  # 真实实现应回填，这里占位
        destination=GeoPoint(lat=0, lng=0),
        mode=TransportMode(mode),
        distance_km=round(dist_m / 1000, 2),
        duration_min=round(dur_s / 60),
        cost=cost,
        steps=steps,
    )


# --------------------------------------------------------------------------- #
# get_weather
# --------------------------------------------------------------------------- #
async def get_weather(city: str, date: str) -> WeatherInfo:
    _require(isinstance(city, str) and city.strip(), "city 必须为非空字符串")
    _require(_is_valid_date(date), "date 必须为 YYYY-MM-DD 格式")
    if settings.amap_real_mode and settings.amap_api_key:
        return await _get_weather_real(city, date)
    return _get_weather_mock(city, date)


def _get_weather_mock(city: str, date: str) -> WeatherInfo:
    month = int(date.split("-")[1])
    if month in (12, 1, 2):
        high, low, w = 5, -5, "晴"
    elif month in (3, 4, 5):
        high, low, w = 20, 10, "多云"
    elif month in (6, 7, 8):
        high, low, w = 33, 25, "雷阵雨"
    else:
        high, low, w = 22, 12, "晴"
    return WeatherInfo(
        city=city,
        date=date,
        day_weather=w,
        night_weather=w,
        temperature_high=float(high),
        temperature_low=float(low),
        wind="微风",
    )


async def _get_weather_real(city: str, date: str) -> WeatherInfo:
    import httpx

    params = {"key": settings.amap_api_key, "city": city, "extensions": "all", "date": date}
    async with httpx.AsyncClient(timeout=settings.request_timeout) as client:
        r = await client.get(f"{settings.amap_base_url}/weather/weatherInfo", params=params)
        r.raise_for_status()
        data = r.json()
    # 真实返回 casts 列表，取与 date 匹配的那天
    cast = next((c for c in data.get("forecasts", [{}])[0].get("casts", []) if c.get("date") == date), {})
    return WeatherInfo(
        city=city,
        date=date,
        day_weather=cast.get("dayweather", ""),
        night_weather=cast.get("nightweather", ""),
        temperature_high=float(cast.get("daytemp", 0)),
        temperature_low=float(cast.get("nighttemp", 0)),
        wind=cast.get("daywind", ""),
    )


# --------------------------------------------------------------------------- #
# get_hotel_price
# --------------------------------------------------------------------------- #
_BASE_HOTEL = {3: 320, 4: 650, 5: 1280}
_CITY_MULT = {"北京": 1.30, "上海": 1.35, "成都": 1.00}


async def get_hotel_price(city: str, checkin: str, star_level: int) -> PriceInfo:
    _require(isinstance(city, str) and city.strip(), "city 必须为非空字符串")
    _require(_is_valid_date(checkin), "checkin 必须为 YYYY-MM-DD 格式")
    _require(star_level in (3, 4, 5), "star_level 必须为 3 / 4 / 5")
    if settings.amap_real_mode:
        # 高德 API 不提供酒店价格，真实模式需接入第三方（如携程/美团开放平台）。
        raise NotImplementedError("real_mode 下 get_hotel_price 尚未接入第三方数据源")
    return _get_hotel_price_mock(city, checkin, star_level)


def _get_hotel_price_mock(city: str, checkin: str, star_level: int) -> PriceInfo:
    base = _BASE_HOTEL[star_level]
    mult = _CITY_MULT.get(city.strip(), 1.0)
    dt = datetime.strptime(checkin, "%Y-%m-%d")
    mult *= 1.2 if dt.weekday() >= 5 else 1.0  # 周末上浮
    price = round(base * mult)
    return PriceInfo(
        city=city,
        checkin=checkin,
        star_level=star_level,
        avg_price_per_night=price,
    )
