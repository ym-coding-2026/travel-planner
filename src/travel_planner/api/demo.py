"""FastAPI 层骨架：`python -m travel_planner.api.demo <cmd>`。

为什么要有这个脚本：`_live.py`（我之前写的临时验证脚本）踩了一个**极隐蔽**的坑，
排查了很久才定位：

> 本机环境变量 `HTTP_PROXY` / `HTTPS_PROXY` 指向一个本地代理（沙箱/系统代理）。
> httpx 默认 `trust_env=True`，会读取这两个变量。于是**发往 127.0.0.1 的请求
> 也会被交给代理**，代理把 `/plan/abc/stream` 这种路径当作"目标站点"改写，
> 导致服务端收到的 `scope["path"]` 变成 `http://127.0.0.1:8799/plan/abc/stream`，
> Starlette 自然 404。而 `/healthz` 恰好被放行，所以表面上"服务是好的"。

两个要点：
1. 访问本地服务必须 `httpx.Client(trust_env=False)`，否则请求根本不直达进程。
2. `Client.send()` 返回的 `Response` 不支持 `with`（需 `resp.close()`），
   读流请用 `client.stream(...)` 上下文管理器。

所以把验证逻辑固化成这个脚本，以后想复验"真实 HTTP 端到端"直接跑它。
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from typing import Any, Dict, List

import httpx

BEIJING: Dict[str, Any] = {
    "destination": "北京",
    "start_date": "2026-11-01",
    "days": 3,
    "travelers": 1,
    "total_budget": 3000,
    "preferences": ["历史", "美食"],
    "transport_pref": "public",
}


def _client(base: str) -> httpx.Client:
    """连本地服务的客户端。

    `trust_env=False` 是关键：不读 HTTP_PROXY/HTTPS_PROXY。否则某些环境下
    请求会被交给系统代理，服务端收到的 path 变成完整 URL → 全部 404。
    """
    return httpx.Client(base_url=base, timeout=120, trust_env=False)


def _start_server(db_path: str, host: str, port: int) -> threading.Thread:
    """后台线程起 uvicorn（真实端口，可被 curl / 浏览器访问）。"""
    import uvicorn

    from .app import create_app

    app = create_app(db_path)
    cfg = uvicorn.Config(app, host=host, port=port, log_level="warning")
    server = uvicorn.Server(cfg)
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    for _ in range(200):
        try:
            with _client(f"http://{host}:{port}") as c:
                if c.get("/healthz").status_code == 200:
                    break
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.05)
    return t


def _bar(title: str) -> None:
    print("=" * 74)
    print(title)
    print("=" * 74)


def _iter_sse(client: httpx.Client, url: str) -> None:
    """读 SSE 直到 task_end。用 client.stream（相对/绝对 URL 都行，但别用 send）。"""
    event = None
    with client.stream("GET", url, timeout=120) as resp:
        print(f"HTTP {resp.status_code}  content-type={resp.headers.get('content-type')}")
        if resp.status_code != 200:
            print("  body=" + resp.read().decode("utf-8", "replace")[:400])
            return
        for line in resp.iter_lines():
            if line.startswith("event:"):
                event = line.split(":", 1)[1].strip()
            elif line.startswith("data:"):
                p = json.loads(line.split(":", 1)[1].strip())
                t = p.get("type")
                if t == "task_start":
                    print(f"  [{event}] ▶ 开始 task={p['task_id']}")
                elif t == "node_end":
                    errs = p.get("errors") or []
                    suffix = f"  ⚠ {errs[0][:70]}" if errs else ""
                    print(
                        f"  [{event}] {p['node']:<16} {p['action']:<38} "
                        f"{p['latency_ms']:>8.2f}ms  {p['status']}{suffix}"
                    )
                elif t == "task_end":
                    print(f"  [{event}] ■ 结束 status={p['status']} 总耗时={p['elapsed_ms']}ms")
                    return
                else:
                    print(f"  [{event}] {json.dumps(p, ensure_ascii=False)[:160]}")


def run(host: str, port: int, db_path: str, budget: float) -> None:
    _start_server(db_path, host, port)
    base = f"http://{host}:{port}"
    body = dict(BEIJING)
    body["total_budget"] = budget

    with _client(base) as c:
        _bar("① POST /plan —— 立刻返回 task_id（202 Accepted）")
        r = c.post("/plan", json=body)
        print(f"HTTP {r.status_code}")
        print(json.dumps(r.json(), ensure_ascii=False, indent=2))
        tid = r.json()["data"]["task_id"]
        print()

        _bar("② GET /plan/{id}/stream —— SSE，节点完成即推送")
        _iter_sse(c, f"/plan/{tid}/stream")
        print()

        _bar("③ GET /plan/{id} —— 从 Checkpointer 读最终结果")
        r = c.get(f"/plan/{tid}")
        d = r.json()["data"]
        print(f"HTTP {r.status_code}  status={d['status']}  partial={d['partial']}  elapsed={d['elapsed_ms']}ms")
        if d["reasons"]:
            print("reasons:")
            for x in d["reasons"]:
                print(f"  - {x}")
        it = d.get("itinerary")
        if it:
            names = {p["poi_id"]: p["name"] for p in d.get("pois") or []}
            print(f"行程标题: {it.get('title')}")
            for day in it["days"]:
                slots = " → ".join(
                    f"{names.get(s['poi_id'], s['poi_id'])}({s['start_time']}-{s['end_time']})"
                    for s in day["schedule"]
                )
                print(f"  第{day['day_index'] + 1}天 {day['date']}: {slots}")
        b = d.get("budget")
        if b:
            print(f"预算: 估算 {b['total_estimated']} / 上限 {body['total_budget']}  within_budget={b['within_budget']}")
        rv = d.get("review")
        if rv:
            print(f"复核: passed={rv['passed']} score={rv['score']}")
            for ck in rv.get("checks") or []:
                print(f"  {'✔' if ck['passed'] else '✘'} {ck['name']}: {ck['detail'][:80]}")
        print()

        _bar("④ GET /plan/{id}/trace —— 每节点耗时（面试演示用）")
        tr = c.get(f"/plan/{tid}/trace").json()["data"]
        print(f"节点数={tr['node_count']}  总耗时={tr['total_elapsed_ms']}ms")
        print(f"调用次数={tr['node_calls']}")
        print(f"回退计数={tr['counts']}")
        print(f"{'seq':>3}  {'agent':<16} {'action':<40} {'latency':>10}  status")
        for s in tr["steps"]:
            lat = f"{s['node_latency_ms']:.2f}ms" if s["node_latency_ms"] is not None else "-"
            print(f"{s['seq']:>3}  {s['agent']:<16} {s['action']:<40} {lat:>10}  {s['status']}")
        print()

        _bar("⑤ GET /plan/nope —— 统一错误信封（404）")
        r = c.get("/plan/nope")
        print(f"HTTP {r.status_code}")
        print(json.dumps(r.json(), ensure_ascii=False, indent=2))
        print()

        _bar("⑥ POST /plan 非法参数 —— 422 字段级校验")
        bad = dict(body)
        bad["days"] = 0
        bad["total_budget"] = -1
        r = c.post("/plan", json=bad)
        print(f"HTTP {r.status_code}")
        print(json.dumps(r.json(), ensure_ascii=False, indent=2))
        print()

        _bar("⑦ GET /plan/{id}/stream 不存在 —— 404 信封")
        r = c.get("/plan/nope/stream")
        print(f"HTTP {r.status_code}  {json.dumps(r.json(), ensure_ascii=False)}")
        print()

        _bar("⑧ OpenAPI —— /docs 与 /openapi.json 可用")
        spec = c.get("/openapi.json").json()
        print(f"title={spec['info']['title']}  version={spec['info']['version']}")
        for path, ops in spec["paths"].items():
            print(f"  {path:<32} {','.join(m.upper() for m in ops)}")
        print()
        print("浏览器打开 http://127.0.0.1:8000/docs 可以看到可交互文档。")


def serve(host: str, port: int, db_path: str) -> None:
    """前台起服务（给 curl / 浏览器用）。"""
    import uvicorn

    from .app import create_app

    print(f"Travel Planner API → http://{host}:{port}")
    print(f"  docs  : http://{host}:{port}/docs")
    print(f"  db    : {db_path}")
    uvicorn.run(create_app(db_path), host=host, port=port, log_level="info")


def main(argv: List[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Travel Planner API 演示 / 自验")
    ap.add_argument("cmd", choices=["run", "serve"], help="run=起服务并自验全部接口；serve=前台起服务")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--db", default="demo_api.sqlite", help="Checkpointer sqlite 路径")
    ap.add_argument("--budget", type=float, default=3000, help="run 模式下提交的预算")
    a = ap.parse_args(argv)
    if a.cmd == "serve":
        serve(a.host, a.port, a.db)
    else:
        # run 模式端口默认换成 8799，避开常被占用的 8000
        port = a.port if a.port != 8000 else 8799
        run(a.host, port, a.db, a.budget)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
