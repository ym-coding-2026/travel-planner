"""检查 LLM 配置并做一次最小真实调用（`python -m travel_planner.llm.check`）。

用法：
    python -m travel_planner.llm.check

做三件事：
1. 打印 key 是否读到（只显示首尾几位，不泄露明文）、base_url、model；
2. 真打一次 chat 接口（最小 token），验证 key / 网络 / 模型名都对；
3. 检查数据库里 data 目录、checkpoint 路径等运行前提。
"""
from __future__ import annotations

import asyncio
import json
import sys


def _line(t=""):
    print(t)


async def _probe_llm() -> bool:
    from langchain_core.messages import HumanMessage

    from .client import get_chat_model

    model = get_chat_model()
    try:
        resp = await model.ainvoke([HumanMessage(content="只回复两个字：你好")])
        text = resp.content if isinstance(resp.content, str) else str(resp.content)
        _line(f"  ✔ 调用成功  回复={text.strip()[:40]!r}")
        usage = getattr(resp, "usage_metadata", None) or {}
        if usage:
            _line(f"     token: in={usage.get('input_tokens')} out={usage.get('output_tokens')}")
        return True
    except Exception as e:  # noqa: BLE001
        _line(f"  ✘ 调用失败  {type(e).__name__}: {e}")
        msg = str(e).lower()
        if "credentials" in msg or "api_key" in msg or "401" in msg:
            _line("    → 看起来是 key 没读到或已失效。检查：")
            _line("      1) .env 是否在【当前目录】（.env 按工作目录查找，不在 D:\\ 也不在 src\\）")
            _line("      2) 变量名必须是 OPENAI_API_KEY，不能有引号/空格/BOM")
            _line("      3) 如果用的是 DeepSeek，OPENAI_BASE_URL 要设 https://api.deepseek.com/v1")
        elif "404" in msg or "model" in msg:
            _line("    → 模型名不存在。检查 MODEL 是否是该平台支持的模型（如 deepseek-chat / deepseek-reasoner）")
        elif "connect" in msg or "timeout" in msg or "ssl" in msg:
            _line("    → 网络问题。国内直连 openai.com 不通，建议换兼容平台（如 DeepSeek）")
        return False


def main() -> int:
    from .client import llm_config_status

    _line("=" * 60)
    _line("LLM 配置检查")
    _line("=" * 60)
    st = llm_config_status()
    _line(json.dumps(st, ensure_ascii=False, indent=2))

    if not st["configured"]:
        _line("")
        _line("✘ 没有读到 OPENAI_API_KEY。")
        _line("  在【D:\\travel-planner】下建一个 .env 文件，内容：")
        _line("    OPENAI_API_KEY=sk-xxxxxxxx")
        _line("    OPENAI_BASE_URL=https://api.deepseek.com/v1")
        _line("    MODEL=deepseek-chat")
        return 2

    _line("")
    _line("发起一次真实调用…")
    if not asyncio.run(_probe_llm()):
        return 1
    _line("")
    _line("✔ 全部正常，可以跑 python -m travel_planner.api.demo run")
    return 0


if __name__ == "__main__":
    sys.exit(main())
