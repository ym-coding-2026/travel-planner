"""图装配：StateGraph + 节点 + 条件边 + Checkpointer。

图拓扑（详见 mermaid 输出）：

    START → supervisor
    supervisor ──(routing.next)──► poi / route / budget / itinerary / review / end
    budget ──(超支 & 有额度)──► reselect_guard ──► poi_agent
           └─(其它)─────────► supervisor
    review ──(pass)──────────► END
           ├─(fail & 有额度)─► repair_guard ──► poi / route（按失败项）
           └─(fail & 用尽)────► fallback → END
    各专家 ──► supervisor（回到大脑重新决策）

Checkpointer（关键，容易踩坑）：
- 本项目所有节点都是 `async def`，因此**必须用异步入口**（`ainvoke` / `astream`）。
  但 `SqliteSaver`（同步版）**不支持 `ainvoke`**，会抛
  `NotImplementedError: The SqliteSaver does not support async methods`。
  所以真正跑流程要用 `AsyncSqliteSaver`（依赖 aiosqlite）。
- 本模块同时提供两套构造：
  * `make_async_checkpointer()` → `AsyncSqliteSaver`（**推荐，跑图用这个**）
  * `make_checkpointer()` → 同步 `SqliteSaver`（只适合纯同步场景 / 读状态）
- 每个 thread_id = 一次规划会话。同 thread_id 重入即从断点恢复；不同 thread_id 互不干扰。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Optional, Tuple

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END as LG_END
from langgraph.graph import START, StateGraph

from ..state import GraphState
from . import edges
from .nodes.budget import budget_agent_node
from .nodes.fallback import fallback_node
from .nodes.guards import repair_guard_node, reselect_guard_node
from .nodes.itinerary import itinerary_agent_node
from .nodes.poi import poi_agent_node
from .nodes.review import review_agent_node
from .nodes.route import route_agent_node
from .nodes.supervisor import supervisor_node


def _default_db_path() -> str:
    """默认 checkpoint 数据库路径（项目根目录下）。"""
    return str(Path(__file__).resolve().parents[3] / "checkpoints.sqlite")


def _allowed_msgpack_modules() -> tuple[tuple[str, ...], ...]:
    """checkpoint 反序列化白名单：本项目所有会写进 state 的 Pydantic 模型。

    为什么需要：LangGraph 用 msgpack 序列化 checkpoint。1.x 起，反序列化任何
    **非内置类型**都会打一条 `Deserializing unregistered type ...` 警告，并说明
    "将来会默认阻断"。根因是安全考虑——checkpoint 库被写入恶意数据时可以借
    反序列化执行代码。正确做法不是关掉警告，而是显式声明"这些类型是我自己的、
    可信的"。配上 `LANGGRAPH_STRICT_MSGPACK=true` 后仍能正常工作。
    """
    from ..models.budget import BudgetCategory, BudgetReport
    from ..models.common import GeoPoint, TransportMode
    from ..models.plan import DayPlan, Itinerary, ScheduledPOI
    from ..models.poi import POI, RouteSegment
    from ..models.requests import TransportPref, UserRequest
    from ..models.review import CheckResult, Issue, ReviewResult
    from ..models.routing import NextStep, RoutingDecision
    from ..state import StepRecord

    classes = (
        UserRequest, TransportPref,
        GeoPoint, TransportMode,
        POI, RouteSegment,
        ScheduledPOI, DayPlan, Itinerary,
        BudgetCategory, BudgetReport,
        Issue, CheckResult, ReviewResult,
        NextStep, RoutingDecision,
        StepRecord,
    )
    return tuple((c.__module__, c.__name__) for c in classes)


def _make_serde():
    """构造带白名单的序列化器（msgpack + json 双白名单）。"""
    from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

    allow = _allowed_msgpack_modules()
    return JsonPlusSerializer(allowed_msgpack_modules=allow, allowed_json_modules=allow)


def make_checkpointer(
    db_path: Optional[str] = None,
) -> Tuple[SqliteSaver, sqlite3.Connection]:
    """创建**同步** SqliteSaver。返回 (saver, conn)，调用方负责 conn.close()。

    注意：同步 saver 不能配 async 节点使用（会 NotImplementedError）。
    跑图请用 make_async_checkpointer()。
    """
    path = db_path or _default_db_path()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    return SqliteSaver(conn, serde=_make_serde()), conn


async def make_async_checkpointer(db_path: Optional[str] = None):
    """创建**异步** AsyncSqliteSaver（跑图用这个）。

    两个坑：
    1. `AsyncSqliteSaver.from_conn_string()` 返回的是**异步上下文管理器**，不能 `await`
       它（否则 `TypeError: object _AsyncGeneratorContextManager can't be used in
       'await' expression`）。这里改用 `aiosqlite.connect()` 手动建连接，
       顺便能传 serde。
    2. 它的 `__init__` 读 `asyncio.get_running_loop()`，所以必须在**事件循环内**创建
       （不能在模块导入时创建）。这也正是把它做成 async 函数的原因。
    """
    import aiosqlite

    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    path = db_path or _default_db_path()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = await aiosqlite.connect(path)
    return AsyncSqliteSaver(conn, serde=_make_serde())


def build_graph(
    checkpointer: Optional[SqliteSaver] = None,
    db_path: Optional[str] = None,
    interrupt_before: Optional[list[str]] = None,
):
    """装配并编译图。

    参数：
        checkpointer：外部传入的 saver（测试里用内存库/临时库）。
        db_path：未传 checkpointer 时，用它创建 SqliteSaver。
        interrupt_before：人工介入断点（如 ["poi_agent"]），用于演示断点恢复。

    返回：编译后的图（CompiledStateGraph）。若内部创建了 saver，会把连接挂在
    `graph._tp_conn` 上，便于调用方 close。
    """
    own_conn: Optional[sqlite3.Connection] = None
    if checkpointer is None:
        checkpointer, own_conn = make_checkpointer(db_path)

    g = StateGraph(GraphState)

    # --- 节点 ---
    g.add_node(edges.SUPERVISOR, supervisor_node)
    g.add_node(edges.POI, poi_agent_node)
    g.add_node(edges.ROUTE, route_agent_node)
    g.add_node(edges.BUDGET, budget_agent_node)
    g.add_node(edges.ITINERARY, itinerary_agent_node)
    g.add_node(edges.REVIEW, review_agent_node)
    g.add_node(edges.FALLBACK, fallback_node)
    # guard 节点：只记账，不规划
    g.add_node(edges.RESELECT_GUARD, reselect_guard_node)
    g.add_node(edges.REPAIR_GUARD, repair_guard_node)

    # --- 入口 ---
    g.add_edge(START, edges.SUPERVISOR)

    # --- supervisor 的条件分派（中心化路由）---
    g.add_conditional_edges(
        edges.SUPERVISOR,
        edges.route_from_supervisor,
        {
            edges.POI: edges.POI,
            edges.ROUTE: edges.ROUTE,
            edges.BUDGET: edges.BUDGET,
            edges.ITINERARY: edges.ITINERARY,
            edges.REVIEW: edges.REVIEW,
            edges.END: LG_END,
        },
    )

    # --- 专家执行完回到 supervisor（重入式编排）---
    g.add_edge(edges.POI, edges.SUPERVISOR)
    g.add_edge(edges.ROUTE, edges.SUPERVISOR)
    g.add_edge(edges.ITINERARY, edges.SUPERVISOR)

    # --- 预算回退边（硬约束 2 / 3）---
    g.add_conditional_edges(
        edges.BUDGET,
        edges.route_after_budget,
        {
            edges.RESELECT_GUARD: edges.RESELECT_GUARD,
            edges.SUPERVISOR: edges.SUPERVISOR,
        },
    )
    # guard 记账后回 POI 重选
    g.add_edge(edges.RESELECT_GUARD, edges.POI)

    # --- review 修复路由 ---
    g.add_conditional_edges(
        edges.REVIEW,
        edges.route_after_review,
        {
            # 键必须是条件函数的返回值（"end"），值才是真正的图端点（"__end__"）
            edges.END: LG_END,
            edges.REPAIR_GUARD: edges.REPAIR_GUARD,
            edges.FALLBACK: edges.FALLBACK,
        },
    )
    # guard 记账后再决定具体回哪个专家
    g.add_conditional_edges(
        edges.REPAIR_GUARD,
        edges.route_after_repair_guard,
        {
            edges.POI: edges.POI,
            edges.ROUTE: edges.ROUTE,
            edges.FALLBACK: edges.FALLBACK,
        },
    )

    # --- 出口 ---
    g.add_edge(edges.FALLBACK, LG_END)

    graph = g.compile(
        checkpointer=checkpointer,
        interrupt_before=interrupt_before,
    )
    if own_conn is not None:
        graph._tp_conn = own_conn  # type: ignore[attr-defined]
    return graph


def mermaid(graph=None, db_path: Optional[str] = None) -> str:
    """返回图的 mermaid 源码（可直接贴到 mermaid.live 检查控制流）。

    未传 graph 时临时装配一张用 InMemorySaver 的图：画图只需要拓扑，
    不落盘，也避免多开一个 sqlite 连接。
    """
    if graph is not None:
        return graph.get_graph().draw_mermaid()
    from langgraph.checkpoint.memory import InMemorySaver

    g = build_graph(checkpointer=InMemorySaver(), db_path=db_path)
    return g.get_graph().draw_mermaid()


async def plan(
    initial_state: dict,
    thread_id: str,
    db_path: Optional[str] = None,
    recursion_limit: int = 80,
) -> dict:
    """一行跑完一次规划会话（便捷入口，给 API 层和脚本用）。

    - 自动创建 AsyncSqliteSaver（节点是 async，必须用异步 saver）
    - thread_id 即会话 ID：同一个 thread_id 重入会从断点恢复
    - recursion_limit 是防死循环的保险丝：超限抛 GraphRecursionError 而非无限跑
    """
    saver = await make_async_checkpointer(db_path)
    try:
        graph = build_graph(checkpointer=saver)
        cfg = {
            "configurable": {"thread_id": thread_id},
            "recursion_limit": recursion_limit,
        }
        return await graph.ainvoke(initial_state, cfg)
    finally:
        await saver.conn.close()
