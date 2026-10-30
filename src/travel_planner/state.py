"""LangGraph 的全局状态定义（GraphState）。

设计要点（对应阶段 0 硬约束）：
- 这是一个 TypedDict，所有 Agent 共享同一份状态。专家节点只写自己负责的字段，
  物理上杜绝"专家之间互相通信"（硬约束 1）。
- `request` 是只读锚点（全程不变），所有 Agent 从中读约束（预算 / 天数 / 偏好）。
- `routing` 由 Supervisor 写回；`errors` 是追加式错误通道（见 supervisor 节点失败处理）。
- 用 Annotated + operator.add 做追加 reducer 的字段（trace / errors）天然支持可观测与可重试，
  不影响其它字段。
"""
from __future__ import annotations

import operator
from typing import Annotated, Optional, TypedDict

from pydantic import BaseModel

from .models.requests import UserRequest
from .models.poi import POI, RouteSegment
from .models.plan import Itinerary
from .models.budget import BudgetReport
from .models.review import ReviewResult
from .models.routing import RoutingDecision


class StepRecord(BaseModel):
    """每一步的结构化日志（追加到 state.trace），便于评测与面试讲可观测性，不承载散文。"""

    agent: str
    action: str
    status: str
    ts: str = ""


class GraphState(TypedDict, total=False):
    request: UserRequest
    pois: list[POI]
    poi_candidates: list[POI]                # poi_agent 写回：MCP search_poi 的**原始候选集**
                                             # （review_agent 的"景点是否存在于检索结果"以此为事实基准）
    segments: list[RouteSegment]             # route_agent 写回：POI 间交通段（算法 + 工具产出）
    route_order: list[list[str]]             # route_agent 写回：每天 POI id 顺序（算法聚类+排序结果）
    route_notes: list[str]                   # route_agent 写回：LLM 润色的每日通勤说明
    itinerary: Optional[Itinerary]
    budget_report: Optional[BudgetReport]
    review: Optional[ReviewResult]
    routing: Optional[RoutingDecision]
    reselect_count: int                      # 预算超支触发的 POI 重选次数（上限 2）
    repair_count: int                        # review fail 触发的修复路由次数（上限 2）
    excluded_poi_ids: list[str]
    budget_pressure: Optional[str]           # 回退边写入的"降低预算"约束标记，poi_agent 消费
    partial: bool                            # fallback 节点置 True，表示"部分满足"
    status: Optional[str]                    # completed / partial / ...
    trace: Annotated[list[StepRecord], operator.add]
    errors: Annotated[list[str], operator.add]
