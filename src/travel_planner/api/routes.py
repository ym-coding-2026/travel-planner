"""API 路由：/plan 的提交、查询、trace、SSE 流。

接口清单：
- POST   /plan                     提交规划任务，立刻返回 task_id
- GET    /plan/{task_id}           查询状态 + 规划结果（从 Checkpointer 读）
- GET    /plan/{task_id}/trace     执行画像（每节点耗时、错误、调用次数）
- GET    /plan/{task_id}/stream    SSE 流式推送节点完成事件
- GET    /healthz                  健康检查
"""
from __future__ import annotations

import asyncio
import json
from collections import Counter
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sse_starlette.sse import EventSourceResponse

from ..models.requests import UserRequest
from .schemas import (
    ApiResponse,
    ErrorCode,
    PlanResultResponse,
    PlanTaskStatus,
    PlanTraceResponse,
    StepTiming,
    SubmitPlanResponse,
)
from .tasks import task_store

router = APIRouter()

# SSE 心跳间隔（秒）。长时间没事件时发 ping，防止中间层（nginx/浏览器）掐断连接。
SSE_PING_SECONDS = 15


def _err(status_code: int, code: ErrorCode, message: str, details: Any = None) -> JSONResponse:
    """统一错误响应。HTTP 状态码表达 HTTP 语义，code 表达业务语义。"""
    body = ApiResponse.failure(code, message, details).model_dump(mode="json")
    return JSONResponse(status_code=status_code, content=body)


def _require_task(task_id: str):
    """取任务；不存在返回 (None, 404 响应)。"""
    rec = task_store.get(task_id)
    if rec is None:
        return None, _err(
            404,
            ErrorCode.TASK_NOT_FOUND,
            f"task_id 不存在: {task_id}（进程重启后内存任务表会清空，请重新提交）",
        )
    return rec, None


# --------------------------------------------------------------------------- #
# POST /plan
# --------------------------------------------------------------------------- #
@router.post(
    "/plan",
    response_model=ApiResponse[SubmitPlanResponse],
    status_code=202,
    summary="提交规划任务（异步）",
    description=(
        "接收 UserRequest，**立刻返回 task_id**，规划在后台异步执行。\n\n"
        "用 202 Accepted 而非 200：语义上是「已接受但尚未完成」。\n\n"
        "后续用 `GET /plan/{task_id}` 轮询，或用 `GET /plan/{task_id}/stream` 订阅进度。"
    ),
    responses={
        202: {"description": "任务已接受，正在后台执行"},
        422: {"description": "请求体不符合 UserRequest 约束"},
    },
)
async def submit_plan(payload: UserRequest, request: Request):
    """提交一个规划任务。

    `payload: UserRequest` 让 FastAPI 自动做 Pydantic 校验：
    days>0、total_budget>0 等约束不满足时直接 422，不会进入业务逻辑。
    """
    rec = await task_store.create(payload)
    base = str(request.base_url).rstrip("/")
    task_store.start(rec)

    data = SubmitPlanResponse(
        task_id=rec.task_id,
        status=rec.status,
        status_url=f"/plan/{rec.task_id}",
        stream_url=f"/plan/{rec.task_id}/stream",
    )
    return JSONResponse(
        status_code=202,
        content=ApiResponse.success(data).model_dump(mode="json"),
    )


# --------------------------------------------------------------------------- #
# GET /plan/{task_id}
# --------------------------------------------------------------------------- #
@router.get(
    "/plan/{task_id}",
    response_model=ApiResponse[PlanResultResponse],
    summary="查询规划状态与结果",
    description=(
        "从 **Checkpointer** 读取该会话的最新 state（不是从内存缓存读）。\n\n"
        "- 任务在跑：`status=running`，`itinerary` 可能为 null（取决于跑到哪一步了）\n"
        "- 任务完成：`status=completed/partial`，返回完整行程\n"
        "- 任务失败：`status=failed`，`reasons` 里带错误摘要\n\n"
        "注意 `partial=true` 不是错误：它表示走了 fallback，结果是「部分满足」的，原因在 `reasons`。"
    ),
    responses={
        404: {"description": "task_id 不存在"},
    },
)
async def get_plan(task_id: str):
    rec, err = _require_task(task_id)
    if err:
        return err

    values = await task_store.read_state(task_id) or {}

    itinerary = values.get("itinerary")
    budget = values.get("budget_report")
    review = values.get("review")
    pois = values.get("pois") or []

    reasons: List[str] = []
    if rec.status == PlanTaskStatus.PARTIAL:
        # fallback 节点把原因写在 errors 的 last 一条（"partial_result: ..."）
        for e in values.get("errors") or []:
            text = str(e)
            if text.startswith("partial_result:"):
                body = text.split(":", 1)[1].strip()
                reasons.extend([r.strip() for r in body.split(";") if r.strip()])
    if rec.status == PlanTaskStatus.FAILED and rec.error:
        reasons.append(rec.error)

    data = PlanResultResponse(
        task_id=task_id,
        status=rec.status,
        partial=rec.status == PlanTaskStatus.PARTIAL,
        reasons=reasons,
        elapsed_ms=rec.elapsed_ms,
        itinerary=_dump(itinerary),
        budget=_dump(budget),
        review=_dump(review),
        pois=[_dump(p) for p in pois],
    )
    return ApiResponse.success(data)


# --------------------------------------------------------------------------- #
# GET /plan/{task_id}/trace
# --------------------------------------------------------------------------- #
@router.get(
    "/plan/{task_id}/trace",
    response_model=ApiResponse[PlanTraceResponse],
    summary="查询执行画像（每节点耗时 / 错误）",
    description=(
        "返回每个节点的执行记录：节点名、动作、耗时、错误。用于调试与面试演示。\n\n"
        "**耗时是怎么来的**：执行器用 `graph.astream(stream_mode='updates')` 逐节点推进，"
        "每收到一个节点完成事件就记一次时间差 —— 这是真实的墙钟耗时，不是估的。\n\n"
        "**token 为什么是 null**：当前 LLM 调用没有挂 callback，所以拿不到 token 数。"
        "接入 `BaseCallbackHandler` 后即可填充，接口契约不变（字段保留）。"
    ),
    responses={404: {"description": "task_id 不存在"}},
)
async def get_trace(task_id: str):
    rec, err = _require_task(task_id)
    if err:
        return err

    values = await task_store.read_state(task_id) or {}
    traces = values.get("trace") or []

    steps: List[StepTiming] = []
    node_calls: Counter = Counter()
    # node_latencies 按调用顺序记录，但 trace 里有多个节点交错，需要按次数对齐
    cursor: Dict[str, int] = {}
    for i, t in enumerate(traces):
        agent = t.agent if hasattr(t, "agent") else t.get("agent", "?")
        action = t.action if hasattr(t, "action") else t.get("action", "")
        status = t.status if hasattr(t, "status") else t.get("status", "")
        ts = t.ts if hasattr(t, "ts") else t.get("ts", "")

        idx = cursor.get(agent, 0)
        lat_list = rec.node_latencies.get(agent) or []
        latency = lat_list[idx] if idx < len(lat_list) else None
        cursor[agent] = idx + 1
        node_calls[agent] += 1

        steps.append(
            StepTiming(
                seq=i,
                agent=agent,
                action=action,
                status=status,
                node_latency_ms=latency,
                ts=ts,
                tokens=None,
            )
        )

    all_errors = [str(e) for e in (values.get("errors") or [])]

    data = PlanTraceResponse(
        task_id=task_id,
        status=rec.status,
        total_elapsed_ms=rec.elapsed_ms,
        node_count=len(steps),
        node_calls=dict(node_calls),
        total_tokens=None,
        steps=steps,
        errors=all_errors,
        counts={
            "reselect_count": values.get("reselect_count", 0),
            "repair_count": values.get("repair_count", 0),
        },
    )
    return ApiResponse.success(data)


# --------------------------------------------------------------------------- #
# GET /plan/{task_id}/stream  (SSE)
# --------------------------------------------------------------------------- #
@router.get(
    "/plan/{task_id}/stream",
    summary="订阅节点完成事件（SSE）",
    description=(
        "Server-Sent Events 流。事件类型：\n\n"
        "- `task_start`  开始执行，带 request\n"
        "- `node_end`    某个节点完成，带 node / action / latency_ms / errors\n"
        "- `task_end`    全部结束，带 status / elapsed_ms，收到即可断开\n\n"
        "**支持回放**：连上来会先把已经发生的事件补发一遍，再推新的。"
        "所以「边跑边连」和「跑完再连」都能看到完整的执行序列。\n\n"
        "curl 直接测试：`curl -N http://127.0.0.1:8000/plan/{task_id}/stream`"
    ),
    responses={404: {"description": "task_id 不存在"}},
)
async def stream_plan(task_id: str, request: Request):
    rec = task_store.get(task_id)
    if rec is None:
        return _err(404, ErrorCode.TASK_NOT_FOUND, f"task_id 不存在: {task_id}")

    history, queue = await task_store.subscribe(rec)

    async def _gen():
        try:
            # 先回放历史事件（断线重连/跑完再连都能看到全貌）
            for ev in history:
                yield {"event": ev.get("type", "message"), "data": json.dumps(ev, ensure_ascii=False)}
            while True:
                if await request.is_disconnected():
                    break
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=SSE_PING_SECONDS)
                except asyncio.TimeoutError:
                    # 心跳：注释行，浏览器不会当事件处理，但保活连接
                    yield {"comment": "ping"}
                    continue
                if item is None:
                    break
                yield {
                    "event": item.get("type", "message"),
                    "data": json.dumps(item, ensure_ascii=False),
                }
        finally:
            task_store.unsubscribe(rec, queue)

    return EventSourceResponse(_gen(), ping=SSE_PING_SECONDS)


# --------------------------------------------------------------------------- #
# GET /healthz
# --------------------------------------------------------------------------- #
@router.get("/healthz", summary="健康检查")
async def healthz():
    """健康检查 + LLM 配置自检。

    把 key 状态放进健康检查，是因为"规划失败"最常见的根因就是没配 key 或配错平台，
    而这个信息在 `/plan` 的报错里往往被包了一层。这里直接摊开（只给提示，不给明文）。
    """
    from ..llm.client import llm_config_status

    return ApiResponse.success({"status": "ok", "llm": llm_config_status()})


def _dump(obj: Optional[Any]) -> Optional[Dict[str, Any]]:
    """Pydantic 模型 → JSON 可序列化 dict；None 原样返回。"""
    if obj is None:
        return None
    if hasattr(obj, "model_dump"):
        return obj.model_dump(mode="json")
    return obj
