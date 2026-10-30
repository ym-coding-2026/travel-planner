"""跑全项目测试，输出 UTF-8 落盘。"""
from __future__ import annotations

import contextlib
import io
import sys
import traceback
from pathlib import Path

ROOT = Path(r"D:\travel-planner")
OUT = ROOT / "full_out.txt"
sys.path.insert(0, str(ROOT / "src"))

buf = io.StringIO()
rc = 1
try:
    import pytest

    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        rc = pytest.main([str(ROOT / "tests"), "-q", "--color=no", "--tb=short"])
except BaseException:
    buf.write("\n=== RUNNER EXCEPTION ===\n")
    buf.write(traceback.format_exc())

text = buf.getvalue() + f"\n\n=== EXIT CODE: {rc} ===\n"
OUT.write_text(text, encoding="utf-8")
print("written", len(text))
