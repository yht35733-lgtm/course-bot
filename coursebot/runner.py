"""编排层：课程 -> 小节 -> 视频/测验，带暂停闸门、断点与常驻循环。"""
from __future__ import annotations

import asyncio
import random
from datetime import datetime
from typing import Optional

from .bank import Bank
from .browser import BrowserSession
from .config import ROOT
from .logger import log
from .models import Course, Task, TaskResult
from .platforms import get_adapter
from .quiz import QuizEngine
from .state import RunState
from .store import Progress
from .video import VideoEngine

JS_VIDEO_COUNT = "() => document.querySelectorAll('video').length"


class Runner:
    def __init__(self, cfg: dict, only_kind: Optional[str] = None,
                 state: Optional[RunState] = None):
        self.cfg = cfg
        self.only_kind = only_kind
        self.platform = cfg.get("platform", "chaoxing")
        self.bank = Bank(cfg["quiz"]["bank_path"])
        self.quiz = QuizEngine(self.bank, cfg["quiz"], cfg.get("llm") or {},
                               pending_path=ROOT / "data" / "pending.jsonl")
        self.progress = Progress(ROOT / "data" / "progress.json")
        self.state = state or RunState()
        self.stats = self.state.stats

    # ---------------- 控制 ----------------

    def stop(self) -> None:
        self.state.control.stop()

    def pause(self) -> None:
        self.state.control.pause()
        self.state.phase = "paused"

    def resume(self) -> None:
        self.state.control.resume()

    @property
    def stopped(self) -> bool:
        return self.state.control.stopped

    def in_window(self) -> bool:
        wins = self.cfg["runner"].get("daily_window") or []
        if not wins:
            return True
        now = datetime.now().strftime("%H:%M")
        for w in wins:
            if "-" in w:
                a, b = w.split("-", 1)
                if a <= now <= b:
                    return True
        log.info("当前时间 " + now + " 不在允许窗口 " + str(wins) + "，等待中")
        return False

    # ---------------- 主流程 ----------------

    async def run_once(self) -> None:
        st = self.state
        st.begin()
        try:
            if not self.in_window():
                st.finish("done")
                return
            adapter_cls = get_adapter(self.platform)
            headless = self.cfg["browser"].get("headless", False)
            async with BrowserSession(self.cfg["browser"], headless=headless) as sess:
                page = await sess.new_page()
                adapter = adapter_cls(page, self.cfg)
                log.info("平台: " + adapter.name + " | 题库: " + self.bank.stats()["file"])

                st.phase = "preparing"
                await adapter.goto_home()
                if not await adapter.is_logged_in():
                    log.warning("未检测到登录态，请在控制台点「登录」")
                    st.finish("error", "未登录")
                    return

                courses = await adapter.list_courses()
                if not courses:
                    log.warning("未获取到课程列表")
                    st.finish("error", "没有课程")
                    return

                ids = self.cfg["runner"].get("course_ids") or []
                wanted = self.cfg["runner"].get("courses") or []
                if ids:
                    courses = [c for c in courses if c.cid in ids]
                elif wanted:
                    courses = [c for c in courses if any(w in c.name for w in wanted)]

                cap = int(self.cfg["runner"].get("max_courses") or 0)
                if cap:
                    courses = courses[:cap]
                if not courses:
                    log.warning("筛选后没有可跑的课程")
                    st.finish("done")
                    return

                st.course_total = len(courses)
                for idx, course in enumerate(courses, 1):
                    if self.stopped:
                        break
                    st.course_index = idx
                    await self.run_course(adapter, course)
                    if self.stopped:
                        break
                    await asyncio.sleep(float(self.cfg["runner"].get("course_interval", 3.0)))

            log.info("本轮统计: " + str(st.stats))
            st.finish("done")
        except Exception as e:
            log.exception("运行中断: " + str(e))
            st.finish("error", str(e))
        finally:
            if st.phase != "error":
                st.running = False

    async def run_course(self, adapter, course: Course) -> None:
        st = self.state
        st.phase = "course"
        st.course = course.name
        log.info("=" * 12 + " 课程: " + course.name + " " + "=" * 12)
        await adapter.open_course(course)
        tasks = await adapter.list_tasks(course)

        if self.only_kind and self.only_kind != "mixed":
            tasks = [t for t in tasks if t.kind == self.only_kind or t.kind == "mixed"]
        limit = int(self.cfg["runner"].get("max_tasks") or 0)
        if limit:
            tasks = tasks[:limit]

        st.task_total = len(tasks)
        for i, task in enumerate(tasks, 1):
            if self.stopped:
                break
            st.task_index = i
            if self.progress.is_done(self.platform, course.cid, task.tid):
                log.info("跳过已完成 [" + task.kind + "] " + task.title)
                continue
            log.info("(" + str(i) + "/" + str(len(tasks)) + ") [" + task.kind + "] " + task.title)
            if self.cfg["runner"].get("reopen_course", True):
                try:
                    await adapter.open_course(course)
                except Exception as e:
                    log.warning("重开课程失败: " + str(e))
            if self.stopped:
                break
            try:
                res = await self.handle_task(adapter, course, task)
            except Exception as e:
                log.exception("任务异常: " + str(e))
                res = TaskResult(task, False, "异常: " + str(e))
            if res.ok:
                self.progress.mark(self.platform, course.cid, task.tid, "done", res.detail)
            else:
                self.progress.mark(self.platform, course.cid, task.tid, "failed", res.detail)
                if self.cfg["runner"].get("stop_on_error"):
                    self.stop()
            await asyncio.sleep(float(self.cfg["runner"].get("cooldown", 0.6)))

    async def handle_task(self, adapter, course: Course, task: Task) -> TaskResult:
        self.state.task = task.title
        self.state.task_kind = task.kind
        self.state.progress = 0.0
        if task.kind == "mixed":
            return await self.do_mixed(adapter, course, task)
        if task.kind == "video":
            return await self.do_video(adapter, course, task)
        if task.kind == "quiz":
            return await self.do_quiz(adapter, course, task)
        log.info("未支持的任务类型 " + task.kind + "，跳过")
        self.stats["skipped"] += 1
        return TaskResult(task, True, "skipped")

    # ---------------- 定位播放器 ----------------

    async def find_video_frame(self, target):
        page = getattr(target, "page", target)
        try:
            frames = list(page.frames)
        except Exception:
            frames = [target]
        for f in frames:
            try:
                if await f.evaluate(JS_VIDEO_COUNT):
                    return f
            except Exception:
                continue
        return None

    async def play_target(self, adapter, task: Task, target) -> TaskResult:
        popup = adapter.VIDEO_QUESTION_POPUP
        hold = {"frame": target}
        st = self.state

        async def on_quiz() -> bool:
            st.phase = "quiz"
            outcome = await self.quiz.run_page(adapter, hold["frame"])
            st.phase = "video"
            return outcome.answered > 0 or outcome.submitted

        if self.cfg["video"].get("skip_quiz"):
            on_quiz = None

        async def refresh_target():
            return await self.find_video_frame(target) or target

        st.phase = "video"

        def on_progress(ratio: float) -> None:
            st.progress = ratio

        engine = VideoEngine(hold["frame"], self.cfg["video"], on_quiz=on_quiz,
                             question_selector=popup, target_getter=refresh_target,
                             gate=st.control, on_progress=on_progress)
        return await engine.play(task)

    # ---------------- 三种任务 ----------------

    async def do_video(self, adapter, course: Course, task: Task) -> TaskResult:
        target = await adapter.open_task(task)
        if target is None:
            return TaskResult(task, False, "无法进入任务页")
        res = await self.play_target(adapter, task, target)
        self._count("video", res.ok)
        (log.info if res.ok else log.warning)(
            "视频" + ("完成: " if res.ok else "失败: ") + task.title + " (" + res.detail + ")")
        return res

    async def do_quiz(self, adapter, course: Course, task: Task) -> TaskResult:
        target = await adapter.open_task(task)
        if target is None:
            return TaskResult(task, False, "无法进入测验页")
        await target.wait_for_timeout(2000)
        self.state.phase = "quiz"
        outcome = await self.quiz.run_page(adapter, target)
        ok = outcome.answered > 0
        self._count("quiz", ok)
        log.info(
            "测验: 共" + str(outcome.total) + "题，作答" + str(outcome.answered) +
            "，题库" + str(outcome.from_bank) + "，模型" + str(outcome.from_llm) +
            "，猜测" + str(outcome.guessed) + " | " + outcome.detail
        )
        return TaskResult(task, ok, outcome.detail)

    async def do_mixed(self, adapter, course: Course, task: Task) -> TaskResult:
        target = await adapter.open_task(task)
        if target is None:
            return TaskResult(task, False, "无法打开小节")
        await asyncio.sleep(1.5)

        details: list[str] = []
        ok_any = False

        video_frame = await self.find_video_frame(target)
        if video_frame is None:
            if await self._click_video_card(target):
                await asyncio.sleep(3.0)
                video_frame = await self.find_video_frame(target)

        if video_frame is not None:
            res = await self.play_target(adapter, task, video_frame)
            self._count("video", res.ok)
            details.append("视频" + ("完成 " + res.detail if res.ok else "失败 " + res.detail))
            ok_any = ok_any or res.ok

        self.state.phase = "quiz"
        questions = await adapter.extract_questions(target)
        if not questions and video_frame is not None:
            questions = await adapter.extract_questions(video_frame)
        if questions:
            outcome = await self.quiz.run_page(adapter, target)
            ok = outcome.answered > 0
            self._count("quiz", ok)
            details.append("测验" + str(outcome.answered) + "/" + str(outcome.total))
            ok_any = ok_any or ok

        if not details:
            self.stats["skipped"] += 1
            return TaskResult(task, True, "无视频无题目，跳过")
        return TaskResult(task, ok_any, "; ".join(details))

    async def _click_video_card(self, target) -> bool:
        for sel in (".ans-attach-online", ".ans-attach-ct", ".videoCard"):
            try:
                loc = target.locator(sel).first
                if await loc.count():
                    await loc.click(timeout=4000)
                    return True
            except Exception:
                continue
        return False

    def _count(self, kind: str, ok: bool) -> None:
        self.stats[kind + ("_ok" if ok else "_fail")] += 1

    # ---------------- 常驻 ----------------

    async def loop(self) -> None:
        interval_min = float(self.cfg["runner"].get("loop_interval") or 0)
        while not self.stopped:
            await self.run_once()
            if interval_min <= 0:
                break
            wait = interval_min * 60 + random.randint(0, 60)
            log.info("下一轮 " + str(round(wait / 60, 1)) + " 分钟后")
            for _ in range(int(wait)):
                if self.stopped:
                    break
                await asyncio.sleep(1)
