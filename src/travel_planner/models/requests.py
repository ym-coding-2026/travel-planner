"""用户输入请求模型。只读锚点，全程不变，所有 Agent 都从它读约束。"""
from datetime import date
from enum import Enum
from typing import List

from pydantic import BaseModel, Field


class TransportPref(str, Enum):
    PUBLIC = "public"
    DRIVE = "drive"
    WALK = "walk"
    FLEXIBLE = "flexible"


class UserRequest(BaseModel):
    destination: str
    start_date: date
    days: int = Field(gt=0)
    travelers: int = 1
    total_budget: float = Field(gt=0)
    currency: str = "CNY"
    preferences: List[str] = []            # ["美食","历史"]
    transport_pref: TransportPref = TransportPref.FLEXIBLE
    max_poi_per_day: int = 4
    excluded_poi_ids: List[str] = []       # 回退边会往这里追加
    special_needs: List[str] = []
