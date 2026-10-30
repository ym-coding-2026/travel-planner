"""MCP 客户端封装：供 LangGraph 节点调用。

特性：
1. 两种传输：
   - "direct"：进程内直连 backend 函数（不经过网络），用于离线评测 / 单元测试，零依赖启动。
   - "stdio"：以子进程方式拉起 mcp_server.py，通过官方 MCP SDK 的 ClientSession 调用，
     完全符合 MCP 协议，用于真实部署。
   两种模式对外暴露同一套 async 方法，切换只改配置（settings.mcp_transport）。
2. 超时：用 asyncio.timeout 包裹每次调用（settings.request_timeout）。
3. 重试：用 tenacity 指数退避重试（settings.max_retries），对超时/网络异常生效。
4. 结构化返回：自动把工具结果反序列化为约定的 Pydantic 模型（list[POI] / RouteInfo / ...），
   节点拿到的就是强类型对象，无需再解析 JSON。
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any, Callable

from pydantic import TypeAdapter
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from ..models.poi import POI
from ..models.tools import PriceInfo, RouteInfo, WeatherInfo
from ..settings import settings


class MCPToolClient:
    def __init__(
        self,
        transport: str | None = None,
        server_command: list[str] | None = None,
        timeout: float | None = None,
        max_retries: int | None = None,
    ):
        self.transport = transport or settings.mcp_transport
        self.timeout = timeout or settings.request_timeout
        self.max_retries = max_retries or settings.max_retries
        self._stack: AsyncExitStack | None = None
        self._session = None

        # stdio 模式下默认拉起同目录下的 mcp_server.py
        self._server_command = server_command or [
            sys.executable,
            str(Path(__file__).resolve().parent / "mcp_server.py"),
        ]

        # direct 模式下的本地函数映射（避免导入 mcp SDK，便于离线评测）
        from .backend import (
            get_hotel_price as _gh,
            get_route as _gr,
            get_weather as _gw,
            search_poi as _sp,
        )

        self._direct = {
            "search_poi": _sp,
            "get_route": _gr,
            "get_weather": _gw,
            "get_hotel_price": _gh,
        }
        # 每个工具对应的返回类型，用于反序列化校验
        self._return_types = {
            "search_poi": list[POI],
            "get_route": RouteInfo,
            "get_weather": WeatherInfo,
            "get_hotel_price": PriceInfo,
        }

    # ----- 生命周期 -----
    async def __aenter__(self):
        if self.transport == "stdio":
            from mcp import ClientSession
            from mcp.client.stdio import StdioServerParameters, stdio_client

            self._stack = AsyncExitStack()
            params = StdioServerParameters(
                command=self._server_command[0],
                args=self._server_command[1:],
                env=dict(os.environ),
            )
            read, write = await self._stack.enter_async_context(stdio_client(params))
            session = await self._stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
            self._session = session
        return self

    async def __aexit__(self, *exc):
        if self._stack:
            await self._stack.aclose()

    # ----- 重试 + 超时 -----
    async def _with_retry(self, coro_factory: Callable[[], Any]) -> Any:
        async def _attempt():
            async with asyncio.timeout(self.timeout):
                return await coro_factory()

        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(self.max_retries),
            wait=wait_exponential(multiplier=0.5, max=5),
            retry=retry_if_exception_type(Exception),
            reraise=True,
        ):
            with attempt:
                return await _attempt()

    # ----- 统一调用入口 -----
    async def _call(self, name: str, kwargs: dict):
        if self.transport == "direct":
            raw = await self._with_retry(lambda: self._direct[name](**kwargs))
            return TypeAdapter(self._return_types[name]).validate_python(raw)

        result = await self._with_retry(lambda: self._session.call_tool(name, kwargs))
        payload = _extract_payload(result)
        return TypeAdapter(self._return_types[name]).validate_python(payload)

    # ----- 业务方法（节点只调用这些） -----
    async def search_poi(self, city: str, keywords: str = "", category: str = "") -> list[POI]:
        return await self._call(
            "search_poi", {"city": city, "keywords": keywords, "category": category}
        )

    async def get_route(
        self, origin_lat, origin_lng, dest_lat, dest_lng, mode
    ) -> RouteInfo:
        return await self._call(
            "get_route",
            {
                "origin_lat": origin_lat,
                "origin_lng": origin_lng,
                "dest_lat": dest_lat,
                "dest_lng": dest_lng,
                "mode": mode,
            },
        )

    async def get_weather(self, city: str, date: str) -> WeatherInfo:
        return await self._call("get_weather", {"city": city, "date": date})

    async def get_hotel_price(self, city: str, checkin: str, star_level: int) -> PriceInfo:
        return await self._call(
            "get_hotel_price", {"city": city, "checkin": checkin, "star_level": star_level}
        )


def _extract_payload(result):
    """兼容 FastMCP 的两种返回形态：结构化内容 或 文本 JSON。"""
    sc = getattr(result, "structuredContent", None)
    if sc:
        if isinstance(sc, dict) and "result" in sc:
            return sc["result"]
        return sc
    text = result.content[0].text
    return json.loads(text)
