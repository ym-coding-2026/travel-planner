"""集中式配置：从环境变量 / .env 读取，全程单例。

real_mode 开关说明：
- amap_real_mode=False（默认）：所有 MCP 工具走 mock 实现，离线可跑、可评测。
- amap_real_mode=True 且 amap_api_key 已配置：search_poi / get_route / get_weather
  自动切换到真实高德 API；无需改任何业务代码，只改配置。
- get_hotel_price 在真实模式下暂未接入（高德无酒店价格 API），会显式报错提示。
"""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="", extra="ignore")

    # MCP / 高德
    amap_real_mode: bool = False
    amap_api_key: str = ""
    amap_base_url: str = "https://restapi.amap.com/v3"

    # 客户端传输方式：stdio（真实 MCP 进程）| direct（进程内直连，评测/测试用）
    mcp_transport: str = "direct"

    # 超时与重试（tenacity 使用）
    request_timeout: float = 10.0
    max_retries: int = 3


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
