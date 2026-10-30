"""Supervisor 的唯一结构化产出：路由决策。

硬约束 1（中心化编排）的落地：专家 Agent 之间不互相通信，全部通过共享 State 交换数据；
Supervisor 读取 State 后只输出一个 RoutingDecision（枚举），由条件边决定下一步节点。
"""
from enum import Enum

from pydantic import BaseModel


class NextStep(str, Enum):
    POI = "poi_agent"
    ROUTE = "route_agent"
    BUDGET = "budget_agent"
    ITINERARY = "itinerary_agent"
    REVIEW = "review_agent"
    END = "end"


class RoutingDecision(BaseModel):
    next: NextStep
    reason_code: str                   # 结构化原因，如 "budget_over"
