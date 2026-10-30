"""任务运行时：内存任务表 + 后台执行 + 事件广播。

为什么需要这一层（设计判断，面试重点）：
- `POST /plan` 要**立刻**返回 task_id，规划在后台跑。FastAPI 的 `BackgroundTasks`
  在响应返回后才执行，且**不能中途推送进度**；`asyncio.create_task` 可以，但有一个坑：
  Starlette/FastAPI 的请求协程结束后，事件循环仍会跑 task，但如果 task 内部捕获到
  "事件循环被关闭"就会静默丢失结果。所以这里显式管理 task 引用（强引用，防止
  GC 提前回收）+ 把异常全部兜住写进任务记录，绝不留下"永远 running"的僵尸任务。

- **状态的真实来源是 Checkpointer，不是内存**。内存里的 `TaskRecord` 只存
  "运行时元信息"（啥时候开始的、task 句柄、异常栈）；规划结果一律通过
  `graph.aget_state(thread_id)` 从 SqliteSaver 读。这样进程重启后，
  结果仍然可查（只要 task_id 能从别处记住）—— 这才是 Checkpointer 的意义。

- **事件广播用 asyncio.Queue 扇出**：SSE 接口订阅时拿一个新 Queue，
  执行器每完成一个节点就往所有订阅者 put 一次。队列有界 + 满了丢最旧，
  避免没人订阅时把内存撑爆（慢消费者问题）。
"""
from __future__ import annotations

import asyncio
import time
import traceback
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from ..graph.builder import build_graph, make_async_checkpointer
from ..models.requests import UserRequest
from ..state import GraphState
from .schemas import PlanTaskStatus

# 每个任务最多缓存多少个事件（防止无人消费时无限增长）
_MAX_EVENTS_PER_TASK = 200
# 单次规划的递归上限（防死循环的保险丝，与 builder.plan 保持一致）
DEFAULT_RECURSION_LIMIT = 80


class TaskRecord:
    """一个规划任务的运行时记录（**不含规划结果**，结果在 Checkpointer 里）。"""

    def __init__(self, task_id: str, request: UserRequest) -> None:
        self.task_id = task_id
        self.request = request
        self.status: PlanTaskStatus = PlanTaskStatus.QUEUED
        self.created_at: float = time.time()
        self.started_at: Optional[float] = None
        self.finished_at: Optional[float] = None
        self.error: Optional[str] = None
        self.traceback: Optional[str] = None
        # 浏览器/前端断线重连时，把已发生的事件回放给它
        self.events: List[dict] = []
        # 活跃的 SSE 订阅者队列
        self.subscribers: List[asyncio.Queue] = []
        # 强引用 asyncio.Task，防止被 GC 提前回收
        self.handle: Optional[asyncio.Task] = None
        # 节点耗时：节点名 → 该节点每次调用的毫秒数（追加）
        self.node_latencies: Dict[str, List[float]] = {}

    @property
    def elapsed_ms(self) -> Optional[float]:
        """已耗时。跑完就是总耗时；在跑就是"到现在为止"。"""
        if self.started_at is None:
            return None
        end = self.finished_at or time.time()
        return round((end - self.started_at) * 1000, 2)

    def publish(self, event: dict) -> None:
        """把事件写入历史 + 推给所有订阅者。队列满时丢最旧（保护内存）。"""
        self.events.append(event)
        if len(self.events) > _MAX_EVENTS_PER_TASK:
            self.events = self.events[-_MAX_EVENTS_PER_TASK:]
        for q in list(self.subscribers):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                # 慢消费者：丢掉它最旧的一条，保证最新事件能进
                try:
                    q.get_nowait()
                    q.put_nowait(event)
                except Exception:  # noqa: BLE001
                    pass

    def finish(self, status: PlanTaskStatus, error: Optional[str] = None) -> None:
        self.status = status
        self.finished_at = time.time()
        if error:
            self.error = error
        # 发一个终止事件，让 SSE 客户端知道可以断开
        self.publish(
            {
                "type": "task_end",
                "task_id": self.task_id,
                "status": status.value,
                "elapsed_ms": self.elapsed_ms,
                "error": error,
            }
        )
        # 关闭所有订阅者队列（塞哨兵 None）
        for q in list(self.subscribers):
            try:
                q.put_nowait(None)
            except asyncio.QueueFull:
                pass
        self.subscribers.clear()


class TaskStore:
    """内存任务表 + 常驻 Checkpointer 连接。生产环境应换成 Redis + Postgres，接口保持一样即可。"""

    def __init__(self) -> None:
        self._tasks: Dict[str, TaskRecord] = {}
        self._db_path: Optional[str] = None
        self._lock: Optional[asyncio.Lock] = None
        # 构造常驻 graph 用的锁（与 _lock 分开：_lock 保护任务表）
        self._build_lock: Optional[asyncio.Lock] = None
        # 常驻 saver：整个应用共用一个连接，避免每次查询新建连接
        self._saver: Any = None
        self._graph: Any = None

    def configure(self, db_path: Optional[str] = None) -> None:
        """设置 Checkpointer 的 sqlite 路径（测试时每个用例一个临时文件）。

        这里**必须**把缓存的 saver/graph 置空，原因不是"换路径"，而是
        **事件循环变了**：
        - 每个 pytest 用例有自己的 loop，`app.router.lifespan_context` 退出时
          `close()` 已经把连接关了；
        - 如果这里不清空，下一个用例沿用上一个用例的 graph → 连接已关
          （`sqlite3.ProgrammingError`）或 `future attached to a different loop`。
        - 另外 `AsyncSqliteSaver` 每次 `aget_tuple` 都会先 `setup()`（CREATE TABLE
          走 executescript），旧连接残留的未提交事务会让新连接 `database is locked`。

        一句话：**连接的生命周期绑定在应用 lifespan 上，configure 是 lifespan 的
        第一步，所以它负责清场**。
        """
        self._db_path = db_path
        self._saver = None
        self._graph = None
        # 锁也要重建：`asyncio.Lock()` 在**创建时**绑定当前事件循环，
        # 跨 loop 复用会抛 "is bound to a different event loop"。
        # 模块级单例在 import 时还没 loop（或绑在别的 loop 上），所以一律惰性创建。
        self._lock = None
        self._build_lock = None

    def _locks(self) -> Tuple[asyncio.Lock, asyncio.Lock]:
        """惰性创建两把锁（必须在运行中的事件循环里调用）。"""
        if self._lock is None:
            self._lock = asyncio.Lock()
        if self._build_lock is None:
            self._build_lock = asyncio.Lock()
        return self._lock, self._build_lock

    async def _ensure_graph(self):
        """懒加载常驻图与 saver（并发安全）。

        为什么必须共用一条连接（`database is locked` 的根因）：
        `AsyncSqliteSaver` 每次 `aget_tuple` 都会先 `setup()`，而 `setup()` 走的是
        `executescript(CREATE TABLE ...)` —— 这会**隐式提交并取得写锁**。
        如果查询接口各自新建连接，而执行器正持有写事务，第二条连接必然撞锁。

        为什么加了 `self._build_lock`：两个请求同时进来（一个查 status、一个在
        跑规划）会同时看到 `_graph is None`，各建一条连接 → 又变成多连接。
        锁住构造过程即可，构造之后是只读复用。
        """
        if self._graph is not None:
            return self._graph
        _, build_lock = self._locks()
        async with build_lock:
            if self._graph is None:
                saver = await make_async_checkpointer(self._db_path)
                self._saver = saver
                self._graph = build_graph(checkpointer=saver)
        return self._graph

    async def close(self) -> None:
        """应用关闭时释放连接。

        必须 `await conn.close()`：aiosqlite 的每个连接背后有一个工作线程，
        只是丢引用不会停线程。事件循环关闭后，那个线程再往 loop 里塞结果就会抛
        `RuntimeError: Event loop is closed`（pytest 会把它报成
        PytestUnhandledThreadExceptionWarning）。关连接 = 给线程发退出信号。
        """
        if self._saver is not None:
            try:
                await self._saver.conn.close()
            except Exception:  # noqa: BLE001
                pass
        self._saver = None
        self._graph = None

    async def aclose_task(self, task_id: str) -> bool:
        """取消一个还在跑的任务。返回是否真的取消了。

        给"用户反悔 / 前端关页面"用；lifespan 关闭时也复用这段。
        """
        rec = self._tasks.get(task_id)
        if rec is None or rec.handle is None or rec.handle.done():
            return False
        rec.handle.cancel()
        return True

    async def create(self, request: UserRequest) -> TaskRecord:
        lock, _ = self._locks()
        async with lock:
            task_id = uuid.uuid4().hex[:12]
            rec = TaskRecord(task_id, request)
            self._tasks[task_id] = rec
            return rec

    def get(self, task_id: str) -> Optional[TaskRecord]:
        return self._tasks.get(task_id)

    def clear(self) -> None:
        """测试用：清空任务表。"""
        self._tasks.clear()

    # ------------------------------------------------------------------ #
    # 后台执行
    # ------------------------------------------------------------------ #
    def start(self, rec: TaskRecord) -> None:
        """启动后台规划。返回后立刻可用 rec.task_id 查询。

        用 asyncio.create_task 而不是 BackgroundTasks：BackgroundTasks 在响应
        发送后才执行，且无法中途广播事件，做不了 SSE 进度推送。
        """
        rec.handle = asyncio.create_task(self._run(rec))

    async def _run(self, rec: TaskRecord) -> None:
        """真正的执行体。所有异常都在这里兜住，绝不让任务永远卡在 running。"""
        rec.status = PlanTaskStatus.RUNNING
        rec.started_at = time.time()
        rec.publish(
            {
                "type": "task_start",
                "task_id": rec.task_id,
                "request": rec.request.model_dump(mode="json"),
                "ts": datetime.now().isoformat(timespec="seconds"),
            }
        )

        try:
            # 常驻共享连接：后台执行器与查询接口复用同一条 saver，
            # 避免 sqlite 多连接写锁冲突（database is locked）。
            graph = await self._ensure_graph()

            initial: GraphState = {
                "request": rec.request,
                "pois": [],
                "segments": [],
                "reselect_count": 0,
                "repair_count": 0,
                "excluded_poi_ids": list(rec.request.excluded_poi_ids),
                "errors": [],
                "trace": [],
            }
            config = {
                "configurable": {"thread_id": rec.task_id},
                "recursion_limit": DEFAULT_RECURSION_LIMIT,
            }

            # 关键：逐个节点推进并计时。
            # stream_mode="updates" 每完成一个节点就 yield {节点名: 该节点写入的字段}，
            # 这正是 SSE 要推的"节点完成事件"，也正好能算节点耗时。
            prev_ts = time.time()
            async for chunk in graph.astream(initial, config, stream_mode="updates"):
                now = time.time()
                latency_ms = round((now - prev_ts) * 1000, 2)
                prev_ts = now

                for node_name, payload in chunk.items():
                    payload = payload or {}
                    step = _extract_step(node_name, payload)
                    rec.node_latencies.setdefault(node_name, []).append(latency_ms)
                    rec.publish(
                        {
                            "type": "node_end",
                            "task_id": rec.task_id,
                            "node": node_name,
                            "action": step["action"],
                            "status": step["status"],
                            "latency_ms": latency_ms,
                            "errors": step["errors"],
                            "partial": bool(payload.get("partial")),
                            "ts": datetime.now().isoformat(timespec="seconds"),
                        }
                    )

            # 收尾：从 Checkpointer 读最终状态，判定 completed / partial / failed。
            #
            # 这里有三条判据，缺一不可（都是实测踩出来的）：
            # 1. partial 标记 / fallback 节点跑过 → partial（走了兜底，结果不全）
            # 2. **没有 itinerary 就不算 completed**：图上 END 有好几个入口
            #    （supervisor 认为该结束、降级 abort 等），都可能在没有产出任何行程时
            #    正常收尾。若不检查，就会返回 200 completed + 空行程 —— 比 500 更糟，
            #    因为调用方会以为规划成功了。
            # 3. 一条成功的规划必然经过 poi_agent 且产出了 pois；否则视为失败。
            snapshot = await graph.aget_state(config)
            values = snapshot.values or {}
            seen_nodes = {
                t.get("agent") if isinstance(t, dict) else getattr(t, "agent", "")
                for t in (values.get("trace") or [])
            }
            # 只把"真正有信息量"的错误作为失败原因，过滤掉历史堆积里的重复项
            errs = [str(e) for e in (values.get("errors") or [])]

            has_itinerary = values.get("itinerary") is not None
            has_pois = bool(values.get("pois"))

            if not has_itinerary or not has_pois:
                reason = _no_deliverable_reason(values, errs)
                rec.finish(PlanTaskStatus.FAILED, error=reason)
            elif bool(values.get("partial")) or values.get("status") == "partial" \
                    or "fallback" in seen_nodes:
                rec.finish(PlanTaskStatus.PARTIAL)
            else:
                rec.finish(PlanTaskStatus.COMPLETED)

        except asyncio.CancelledError:
            rec.finish(PlanTaskStatus.FAILED, error="task cancelled")
            raise
        except Exception as e:  # noqa: BLE001 — 兜底，绝不留僵尸任务
            name = type(e).__name__
            msg = f"{name}: {e}"
            if "Recursion" in name or "recursion" in msg.lower():
                msg = f"RECURSION_LIMIT: 规划步数超限（{DEFAULT_RECURSION_LIMIT}），疑似控制流异常"
            rec.traceback = traceback.format_exc()
            rec.finish(PlanTaskStatus.FAILED, error=msg)

    # ------------------------------------------------------------------ #
    # 读状态（真实来源是 Checkpointer）
    # ------------------------------------------------------------------ #
    async def read_state(self, task_id: str) -> Optional[Dict[str, Any]]:
        """从 Checkpointer 读该会话的最新 state。读不到返回 None。

        走的是与应用共享的常驻 saver，因此不依赖后台任务是否还在跑。
        """
        graph = await self._ensure_graph()
        snapshot = await graph.aget_state({"configurable": {"thread_id": task_id}})
        if not snapshot or not snapshot.values:
            return None
        return dict(snapshot.values)

    async def subscribe(self, rec: TaskRecord) -> Tuple[List[dict], asyncio.Queue]:
        """订阅事件流。返回 (历史事件, 新事件队列) —— 先回放历史，再听新的。"""
        q: asyncio.Queue = asyncio.Queue(maxsize=_MAX_EVENTS_PER_TASK)
        history = list(rec.events)
        if rec.status in (
            PlanTaskStatus.COMPLETED,
            PlanTaskStatus.PARTIAL,
            PlanTaskStatus.FAILED,
        ):
            # 已经结束：回放完直接塞哨兵，调用方读完就断开
            q.put_nowait(None)
        else:
            rec.subscribers.append(q)
        return history, q

    def unsubscribe(self, rec: TaskRecord, q: asyncio.Queue) -> None:
        if q in rec.subscribers:
            rec.subscribers.remove(q)


def _no_deliverable_reason(values: Dict[str, Any], errs: List[str]) -> str:
    """图跑完了但没产出可交付行程时，给一句人话解释。

    优先用真实错误（比如缺 API Key），而不是笼统的"未产出行程"——
    否则用户看到 failed 却不知道去配什么。
    """
    for e in errs:
        if e.startswith("partial_result:"):
            continue
        return f"规划未产出可交付行程：{e}"
    if not values.get("pois"):
        return "规划未产出可交付行程：未选出任何 POI（可能是 LLM 不可用或候选池为空）"
    return "规划未产出可交付行程：行程构建阶段未完成"


def _extract_step(node_name: str, payload: dict) -> Dict[str, Any]:
    """从节点写入的字段里提取"这一步做了什么"。

    节点写回的是 {"trace": [新增的 StepRecord], "errors": [...]}。

    **坑（实测踩到）**：`stream_mode="updates"` 给的是该节点**原始返回值**，
    还没经过 state 的 reducer 归约——所以 payload["trace"] 只含本节点新加的那一条，
    `[-1]` 就是它。但如果哪天 payload 变成归约后的全量 trace，`[-1]` 就会错拿
    别的节点的记录。所以这里加一层"按 agent 名匹配，匹配不上才退回最后一条"。
    没有 trace 的节点（如 guard）用写入的字段名拼一个可读描述。
    """
    traces = payload.get("trace") or []
    errors = [str(e) for e in (payload.get("errors") or [])]

    last = None
    for t in traces:
        agent = getattr(t, "agent", None) or (t.get("agent") if isinstance(t, dict) else None)
        if agent == node_name:
            last = t
    if last is None and traces:
        last = traces[-1]

    if last is not None:
        if hasattr(last, "action"):
            return {"action": last.action, "status": last.status, "errors": errors}
        return {
            "action": last.get("action", ""),
            "status": last.get("status", ""),
            "errors": errors,
        }
    # guard 节点等没有 trace 的：列出它写了哪些字段
    keys = [k for k in payload.keys() if k not in ("errors",)]
    return {"action": "update:" + ",".join(sorted(keys)), "status": "ok", "errors": errors}


# 全局单例（FastAPI 应用生命周期内共享）
task_store = TaskStore()
