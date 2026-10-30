"""OpenAI 兼容 ChatModel 工厂：支持一键切换模型（仅改环境变量，不碰业务代码）。

读取环境变量（不写进 settings，避免改动已有配置）：
- OPENAI_API_KEY：API key（必填）
- OPENAI_BASE_URL：兼容接口地址，默认官方 https://api.openai.com/v1
- MODEL：模型名，默认 gpt-4o-mini

**关于 .env**：`find_dotenv(usecwd=True)` 会在"当前工作目录"里找 .env。
为什么不靠 pydantic-settings 的那个 .env：`Settings` 只声明了 amap_* 等字段且
`extra="ignore"`，所以写进 .env 的 OPENAI_API_KEY **不会**被它带进 os.environ
（实测会表现为"配了 key 却说 Missing credentials"）。这里显式加载，且
`override=False` 保证已有的真实环境变量优先。

真实调用路径由 supervisor 节点（及后续专家节点）通过 with_structured_output 使用。
本文件刻意保持最小；langchain_openai 在 get_chat_model() 内部才 import，
因此即使环境未装 langchain，本模块也能被 import（便于离线评测与打桩）。
"""
from __future__ import annotations

import os

DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_MODEL = "gpt-4o-mini"

_dotenv_loaded = False


def _load_dotenv_once() -> None:
    """从当前工作目录加载 .env（只做一次，不覆盖已有环境变量）。"""
    global _dotenv_loaded
    if _dotenv_loaded:
        return
    _dotenv_loaded = True
    try:
        from dotenv import find_dotenv, load_dotenv

        path = find_dotenv(usecwd=True)
        if path:
            load_dotenv(path, override=False)
    except Exception:  # noqa: BLE001 — 没装 dotenv / .env 坏了都不该阻碍启动
        pass


def get_chat_model(**kwargs):
    """返回一个 OpenAI 兼容的 ChatModel 实例（temperature=0，便于路由这类确定性决策）。"""
    from langchain_openai import ChatOpenAI

    _load_dotenv_once()
    return ChatOpenAI(
        model=os.getenv("MODEL", DEFAULT_MODEL),
        api_key=os.getenv("OPENAI_API_KEY", ""),
        base_url=os.getenv("OPENAI_BASE_URL", DEFAULT_BASE_URL),
        temperature=0,
        **kwargs,
    )


def llm_config_status() -> dict:
    """返回当前 LLM 配置状态（不含 key 明文），供健康检查 / 排错命令使用。"""
    _load_dotenv_once()
    key = os.getenv("OPENAI_API_KEY", "")
    base = os.getenv("OPENAI_BASE_URL", DEFAULT_BASE_URL)
    model = os.getenv("MODEL", DEFAULT_MODEL)
    return {
        "configured": bool(key),
        "key_hint": f"{key[:6]}...{key[-4:]}" if len(key) > 12 else ("<未设置>" if not key else "<过短>"),
        "base_url": base,
        "model": model,
        "provider_guess": (
            "deepseek" if "deepseek" in base
            else "openai" if "openai.com" in base
            else "other-compatible"
        ),
        "structured_output_method": resolve_structured_output_method(),
    }


# ---------------------------------------------------------------- 结构化输出
# 背景（实测踩坑）：langchain_openai 的 with_structured_output() 默认
# method="json_schema"，对应请求体
#   response_format={"type": "json_schema", "json_schema": {...}}
# 这是 OpenAI 的"严格模式"，要求服务端支持 JSON Schema 约束解码。
# 但 DeepSeek / 多数国产兼容接口**只实现了** {"type": "json_object"}
# （甚至连 function calling 的严格模式也不全），于是直接报
#   400 This response_format type is unavailable now
#
# 解决思路：把"用哪种结构化输出"做成可解析的策略，而不是散落在各节点里写死。
#   - 显式覆盖：环境变量 STRUCTURED_OUTPUT_METHOD，值 json_schema / json_mode /
#     function_calling，用于特殊 provider 或做对照实验
#   - 自动判断：按 base_url 猜 provider，DeepSeek 及已知只支持 json_object 的
#     兼容接口一律走 json_mode
#   - 兜底：其余（含官方 OpenAI）保持 langchain 默认的 json_schema
#
# json_mode 的代价：model 只保证"输出是合法 JSON"，不再强制 schema，所以各节点
# 里那句 `if isinstance(x, dict): X.model_validate(x)` 的防御分支是必需的，
# 不能删——它就是从 json_mode 兜住解析错误的地方。
_JSON_MODE_PROVIDERS = ("deepseek", "moonshot", "dashscope", "aliyuncs", "bigmodel", "zhipu")

STRUCTURED_OUTPUT_METHODS = ("json_schema", "json_mode", "function_calling")


def resolve_structured_output_method() -> str:
    """决定 `with_structured_output` 用哪种 method（见上方注释）。"""
    override = (os.getenv("STRUCTURED_OUTPUT_METHOD") or "").strip().lower()
    if override in STRUCTURED_OUTPUT_METHODS:
        return override
    base = (os.getenv("OPENAI_BASE_URL", DEFAULT_BASE_URL) or "").lower()
    if any(p in base for p in _JSON_MODE_PROVIDERS):
        return "json_mode"
    return "json_schema"


def structured_model(schema, **kwargs):
    """`model.with_structured_output(schema)` 的兼容版本，自动选对 method。

    用法与原 API 完全一致：把
        structured = get_chat_model().with_structured_output(RoutingDecision)
    换成
        structured = structured_model(RoutingDecision)
    """
    model = get_chat_model(**kwargs)
    method = resolve_structured_output_method()
    if method == "json_schema":
        # 走默认路径，保持对官方 OpenAI 的严格模式支持
        return model.with_structured_output(schema)
    if method == "json_mode":
        # json_mode 下 langchain 会在 prompt 末尾追加"必须输出 JSON"的指令，
        # 并用 schema 做一次 parse + validate
        return model.with_structured_output(schema, method="json_mode")
    return model.with_structured_output(schema, method="function_calling")
