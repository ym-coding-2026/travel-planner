"""离线评测 / 演示用：固定 LLM 输出的 stub 模型。

真实运行时 `travel_planner.agents.*` 模块通过 `get_llm()` 拿 ChatModel；
这里用 `monkeypatch` 把 `get_llm` 换成返回 `StubLLM`，这样整条链路
不依赖外网和 API Key，跑得快、结果稳定、可断言。

为什么不用 `FakeListChatModel`：项目里每个节点用的是 `with_structured_output`
（绑定 JSON Schema 的 ChatModel），Fake 系列不支持，只能自定义一个最小实现。
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Sequence, Type

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult


class StubRunnable:
    """`with_structured_output` 的返回值：直接吐预设的 dict。"""

    def __init__(self, payload: Any) -> None:
        self._payload = payload

    def invoke(self, *args: Any, **kwargs: Any) -> Any:
        return self._payload

    async def ainvoke(self, *args: Any, **kwargs: Any) -> Any:
        return self._payload

    def __or__(self, other: Any) -> Any:  # 让 `prompt | model` 仍能用
        from langchain_core.runnables import RunnableLambda

        return RunnableLambda(lambda _: self.invoke()) | other


class StubLLM(BaseChatModel):
    """按调用顺序返回预设 JSON 的假模型。

    `responses` 是一个队列：每次 invoke 弹一个；弹空后重复最后一个。
    这样多轮/多 Agent 调用都能覆盖，且不需要精确计数。
    """

    responses: List[Any] = []
    calls: int = 0
    _cursor: int = 0

    @property
    def _llm_type(self) -> str:
        return "stub-llm"

    def _next(self) -> Any:
        self.calls += 1
        if not self.responses:
            return {}
        idx = min(self._cursor, len(self.responses) - 1)
        self._cursor += 1
        return self.responses[idx]

    def _to_result(self, payload: Any) -> ChatResult:
        text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=text))])

    def _generate(
        self,
        messages: List[BaseMessage],
        stop: Optional[List[str]] = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        return self._to_result(self._next())

    def with_structured_output(self, schema: Any = None, **kwargs: Any):  # noqa: ANN201
        payload = self._next()
        if schema is not None and hasattr(schema, "model_validate") and isinstance(payload, dict):
            try:
                payload = schema.model_validate(payload)
            except Exception:  # noqa: BLE001 — 校验失败就用原始 dict，让节点自己兜
                pass
        return StubRunnable(payload)


# --------------------------------------------------------------------------- #
# 各节点的标准 stub 回应
# --------------------------------------------------------------------------- #
def supervisor_route(action: str = "route", next_agent: str = "poi_agent") -> Dict[str, Any]:
    return {
        "action": action,
        "next_agent": next_agent,
        "reason": "stub: 按流水线推进",
        "instruction": "",
    }


def poi_select(poi_ids: Sequence[str]) -> Dict[str, Any]:
    return {
        "selected": list(poi_ids),
        "reason": "stub: 全选候选",
        "notes": "",
    }


def route_plan(segments: Optional[List[dict]] = None) -> Dict[str, Any]:
    return {"segments": segments or [], "reason": "stub: 沿用算法分段"}


def budget_report() -> Dict[str, Any]:
    return {
        "items": [],
        "total_estimated": 0.0,
        "within_budget": True,
        "advice": "stub: 预算充足",
    }


def review_result(passed: bool = True) -> Dict[str, Any]:
    return {
        "passed": passed,
        "score": 100 if passed else 60,
        "checks": [],
        "issues": [],
        "reason": "stub: 复核通过",
    }


def itinerary_text(destination: str = "北京", days: int = 3) -> Dict[str, Any]:
    return {
        "title": f"{destination} {days} 日游（stub）",
        "overview": "由 stub 模型生成的行程说明。",
        "highlights": ["离线评测"],
    }


_JSON_RE = re.compile(r"\{.*\}", re.S)


def parse_json(text: str) -> Dict[str, Any]:
    """从 LLM 文本里抠 JSON（兼容 ```json 包裹、前后废话）。"""
    m = _JSON_RE.search(text or "")
    if not m:
        return {}
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return {}


def make_stub(responses: List[Any]) -> StubLLM:
    return StubLLM(responses=responses, calls=0, _cursor=0)
