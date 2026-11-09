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
    QUIZ_CLOSE: str = ""
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

    VERIFY_URL: str = ""

    async def verify_login(self) -> bool:
        """只用 cookie 判断会被残留票据骗到，所以真去请求一次需要登录的页面。

        默认实现返回 True，子类有 COURSE_URL 时覆盖。
        """
        return True

    async def login(self, wait_seconds: int = 900) -> bool:
        """打开登录页，等人工扫码/账密登录完成。

        判定分两步：先看 cookie 里有没有票据，再真的请求一次目标页确认服务端认可。
        只看 cookie 会误判——上一轮的残留票据还在，一开窗就"秒成功"，窗口立刻被关掉。
        """
        wait_seconds = int(self.cfg.get("login_wait") or wait_seconds)
        try:
            await self.page.goto(self.login_url or self.home_url, wait_until="domcontentloaded")
        except Exception:
            pass
        log.warning("请在浏览器窗口中完成登录（扫码或账密），最长等待 "
                    + str(wait_seconds) + " 秒，完成后窗口会自动关闭")
        rounds = max(1, int(wait_seconds / 3))
        for i in range(rounds):
            await asyncio.sleep(3)
            if not await self.is_logged_in():
                if i and i % 20 == 0:
                    log.info("仍在等待登录…（已等 " + str(i * 3) + " 秒）")
                continue
            if await self.verify_login():
                log.info("登录成功，服务端已确认")
                return True
            if i % 5 == 0:
                log.info("检测到登录票据但服务端还没认，继续等待…")
        log.error("登录等待超时（" + str(wait_seconds) + " 秒）")
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

    async def submit_popup_quiz(self, page) -> bool:
        """视频弹题的提交入口。没有独立按钮的平台直接复用普通提交。"""
        return await self.submit_quiz(page)

    async def read_quiz_result(self, page, questions: list[Question]) -> None:
        return None

    JS_CLEAR_MASK = """
() => {
  // 只把可见的遮罩藏起来，不删任何元素。
  // #workpop 是 workPop() 弹确认框用的容器，.popDiv 里装着提交按钮，
  // 删掉之后手动点提交会毫无反应（点了 btnBlueSubmit 却弹不出确认框）。
  document.querySelectorAll(".maskDiv, .layui-layer-shade").forEach(function (n) {
    try {
      if (getComputedStyle(n).display !== "none") n.style.display = "none";
    } catch (e) {}
  });
  document.body.style.overflow = "";
  document.documentElement.style.overflow = "";
  return true;
}
"""

    async def clear_mask(self, page) -> None:
        """遮罩常留在顶层文档（章节详情顶栏、#workpop），只清 iframe 内部没用。"""
        targets = [page]
        owner = getattr(page, "page", None)
        if owner is not None and owner is not page:
            targets.append(owner)
        for t in targets:
            try:
                await t.evaluate(self.JS_CLEAR_MASK)
            except Exception:
                continue

    async def dismiss_quiz(self, page) -> bool:
        """弹题答不上时把它关掉，否则视频会永远停在这里，整个章节做不完。

        先试关闭按钮，最后用 JS 直接把容器从 DOM 里摘掉——这一步保证下一次
        轮询检测不到弹题，避免"检测到→答不上→再检测到"的死循环。
        """
        closed = False
        for sel in (self.QUIZ_CLOSE or "").split(","):
            sel = sel.strip()
            if not sel:
                continue
            try:
                btn = page.locator(sel).first
                if await btn.count() and await btn.is_visible():
                    await btn.click(timeout=2500)
                    closed = True
                    break
            except Exception:
                continue

        removed = 0
        if self.VIDEO_QUESTION_POPUP:
            try:
                removed = await page.evaluate(
                    "(sel) => { const ns = document.querySelectorAll(sel);"
                    " let c = 0; ns.forEach((n) => { n.remove(); c++; }); return c; }",
                    self.VIDEO_QUESTION_POPUP,
                )
            except Exception:
                removed = 0
        return closed or bool(removed)

    # ---------------- 工具 ----------------

    async def content_frame(self, page):
        """取视频所在的 iframe frame；找不到就退回主页。"""
        # 先按 URL 特征找（iframe 的 id 经常变，URL 里的模块名反而稳定）
        try:
            # 任务点图标全在 knowledge/cards 里，先找它；ananas 只是播放器内层
            for f in page.frames:
                if "knowledge/cards" in (f.url or ""):
                    return f
            for f in page.frames:
                if "ananas/modules" in (f.url or ""):
                    return f
        except Exception:
            pass
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
        # 遮罩存在时 Playwright 会一直重试到超时，等 1.5 秒就够判断了
        try:
            await locator.click(timeout=1500)
            return True
        except Exception:
            pass
        # 被遮罩拦住 / 判"不可操作"时，用 JS 直接触发（JS 点击不受浮层影响）
        for attempt in ("el => el.click()", "el => el.dispatchEvent(new MouseEvent('click', {bubbles: true}))"):
            try:
                await locator.evaluate(attempt)
                return True
            except Exception:
                continue
        return False
