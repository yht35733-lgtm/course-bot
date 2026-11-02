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

                # 前端可能把 id 传成数字，统一转字符串再比，否则一门都筛不出来
                ids = {str(x) for x in (self.cfg["runner"].get("course_ids") or [])}
                wanted = self.cfg["runner"].get("courses") or []
                if ids:
                    courses = [c for c in courses if str(c.cid) in ids]
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
        # --task 按标题关键词挑小节，方便单独补某一个
        picks = self.cfg["runner"].get("only_task") or []
        if picks:
            tasks = [t for t in tasks if any(k in t.title for k in picks)]
            log.info("按关键词筛出 " + str(len(tasks)) + " 个小节: " + " / ".join(picks))

        limit = int(self.cfg["runner"].get("max_tasks") or 0)

        st.task_total = len(tasks)
        ran = 0
        for i, task in enumerate(tasks, 1):
            if self.stopped:
                break
            st.task_index = i
            if (not self.cfg["runner"].get("ignore_done")
                    and self.progress.is_done(self.platform, course.cid, task.tid)):
                log.info("跳过已完成 [" + task.kind + "] " + task.title)
                continue
            # 上限算的是"这一轮真跑了几个"，跳过的不占名额
            if limit and ran >= limit:
                log.info("已达到本轮上限 " + str(limit) + " 个任务，停止")
                break
            ran += 1
            log.info("(" + str(ran) + "/" + str(limit or len(tasks)) + ") [" + task.kind + "] " + task.title)
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

    async def play_target(self, adapter, task: Task, target, refetch=None) -> TaskResult:
        popup = adapter.VIDEO_QUESTION_POPUP
        hold = {"frame": target}
        st = self.state

        async def on_quiz() -> bool:
            st.phase = "quiz"
            frame = hold["frame"]
            try:
                owner = getattr(frame, "page", None) or frame
                all_frames = list(owner.frames)
            except Exception:
                all_frames = []
            outcome = await self.quiz.run_page(adapter, frame, frames=all_frames)
            st.phase = "video"
            if outcome.answered > 0 or outcome.submitted:
                return True
            # 答不上就关掉弹题，否则视频永远停在这儿，整个章节都别想完成
            log.warning("弹题未能作答（" + outcome.detail + "），关闭弹题继续播放")
            try:
                closed = await adapter.dismiss_quiz(frame)
                log.info("弹题已关闭" if closed else "弹题关闭失败，下一轮再试")
            except Exception as e:
                log.warning("关闭弹题异常: " + str(e))
            return False

        if self.cfg["video"].get("skip_quiz"):
            on_quiz = None

        async def refresh_target():
            # 优先用 click_job 给的 refetch，保证还是同一个视频 iframe
            if refetch is not None:
                try:
                    f = await refetch()
                    if f is not None:
                        return f
                except Exception:
                    pass
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

        # 顶部「1视频 / 2章节测验」是分组标签，测验藏在第二个标签后面，
        # 不切过去就永远只看到视频。逐个标签处理。
        tabs = []
        try:
            tabs = await adapter.job_tabs()
        except Exception:
            tabs = []
        if len(tabs) > 1:
            log.info("本节有 " + str(len(tabs)) + " 组任务点: " +
                     " / ".join(t["text"] for t in tabs))

        details: list[str] = []
        ok_any = False
        page = getattr(target, "page", target)

        for i in range(max(1, len(tabs))):
            if self.stopped:
                break
            if len(tabs) > 1:
                if not await adapter.switch_job_tab(tabs[i]["i"]):
                    continue
                log.info("切到任务点组: " + str(tabs[i]["text"]))
                await asyncio.sleep(1.8)
                target = await adapter.content_frame(page)
            got, det = await self._drain_jobs(adapter, course, task, target)
            ok_any = ok_any or got
            details.extend(det)

        if details:
            await self.reconcile(adapter, course, task)
        if not details:
            self.stats["skipped"] += 1
            return TaskResult(task, True, "无任务点，跳过")
        return TaskResult(task, ok_any, "; ".join(details))

    async def _drain_jobs(self, adapter, course, task, target) -> tuple[bool, list[str]]:
        """把当前标签下的任务点逐个做完，结果按任务点粒度写进 progress。"""
        details: list[str] = []
        ok_any = False
        tried: dict[int, int] = {}
        for _ in range(12):
            if self.stopped:
                return ok_any, details
            jobs = await self._list_jobs(target)
            pending = [j for j in jobs if not j["done"] and tried.get(j["i"], 0) < 2]
            if not pending:
                break
            job = pending[0]
            tried[job["i"]] = tried.get(job["i"], 0) + 1
            log.info("处理任务点 " + str(job["i"] + 1) + "/" + str(len(jobs)) +
                     " 类型 " + job["kind"] + "（剩 " + str(len(pending)) + " 个）")

            if job["kind"] == "work":
                done = await self._do_work_job(adapter, target, job)
                self._count("quiz", done)
                ok_any = ok_any or done
                details.append("测验" + ("完成" if done else "未提交"))
                self.progress.mark(self.platform, course.cid, task.tid,
                                   "done" if done else "submit_blocked",
                                   "测验已提交" if done else "答题完成但提交被拦",
                                   job=job["i"])
                await asyncio.sleep(2.0)
                continue

            vf, refetch = await self._click_job(target, job)
            if vf is None:
                vf = await self.find_video_frame(target)
            if vf is None:
                log.info("这个任务点里没有 video，跳过")
                self.progress.mark(self.platform, course.cid, task.tid, "skipped",
                                   "无视频", job=job["i"])
                continue
            res = await self.play_target(adapter, task, vf, refetch=refetch)
            self._count("video", res.ok)
            ok_any = ok_any or res.ok
            details.append("视频" + ("完成" if res.ok else "失败"))
            self.progress.mark(self.platform, course.cid, task.tid,
                               "done" if res.ok else "failed", res.detail, job=job["i"])
            await asyncio.sleep(1.5)

        return ok_any, details

    async def reconcile(self, adapter, course, task) -> None:
        """重进小节读平台真实状态：全绿就记小节完成，没绿的点出来。"""
        try:
            await adapter.open_course(course)
            target = await adapter.open_task(task)
            if target is None:
                return
            jobs = await self._list_jobs(target)
        except Exception as e:
            log.debug("重进核对失败: " + str(e))
            return
        if not jobs:
            return
        still = [j for j in jobs if not j["done"]]
        if not still:
            self.progress.mark(self.platform, course.cid, task.tid, "done", "全部任务点已完成")
            log.info("重进核对: 本节任务点已全部变绿")
            return
        for j in still:
            prev = self.progress.status(self.platform, course.cid, task.tid, j["i"])
            # 平台可能延迟，给两轮机会；连续两次都不过就锁死，避免无限重播
            status = "platform_reject" if prev == "platform_reject" else "failed"
            self.progress.mark(self.platform, course.cid, task.tid, status,
                               "重进后仍未完成", job=j["i"])
        log.warning("重进核对: 还有 " + str(len(still)) + " 个任务点没变绿")

    JS_CLEAR_MASK = """
() => {
  // 测验弹窗的遮罩/浮层会挡住任务点，点之前先摘掉
  ['#workpop', '.maskDiv', '.layui-layer-shade', '.layui-layer', '.popDiv'].forEach(function (s) {
    document.querySelectorAll(s).forEach(function (n) { n.remove(); });
  });
  // 顶部导航条也会压住元素，让它不再吃点击
  document.querySelectorAll('.subNav').forEach(function (n) { n.style.pointerEvents = 'none'; });
  return true;
}
"""

    async def _clear_mask(self, target) -> None:
        try:
            await target.evaluate(self.JS_CLEAR_MASK)
        except Exception:
            pass

    JS_JOBS = """
() => {
  // 类型主要看容器里的 iframe 模块名：modules/video 是视频，modules/work 是测验
  const out = [];
  document.querySelectorAll('.ans-job-icon').forEach(function (el, i) {
    const box = el.closest('.ans-attach-ct') || el.parentElement;
    const cls = (el.className || '').toString();
    let kind = 'unknown';
    if (cls.indexOf('ans-job-video') >= 0) kind = 'video';
    else if (cls.indexOf('ans-job-work') >= 0) kind = 'work';
    else if (box) {
      const ifr = box.querySelector('iframe');
      const src = ifr ? (ifr.getAttribute('src') || '') : '';
      if (src.indexOf('modules/work') >= 0) kind = 'work';
      else if (src.indexOf('modules/video') >= 0) kind = 'video';
      else if (box.querySelector('.ans-attach-online, video')) kind = 'video';
      else if (box.querySelector('a[href*="work"], [onclick*="work"]')) kind = 'work';
    }
    out.push({
      i: i, kind: kind,
      done: /ans-job-finished/.test(box ? (box.className || '') : '')
    });
  });
  return out;
}
"""

    async def _list_jobs(self, target) -> list[dict]:
        # 任务点是懒加载的：不滚到底，后面的答题根本不会出现在 DOM 里
        try:
            await target.evaluate(
                "() => { window.scrollTo(0, document.body.scrollHeight); "
                "setTimeout(function(){ window.scrollTo(0, 0); }, 300); }"
            )
            await asyncio.sleep(2.0)
        except Exception:
            pass
        try:
            jobs = await target.evaluate(self.JS_JOBS)
        except Exception as e:
            log.warning("读取任务点失败: " + str(e))
            return []
        known = [j for j in jobs if j.get("kind") != "unknown"]
        if not known:
            log.warning("本组没读到任务点（原始 %d 个，全部 unknown）frame=%s"
                        % (len(jobs), (getattr(target, "url", "") or "")[:60]))
        return known

    async def _click_job(self, target, job: dict):
        """点开任务点，返回 (视频 frame, 重新取 frame 的闭包)。

        必须返回"这个任务点自己的" iframe，否则同一节有两个视频时会一直播第一个。
        """
        try:
            await self._clear_mask(target)
            icon = target.locator(".ans-job-icon").nth(job["i"])
            await icon.scroll_into_view_if_needed(timeout=5000)
            box = icon.locator("xpath=ancestor::div[contains(@class,'ans-attach-ct')][1]")
            if job.get("kind") == "video" and await box.count():
                card = box.locator(".ans-attach-online").first
                if await card.count():
                    await card.click(timeout=6000)
                else:
                    await icon.click(timeout=6000)
            else:
                await icon.click(timeout=6000)

            if job.get("kind") != "video":
                return None, None

            await asyncio.sleep(2.0)
            iframe = box.locator("iframe").first
            if not await iframe.count():
                return None, None
            handle = await iframe.element_handle()
            frame = await handle.content_frame() if handle is not None else None

            async def refetch():
                h = await box.locator("iframe").first.element_handle()
                return await h.content_frame() if h is not None else None

            return frame, refetch
        except Exception as e:
            log.warning("点击任务点失败: " + str(e))
            return None, None

    async def _do_work_job(self, adapter, target, job: dict) -> bool:
        """章节测验：modules/work 只是个空壳，点了任务点图标它才会把题目拉进来。"""
        page = getattr(target, "page", target)
        # 点测验图标常常会弹新标签页，答完不关就会越堆越多，把前台焦点抢走
        ctx = page.context
        try:
            opened_before = {id(p) for p in ctx.pages}
        except Exception:
            opened_before = set()
        try:
            await self._clear_mask(target)
            icon = target.locator(".ans-job-icon").nth(job["i"])
            await icon.scroll_into_view_if_needed(timeout=5000)
            await icon.click(timeout=6000)
            log.info("已点击测验任务点，等题目加载")
        except Exception as e:
            log.warning("点击测验任务点失败: " + str(e))
        await asyncio.sleep(4.0)

        # 点开后题目可能落在新标签页、弹层、或 work iframe 自己里面
        best = None
        try:
            pages = list(page.context.pages)
        except Exception:
            pages = [page]
        for pg in pages:
            try:
                frames = list(pg.frames)
            except Exception:
                frames = []
            for f in frames:
                u = (f.url or "").lower()
                if "work" not in u and "exam" not in u and "homework" not in u:
                    continue
                try:
                    qs = await adapter.extract_questions(f)
                except Exception:
                    qs = []
                if qs:
                    best = f
                    break
            if best is not None:
                break

        if best is None:
            log.warning("测验页里没识别到题目（work 页面结构可能不匹配）")
            return False

        await best.wait_for_timeout(500)

        # 题目是字体反爬的乱码，先解码，否则题库和模型看到的都是天书
        try:
            from .fontdecode import FontDecoder
            dec = FontDecoder()
            n = await dec.build_from_page(best)
            if n > 0:
                adapter.font_map = dec.map
            else:
                log.warning("字体解码没成功，题干可能仍是乱码")
        except Exception as e:
            log.warning("字体解码异常: " + str(e))

        self.state.phase = "quiz"
        outcome = await self.quiz.run_page(adapter, best)
        log.info("测验: 共 " + str(outcome.total) + " 题，作答 " + str(outcome.answered) +
                 "，题库 " + str(outcome.from_bank) + "，模型 " + str(outcome.from_llm) +
                 " | " + outcome.detail)

        # 关掉这次答题新开的标签页
        try:
            for p in list(ctx.pages):
                if id(p) not in opened_before and p is not page:
                    await p.close()
        except Exception:
            pass
        if not outcome.answered:
            return False
        # 只有提交成功才算任务点完成；答完但提交被拦会退成保存，记 submit_blocked
        if self.cfg["quiz"].get("submit", True) and not outcome.submitted:
            log.warning("答完了但没提交成功，任务点不会变绿")
            return False
        return True

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
        # 上一轮点过「停止」会留下 stopped=True，不清掉新任务连循环体都进不去，
        # 表现就是"点了开始什么都不发生"
        self.state.control.reset()
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
