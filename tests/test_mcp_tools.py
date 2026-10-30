"""MCP 工具层离线评测（不依赖 LLM 打分，也不拉起真实 server）。

覆盖点：
1. mock 数据规模：3 城 × >=10 POI。
2. 四类工具返回结构正确（强类型 Pydantic）。
3. 输入校验生效（非法 mode / 非法日期 / 非法星级应抛 ValueError）。
4. direct 传输下超时/重试包装不报错（正常路径）。
"""
import pytest

from travel_planner.models.poi import POI
from travel_planner.models.tools import PriceInfo, RouteInfo, WeatherInfo
from travel_planner.tools.mcp_client import MCPToolClient
from travel_planner.tools.mock_data import MOCK_POIS


@pytest.fixture
def client():
    # direct 模式：进程内直连 backend，最快、最稳，适合 CI/评测
    return MCPToolClient(transport="direct")


# ---------- 1. mock 数据规模 ----------
def test_mock_data_scale():
    assert set(MOCK_POIS.keys()) >= {"北京", "上海", "成都"}
    for city, pois in MOCK_POIS.items():
        assert len(pois) >= 10, f"{city} 应有至少 10 个 POI，实际 {len(pois)}"
        for p in pois:
            assert "lat" in p and "lng" in p
            assert p["open_time"] is not None
            assert p["ticket_price"] is not None


# ---------- 2. 返回结构 ----------
@pytest.mark.asyncio
async def test_search_poi(client):
    pois = await client.search_poi("北京")
    assert len(pois) >= 10
    assert all(isinstance(p, POI) for p in pois)
    # 关键词过滤
    subset = await client.search_poi("成都", keywords="美食")
    assert len(subset) >= 1
    # 分类过滤
    historic = await client.search_poi("北京", category="历史古迹")
    assert all(p.category == "历史古迹" for p in historic)


@pytest.mark.asyncio
async def test_get_route(client):
    r = await client.get_route(39.9163, 116.3972, 39.9999, 116.2755, "public")
    assert isinstance(r, RouteInfo)
    assert r.distance_km > 0
    assert r.duration_min > 0
    assert r.mode.value == "public"


@pytest.mark.asyncio
async def test_get_weather(client):
    w = await client.get_weather("上海", "2026-07-01")
    assert isinstance(w, WeatherInfo)
    assert w.temperature_high >= w.temperature_low


@pytest.mark.asyncio
async def test_get_hotel_price(client):
    h = await client.get_hotel_price("成都", "2026-12-25", 5)
    assert isinstance(h, PriceInfo)
    assert h.avg_price_per_night > 0
    # 北京五星应贵于成都五星（城市系数）
    bj = await client.get_hotel_price("北京", "2026-12-25", 5)
    cd = await client.get_hotel_price("成都", "2026-12-25", 5)
    assert bj.avg_price_per_night > cd.avg_price_per_night


# ---------- 3. 输入校验 ----------
@pytest.mark.asyncio
async def test_get_route_invalid_mode(client):
    with pytest.raises(ValueError):
        await client.get_route(39.9, 116.4, 39.9, 116.5, "spaceship")


@pytest.mark.asyncio
async def test_get_weather_invalid_date(client):
    with pytest.raises(ValueError):
        await client.get_weather("上海", "2026/07/01")


@pytest.mark.asyncio
async def test_hotel_invalid_star(client):
    with pytest.raises(ValueError):
        await client.get_hotel_price("成都", "2026-12-25", 6)


# ---------- 4. three cities smoke ----------
@pytest.mark.asyncio
async def test_three_cities():
    c = MCPToolClient(transport="direct")
    for city in ["北京", "上海", "成都"]:
        pois = await c.search_poi(city)
        assert len(pois) >= 10
