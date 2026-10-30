"""预算报告数据模型。budget_agent 的产物，也是"预算超支回退边"的判定依据。"""
from enum import Enum
from typing import List

from pydantic import BaseModel


class BudgetCategory(str, Enum):
    TICKET = "ticket"
    TRANSPORT = "transport"
    FOOD = "food"
    ACCOMMODATION = "accommodation"
    OTHER = "other"


class BudgetItem(BaseModel):
    category: BudgetCategory
    amount: float
    detail: str = ""


class BudgetReport(BaseModel):
    total_estimated: float
    items: List[BudgetItem]
    currency: str = "CNY"
    within_budget: bool
    over_by: float = 0.0                 # max(0, 估算 - 预算)
    suggested_cuts: List[str] = []       # 建议砍掉的 POI id / 类别，供回退边消费
