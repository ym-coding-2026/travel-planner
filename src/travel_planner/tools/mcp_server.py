"""MCP Server：用官方 MCP Python SDK 暴露高德（mock）工具。

运行方式：
- 作为 stdio MCP server 被客户端拉起：
    python -m travel_planner.tools.mcp_server
  或
    python src/travel_planner/tools/mcp_server.py
- 切换真实高德：设置环境变量 AMAP_REAL_MODE=true 与 AMAP_API_KEY=xxx（仅改配置，不改代码）。

每个工具都带有清晰的 docstring 与输入校验：LLM 通过协议看到的就是这些描述，
因此 docstring 必须说明参数含义、合法取值、返回结构，便于模型正确决策调用。
"""
from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from ..models.poi import POI
from ..models.tools import PriceInfo, RouteInfo, WeatherInfo
from .backend import (
    get_hotel_price as _get_hotel_price,
    get_route as _get_route,
    get_weather as _get_weather,
    search_poi as _search_poi,
)

mcp = FastMCP("amap-travel-mock")


@mcp.tool()
async def search_poi(city: str, keywords: str = "", category: str = "") -> list[POI]:
    """搜索指定城市的兴趣点（POI）。用于根据用户偏好挑选景点、餐厅、酒店周边等。

    参数:
        city: 城市中文名，必填。支持 "北京" / "上海" / "成都"（mock 数据）。
        keywords: 名称或标签关键词，可选。如 "故宫"、"美食"、"博物馆"。
        category: POI 分类精确过滤，可选。如 "历史古迹"、"公园"、"博物馆"、"美食街"、"商业街"。
                  留空表示不限分类。

    返回:
        List[POI]: 匹配的 POI 列表。每个 POI 含 poi_id、name、category、location(经纬度)、
        address、rating、avg_cost_per_person(人均消费)、suggested_duration_min(建议时长)、
        open_time(开放时间)、ticket_price(门票) 等字段，足以支撑后续路线与预算计算。

    注意:
        - 必须传入有效 city，否则报错。
        - keywords 与 category 可同时为空，此时返回该城市全部样例 POI（便于无偏好兜底）。
    """
    return await _search_poi(city, keywords, category)


@mcp.tool()
async def get_route(
    origin_lat: float,
    origin_lng: float,
    dest_lat: float,
    dest_lng: float,
    mode: str,
) -> RouteInfo:
    """计算两点之间的路线（距离、耗时、费用）。用于安排 POI 之间的交通段。

    参数:
        origin_lat: 起点纬度（十进制，范围 -90~90）。
        origin_lng: 起点经度（十进制，范围 -180~180）。
        dest_lat:   终点纬度。
        dest_lng:   终点经度。
        mode: 交通方式，必须是 "walk"(步行) / "public"(公交地铁) / "drive"(驾车) / "taxi"(打车) 之一。

    返回:
        RouteInfo: 含起点/终点坐标、交通方式、总距离(km)、总耗时(分钟)、预估费用(元)，以及分步说明 steps。

    注意:
        - 经纬度必须是合法十进制数值，越界会报错。
        - mode 取错值会报错并提示合法取值。
    """
    return await _get_route(origin_lat, origin_lng, dest_lat, dest_lng, mode)


@mcp.tool()
async def get_weather(city: str, date: str) -> WeatherInfo:
    """查询某城市指定日期的天气。用于判断出行适宜度、调整户外行程。

    参数:
        city: 城市中文名，如 "北京"。
        date: 日期，格式严格为 "YYYY-MM-DD"。

    返回:
        WeatherInfo: 含城市、日期、白天/夜间天气、最高/最低气温、风力。
    """
    return await _get_weather(city, date)


@mcp.tool()
async def get_hotel_price(city: str, checkin: str, star_level: int) -> PriceInfo:
    """查询某城市某入住日期、某星级的酒店均价。用于预算估算中的住宿成本。

    参数:
        city: 城市中文名，如 "成都"。
        checkin: 入住日期，格式 "YYYY-MM-DD"。
        star_level: 酒店星级，必须是 3 / 4 / 5。

    返回:
        PriceInfo: 含城市、入住日期、星级、每晚均价(avg_price_per_night)、货币(默认 CNY)。
    """
    return await _get_hotel_price(city, checkin, star_level)


if __name__ == "__main__":
    mcp.run(transport="stdio")
