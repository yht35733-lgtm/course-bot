"""公共数据模型。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class Course:
    """一门课程。cid 取自平台，用于去重与续跑。"""
    cid: str
    name: str
    url: str = ""
    raw: Any = None

    def __str__(self) -> str:
        return f"[{self.cid}] {self.name}"


@dataclass
class Task:
    """课程内的一个学习单元：视频 / 测验 / 文档 / 讨论。"""
    tid: str
    kind: str                      # video | quiz | doc | discuss | unknown
    title: str
    url: str = ""
    status: str = "pending"        # pending | done | locked | failed | skipped
    meta: dict = field(default_factory=dict)

    def __str__(self) -> str:
        return f"<{self.kind}:{self.title}>"


@dataclass
class TaskResult:
    task: Task
    ok: bool
    detail: str = ""
    score: Optional[float] = None

    def __bool__(self) -> bool:
        return self.ok
