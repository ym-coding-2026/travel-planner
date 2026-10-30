"""API 层的统一响应结构与错误码。

设计原则（面试可讲）：
1. **统一信封**：所有接口都返回 `{"ok": bool, "data": ..., "error": ...}`，
   前端/Curl 不需要为每个接口写不同的解析逻辑。
2. **错误可判**：错误不是一句人话，而是 `code`（机器可判的枚举）+ `message`（人读）
   + `details`（结构化上下文，如哪个字段校验失败）。这样前端能做分支、评测能统计。
3. **HTTP 状态码与 code 分离**：状态码表达"HTTP 语义"，code 表达"业务语义"。
   例如任务还在跑时 `GET /plan/{id}` 返回 200 + `status=running`（这不是错误），
   而任务不存在返回 404 + `code=TASK_NOT_FOUND`。
"""
from __future__ import annotations

from enum import Enum
from typing import Any, Dict, Generic, List, Optional, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")


class ErrorCode(str, Enum):
    """错误码枚举。前端按 code 分支，不要按 message 字符串匹配。"""

    VALIDATION_ERROR = "VALIDATION_ERROR"        # 请求体不符合 UserRequest 约束（422）
    TASK_NOT_FOUND = "TASK_NOT_FOUND"            # task_id 不存在（404）
    TASK_NOT_FINISHED = "TASK_NOT_FINISHED"      # 任务还没跑完，结果不可读（409）
    TASK_ALREADY_RUNNING = "TASK_ALREADY_RUNNING"  # 同一 task_id 重复提交（409）
    TASK_FAILED = "TASK_FAILED"                  # 图执行抛异常（500）
    RECURSION_LIMIT = "RECURSION_LIMIT"          # 撞到 recursion_limit（500）
    INTERNAL_ERROR = "INTERNAL_ERROR"            # 未分类异常（500）


class ErrorDetail(BaseModel):
    """错误的详细信息。details 是结构化上下文，不要往里塞散文。"""

    code: ErrorCode
    message: str
    details: Optional[Any] = None


class ApiResponse(BaseModel, Generic[T]):
    """统一信封：ok=true 时看 data，ok=false 时看 error。"""

    ok: bool = True
    data: Optional[T] = None
    error: Optional[ErrorDetail] = None

    @classmethod
    def success(cls, data: T) -> "ApiResponse[T]":
        return cls(ok=True, data=data, error=None)

    @classmethod
    def failure(cls, code: ErrorCode, message: str, details: Any = None) -> "ApiResponse[T]":
        return cls(ok=False, data=None, error=ErrorDetail(code=code, message=message, details=details))


# --------------------------------------------------------------------------- #
# 业务 DTO（不是图内部的 Pydantic 模型，是"对外契约"）
# --------------------------------------------------------------------------- #
class PlanTaskStatus(str, Enum):
    """任务状态。故意与图内部的 status 字段解耦：图内部叫什么由节点决定，
    对外契约只认这四个值。"""

    QUEUED = "queued"        # 已接受，还没开始跑
    RUNNING = "running"      # 正在跑
    COMPLETED = "completed"  # 跑完且全部满足
    PARTIAL = "partial"      # 跑完但部分满足（fallback 出口）
    FAILED = "failed"        # 异常终止


class SubmitPlanResponse(BaseModel):
    """POST /plan 的返回：只给 task_id 和查询入口，不返回规划结果（异步语义）。"""

    task_id: str = Field(description="任务 ID，后续所有查询都用它")
    status: PlanTaskStatus = Field(description="当前状态，刚提交时是 queued")
    status_url: str = Field(description="查询状态的相对路径")
    stream_url: str = Field(description="SSE 流式事件订阅路径")


class StepTiming(BaseModel):
    """单个节点的执行记录（GET /plan/{id}/trace 的核心）。"""

    seq: int = Field(description="全局递增序号")
    agent: str = Field(description="节点名，如 poi_agent")
    action: str = Field(description="该节点做了什么，如 select->10pois")
    status: str = Field(description="ok / degraded / error / partial / pass ...")
    node_latency_ms: Optional[float] = Field(default=None, description="节点执行耗时（毫秒）")
    ts: Optional[str] = Field(default=None, description="节点自己记录的时间戳")
    tokens: Optional[int] = Field(
        default=None,
        description="该节点消耗的 token 数。当前为 None：未接入 token 计费回调，见 README 说明",
    )
    errors: List[str] = Field(default_factory=list, description="该节点本轮记录的错误")


class PlanTraceResponse(BaseModel):
    """GET /plan/{id}/trace 的返回：给面试演示/调试用的完整执行画像。"""

    task_id: str
    status: PlanTaskStatus
    total_elapsed_ms: Optional[float] = Field(default=None, description="从开始跑到结束的总耗时")
    node_count: int = Field(description="节点执行步数")
    node_calls: dict = Field(description="每个节点被调用的次数（看编排效率）")
    total_tokens: Optional[int] = Field(default=None, description="总 token 消耗（当前未接入）")
    steps: List[StepTiming] = Field(description="按时间顺序的每步执行记录")
    errors: List[str] = Field(default_factory=list, description="整条链路上的所有错误")
    counts: dict = Field(
        default_factory=dict,
        description="回退计数：reselect_count / repair_count（面试时可讲回退真实发生过几次）",
    )


class PlanResultResponse(BaseModel):
    """GET /plan/{id} 的返回：状态 + 规划结果（结果可能为 null，取决于状态）。"""

    task_id: str
    status: PlanTaskStatus
    partial: bool = Field(default=False, description="是否走了 fallback（部分满足）")
    reasons: List[str] = Field(default_factory=list, description="partial 时的具体原因")
    elapsed_ms: Optional[float] = Field(default=None, description="已耗时（running 时也在增长）")
    itinerary: Optional[Dict[str, Any]] = Field(default=None, description="Itinerary 序列化结果")
    budget: Optional[Dict[str, Any]] = Field(default=None, description="BudgetReport 序列化结果")
    review: Optional[Dict[str, Any]] = Field(default=None, description="ReviewResult 序列化结果")
    pois: List[Dict[str, Any]] = Field(default_factory=list, description="选中的 POI 明细")
