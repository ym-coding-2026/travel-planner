"""FastAPI 应用装配。

启动方式：
    uvicorn travel_planner.api.app:app --reload --port 8000

或者（不装 uvicorn 命令行也能跑）：
    python -m travel_planner.api.app
"""
from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from .routes import router
from .schemas import ApiResponse, ErrorCode
from .tasks import task_store

TITLE = "Travel Planner API"
DESCRIPTION = """多 Agent 旅游规划系统的 HTTP 接口层（LangGraph + MCP + 离线评测）。

## 调用流程

1. `POST /plan` 提交请求 → 拿到 `task_id`（立刻返回，规划在后台跑）
2. 用 `task_id` 查进度：
   - 轮询 `GET /plan/{task_id}`
   - 或订阅 `GET /plan/{task_id}/stream`（SSE，推荐）
3. 调试/演示：`GET /plan/{task_id}/trace` 看每个节点的耗时与错误

## 统一响应格式

所有接口都返回同一个信封：

```json
{"ok": true,  "data": {...}, "error": null}
{"ok": false, "data": null,  "error": {"code": "TASK_NOT_FOUND", "message": "...", "details": null}}
```

前端按 `error.code` 分支，**不要**按 `message` 字符串匹配。

## 两个重要语义

- `202 Accepted`：提交成功但**结果还没好**，不是错误。
- `partial: true`：规划跑完了但**只部分满足**（比如预算压不下来、复核不通过），
  原因在 `reasons` 里。这是正常交付，不是 500。
"""


def create_app(db_path: Optional[str] = None) -> FastAPI:
    """构造应用。db_path 为空时用项目根目录的 checkpoints.sqlite。

    测试时传 `:memory:` 或临时文件，避免污染真实数据。
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # 启动：配置 Checkpointer 路径
        task_store.configure(db_path or os.getenv("TRAVEL_PLANNER_DB") or None)
        yield
        # 关闭：取消所有还在跑的后台任务，并**等它们真正退出**再关连接。
        # 只 cancel 不 await 的话，任务会在连接关闭之后才落到 except 分支，
        # 访问已关闭的 saver 又抛一次异常（"Task was destroyed but it is pending"）。
        pending = []
        for rec in list(task_store._tasks.values()):  # noqa: SLF001
            if rec.handle is not None and not rec.handle.done():
                rec.handle.cancel()
                pending.append(rec.handle)
        for h in pending:
            try:
                await h
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        # 释放常驻的 Checkpointer 连接（必须 await，否则 aiosqlite 线程泄漏）
        await task_store.close()

    app = FastAPI(
        title=TITLE,
        description=DESCRIPTION,
        version="0.1.0",
        lifespan=lifespan,
        openapi_tags=[
            {"name": "plan", "description": "规划任务的提交、查询、执行画像、流式事件"},
            {"name": "ops", "description": "运维接口"},
        ],
    )

    app.include_router(router, tags=["plan"])

    # ------------------------------------------------------------------ #
    # 统一错误处理：把 FastAPI/Pydantic 的默认错误也套进同一个信封
    # ------------------------------------------------------------------ #
    @app.exception_handler(RequestValidationError)
    async def _on_validation_error(request: Request, exc: RequestValidationError):
        """422：请求体不合法。把 Pydantic 的字段级错误摊平成前端能直接用的结构。"""
        details = []
        for e in exc.errors():
            loc = ".".join(str(x) for x in e.get("loc", []) if x != "body")
            details.append({"field": loc or "body", "reason": e.get("msg", ""), "type": e.get("type", "")})
        body = ApiResponse.failure(
            ErrorCode.VALIDATION_ERROR,
            f"请求参数校验失败（{len(details)} 处）",
            details,
        ).model_dump(mode="json")
        return JSONResponse(status_code=422, content=body)

    @app.exception_handler(ValidationError)
    async def _on_pydantic_error(request: Request, exc: ValidationError):
        body = ApiResponse.failure(
            ErrorCode.VALIDATION_ERROR, "数据校验失败", exc.errors()
        ).model_dump(mode="json")
        return JSONResponse(status_code=422, content=body)

    @app.exception_handler(Exception)
    async def _on_unhandled(request: Request, exc: Exception):
        """500：兜底。注意不要把 traceback 返回给前端（泄漏内部结构），只返回类型 + 简述。"""
        body = ApiResponse.failure(
            ErrorCode.INTERNAL_ERROR,
            f"{type(exc).__name__}: {exc}",
        ).model_dump(mode="json")
        return JSONResponse(status_code=500, content=body)

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("travel_planner.api.app:app", host="127.0.0.1", port=8000, reload=False)
