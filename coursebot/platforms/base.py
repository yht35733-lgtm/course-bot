"""平台适配器抽象层。

所有平台差异收敛到这一个类：选择器常量 + 几个 DOM 操作。
runner 只认这里的接口，换平台 = 新增一个子类。
"""
from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from typing import Any, Optional

from ..logger import log
from ..models import Course, Task
from ..quiz import Question


class PlatformAdapter(ABC):
    name: str = "base"
    home_url: str = ""
    login_url: str = ""

    # ---- 选择器（子类按平台覆盖）----
    COURSE_CARD: str = ""
    CHAPTER_ITEM: str = ""
    CONTENT_IFRAME: str = ""
    VIDEO_QUESTION_POPUP: str = ""
    QUESTION_ITEM: str = ""
    QUESTION_STEM: str = ""
    QUESTION_OPTION: str = ""
    SUBMIT_BTN: str = ""
    IFRAME_TIMEOUT: int = 8000

    def __init__(self, page, cfg: Optional[dict] = None):
        self.page = page
        self.cfg = cfg or {}

    # ---------------- 登录 ----------------

    async def goto_home(self) -> None:
        try:
            await self.page.goto(self.home_url, wait_until="domcontentloaded")
        except Exception:
            pass

    @abstractmethod
    async def is_logged_in(self) -> bool:
        ...

    async def login(self, wait_seconds: int = 180) -> bool:
        """打开登录页，等人工扫码/账密登录完成。"""
        try:
            await self.page.goto(self.login_url or self.home_url, wait_until="domcontentloaded")
        except Exception:
            pass
        log.warning("请在浏览器窗口中完成登录（扫码或账密），最长等待 " + str(wait_seconds) + " 秒")
        for _ in range(int(wait_seconds / 3)):
            await asyncio.sleep(3)
            if await self.is_logged_in():
                log.info("登录成功，登录态已写入 profile")
                return True
        log.error("登录等待超时")
        return False

    # ---------------- 课程与任务 ----------------

    @abstractmethod
    async def list_courses(self) -> list[Course]:
        ...

    @abstractmethod
    async def open_course(self, course: Course) -> None:
        ...

    @abstractmethod
    async def list_tasks(self, course: Course) -> list[Task]:
        ...

    @abstractmethod
    async def open_task(self, task: Task) -> Optional[Any]:
        """打开任务，返回真正的播放/答题目标（frame 或 page）。"""

    # ---------------- 答题 ----------------

    @abstractmethod
    async def extract_questions(self, page) -> list[Question]:
        ...

    @abstractmethod
    async def answer_question(self, page, q: Question) -> None:
        ...

    @abstractmethod
    async def submit_quiz(self, page) -> bool:
        ...

    async def read_quiz_result(self, page, questions: list[Question]) -> None:
        return None

    # ---------------- 工具 ----------------

    async def content_frame(self, page):
        """取视频所在的 iframe frame；找不到就退回主页。"""
        if not self.CONTENT_IFRAME:
            return page
        try:
            handle = await page.wait_for_selector(self.CONTENT_IFRAME, timeout=self.IFRAME_TIMEOUT)
            if handle:
                frame = await handle.content_frame()
                if frame:
                    return frame
        except Exception:
            pass
        return page

    @staticmethod
    async def text_of(locator, default: str = "") -> str:
        try:
            t = await locator.inner_text(timeout=2000)
            return (t or "").strip()
        except Exception:
            return default

    @staticmethod
    async def safe_click(locator) -> bool:
        try:
            await locator.click(timeout=4000)
            return True
        except Exception:
            try:
                await locator.click(force=True, timeout=3000)
                return True
            except Exception:
                return False
