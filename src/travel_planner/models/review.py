"""复盘 / 校验结果数据模型。review_agent 的产物，结构化、可断言、不靠 LLM 打分。"""
from enum import Enum
from typing import List

from pydantic import BaseModel


class IssueSeverity(str, Enum):
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


class Issue(BaseModel):
    severity: IssueSeverity
    code: str
    message: str
    related_poi_ids: List[str] = []


class CheckResult(BaseModel):
    name: str
    passed: bool
    detail: str = ""


class ReviewResult(BaseModel):
    passed: bool
    score: float                       # 0-100
    issues: List[Issue]
    checks: List[CheckResult]
