"""跨模型共享的基础类型：地理坐标点 + 交通方式枚举。"""
from enum import Enum

from pydantic import BaseModel


class TransportMode(str, Enum):
    """Agent 在路线规划时可选择的交通方式（值即高德 API 的 mode 取值）。"""

    WALK = "walk"        # 步行
    PUBLIC = "public"    # 公交 / 地铁
    DRIVE = "drive"      # 驾车
    TAXI = "taxi"        # 打车


class GeoPoint(BaseModel):
    """十进制经纬度坐标点。lat 纬度，lng 经度。"""

    lng: float
    lat: float
