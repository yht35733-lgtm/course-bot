"""超星 / 学习通 适配器（对齐 2025-2026 的 mooc2-ans 版本）。

实测结构（2026-10）
-------------------
* 课程列表  https://mooc1.chaoxing.com/visit/interaction
            卡片 div.course，链接 /mooc-ans/visit/stucoursemiddle?courseid=...
* 课程门户  https://mooc2-ans.chaoxing.com/mooc2-ans/mycourse/stu?courseid=...
            顶部 tab：任务 / 章节 / 讨论 / 作业 / 考试 ...
* 章节列表  点「章节」后加载 iframe#frame_content-zj
            -> /mooc2-ans/mycourse/studentcourse，条目 .catalog_title（章标题带
               chapter_Thats_bnt，小节没有，用它区分）
* 学习页    点小节标题后加载 iframe#frame_content，video 在更内层的卡片 iframe 里
* 弹题      .ans-videoquiz-opt
* 测验      .questionLi / .Zy_TItle

登录态判定不看 URL，只看 _uid 等票据 cookie——扫码后先跳中转页，
那时 fid 已种下但 _uid 还没有，按 URL 判会提前收工。
"""
from __future__ import annotations

import re
from typing import Any, Optional
from urllib.parse import urljoin

from ..logger import log
from ..models import Course, Task
from ..quiz import Question
from .base import PlatformAdapter


class ChaoxingAdapter(PlatformAdapter):
    name = "chaoxing"
    home_url = "https://i.chaoxing.com/"
    login_url = "https://passport2.chaoxing.com/login"
    COURSE_URL = "https://mooc1.chaoxing.com/visit/interaction"

    COURSE_CARD = "div.course, .course-info, li.courseItem"
    CHAPTER_TAB_TEXT = ("章节",)
    CATALOG_IFRAME = "#frame_content-zj"
    TASK_ITEM = ".catalog_title:not(.chapter_Thats_bnt)"
    CONTENT_IFRAME = "#frame_content"
    VIDEO_QUESTION_POPUP = ".ans-videoquiz-opt, .videoquiz, #videoquiz"
    QUESTION_ITEM = ".questionLi, .TiMu, .question-item"
    QUESTION_STEM = ".Zy_TItle, .mark_name, .question-title"
    QUESTION_OPTION = "li, .answer_p, .option"
    # 超星作业页两个按钮：.btnSave=暂时保存（noSubmit()），.btnSubmit=提交（btnBlueSubmit()）。
    # 之前的选择器没含 .btnSubmit，反而点到了保存，作业只存草稿，任务点永远不变绿。
    SUBMIT_BTN = (".btnSubmit, .completeBtn, a:has-text('提交'),"
                  " button:has-text('提交'), a:has-text('交卷')")
    QUIZ_CLOSE = (".ans-videoquiz-close, .ans-videoquiz .close, .popClose,"
                  " .layui-layer-close, .close-btn")

    # 只认真正的用户票据；别把随便一个叫 id 的 cookie 当成登录成功
    SESSION_COOKIES = ("_uid", "UID")

    # ---------------- 登录态 ----------------

    async def session_cookies(self) -> set[str]:
        try:
            cookies = await self.page.context.cookies()
        except Exception:
            return set()
        return {c["name"] for c in cookies
                if c.get("value") and (c.get("domain") or "").endswith("chaoxing.com")}

    async def is_logged_in(self) -> bool:
        names = await self.session_cookies()
        return any(n in names for n in self.SESSION_COOKIES)

    async def verify_login(self) -> bool:
        """票据可能是上一轮留下的空壳：开个临时标签页真请求一次课程页。

        被重定向到 passport 就说明服务端不认，不能算登录成功。
        """
        try:
            ctx = self.page.context
        except Exception:
            return False
        probe = None
        try:
            probe = await ctx.new_page()
            await probe.goto(self.COURSE_URL, wait_until="domcontentloaded", timeout=25000)
            await probe.wait_for_timeout(1200)
            url = (probe.url or "").lower()
            return not ("passport" in url or "/login" in url)
        except Exception as e:
            log.debug("登录验证失败: " + str(e))
            return False
        finally:
            if probe is not None:
                try:
                    await probe.close()
                except Exception:
                    pass

    # ---------------- 课程 ----------------

    async def list_courses(self) -> list[Course]:
        try:
            await self.page.goto(self.COURSE_URL, wait_until="domcontentloaded")
        except Exception:
            pass
        await self.page.wait_for_timeout(2500)

        courses: list[Course] = []
        seen = set()
        nodes = self.page.locator(self.COURSE_CARD)
        for i in range(await nodes.count()):
            node = nodes.nth(i)
            inner = node.locator(".course-name, h3, .title")
            title = await self.text_of(inner.first if await inner.count() else node)
            if not title:
                continue
            href = ""
            try:
                href = await node.locator("a").first.get_attribute("href") or ""
            except Exception:
                pass
            cid = self._extract_cid(href or title)
            if cid in seen:
                continue
            seen.add(cid)
            courses.append(Course(cid=cid, name=title.split("\n")[0][:60], url=href))
        log.info("发现 " + str(len(courses)) + " 门课程")
        return courses

    @staticmethod
    def _extract_cid(text: str) -> str:
        m = re.search(r"courseid=(\d+)", text or "", re.I)
        if m:
            return m.group(1)
        return re.sub(r"\W+", "", text or "")[:16] or "unknown"

    async def open_course(self, course: Course) -> None:
        if course.url:
            url = course.url if course.url.startswith("http") else urljoin(self.COURSE_URL, course.url)
            try:
                await self.page.goto(url, wait_until="domcontentloaded")
                await self.page.wait_for_timeout(2500)
            except Exception as e:
                log.warning("直接打开课程失败: " + str(e))
        else:
            try:
                await self.page.goto(self.COURSE_URL, wait_until="domcontentloaded")
                await self.page.wait_for_timeout(2000)
                await self.page.get_by_text(course.name, exact=False).first.click(timeout=8000)
                await self.page.wait_for_timeout(3000)
            except Exception as e:
                log.error("打开课程失败: " + str(e))
        await self.open_chapter_tab()

    async def open_chapter_tab(self) -> None:
        """课程门户默认停在「任务」，章节列表必须点「章节」才加载。"""
        for label in self.CHAPTER_TAB_TEXT:
            try:
                loc = self.page.get_by_text(label, exact=True).first
                if await loc.count():
                    await loc.click(timeout=6000)
                    await self.page.wait_for_timeout(2500)
                    return
            except Exception:
                continue
        log.debug("未找到「章节」标签，可能已经在章节页")

    async def catalog_frame(self):
        try:
            handle = await self.page.wait_for_selector(self.CATALOG_IFRAME, timeout=15000)
            frame = await handle.content_frame()
            if frame:
                await frame.wait_for_timeout(1200)
                return frame
        except Exception as e:
            log.warning("未取到章节 iframe: " + str(e))
        return None

    async def list_tasks(self, course: Course) -> list[Task]:
        frame = await self.catalog_frame()
        if frame is None:
            log.warning("章节 iframe 不存在，可能课程未开放或选择器需调整")
            return []

        tasks: list[Task] = []
        try:
            items = frame.locator(self.TASK_ITEM)
            n = await items.count()
        except Exception as e:
            log.warning("读取章节失败: " + str(e))
            return []

        for i in range(n):
            node = items.nth(i)
            title = await self.text_of(node)
            if not title:
                continue
            # 标题行尾会跟一个任务点数量，去掉它
            title = re.sub(r"\s+\d+\s*$", "", " ".join(title.split()))[:80]
            tasks.append(Task(tid="ch" + str(i), kind="mixed", title=title,
                              meta={"index": i}))
        log.info("课程「" + course.name + "」发现 " + str(len(tasks)) + " 个小节")
        return tasks

    # 顶部那排「1视频 / 2章节测验」是任务点分组标签，测验藏在第二个标签后面
    JS_TABS = """
() => Array.from(document.querySelectorAll('#prev_tab li, .prev_tab li, #prev_list li')).map(function (li, i) {
  return { i: i,
           text: (li.innerText || '').replace(/\s+/g, '').slice(0, 20),
           active: /active/.test(li.className || '') };
})
"""

    async def job_tabs(self) -> list[dict]:
        """分组的 li 不一定在顶层文档里，逐个 frame 找。"""
        for attempt in range(3):
            for f in self.page.frames:
                try:
                    res = await f.evaluate(self.JS_TABS)
                except Exception:
                    continue
                if res and len(res) > 0:
                    self._tabs_frame = f
                    log.info("任务点分组: " + " / ".join(t["text"] for t in res))
                    return res
            await self.page.wait_for_timeout(1500)
        return []

    async def switch_job_tab(self, index: int) -> bool:
        frame = getattr(self, "_tabs_frame", None) or self.page
        try:
            li = frame.locator("#prev_tab li, .prev_tab li, #prev_list li").nth(index)
            await li.scroll_into_view_if_needed(timeout=4000)
            await li.click(timeout=6000)
            await self.page.wait_for_timeout(2800)
            return True
        except Exception as e:
            log.warning("切换任务点标签失败: " + str(e))
            return False

    async def open_task(self, task: Task) -> Optional[Any]:
        """点击小节标题，返回真正承载播放器/题目的 frame。"""
        frame = await self.catalog_frame()
        if frame is None:
            return None
        idx = int(task.meta.get("index", 0))
        try:
            node = frame.locator(self.TASK_ITEM).nth(idx)
            await node.click(timeout=8000)
        except Exception as e:
            log.warning("点击小节失败: " + str(e))
            return None
        await self.page.wait_for_timeout(3000)
        # 播放器 / 任务点卡片都在 knowledge/cards 这个 iframe 里，
        # 按 URL 找最稳；退回 #frame_content 常常拿到的是外面那层学习页
        for f in self.page.frames:
            if "knowledge/cards" in (f.url or ""):
                return f
        return await self.content_frame(self.page)

    # ---------------- 答题 ----------------

    def decode(self, text: str) -> str:
        """把字体反爬的乱码还原成真字。"""
        m = getattr(self, "font_map", None)
        if not m or not text:
            return text
        return "".join(m.get(ch, ch) for ch in text)

    async def extract_questions(self, page) -> list[Question]:
        questions: list[Question] = []
        try:
            items = page.locator(self.QUESTION_ITEM)
            n = await items.count()
        except Exception:
            return questions

        for i in range(n):
            root = items.nth(i)
            try:
                if not await root.is_visible():
                    continue
            except Exception:
                pass
            stem = ""
            for sel in self.QUESTION_STEM.split(","):
                sel = sel.strip()
                if not sel:
                    continue
                try:
                    loc = root.locator(sel).first
                    if await loc.count():
                        stem = (await loc.inner_text(timeout=1500) or "").strip()
                        if stem:
                            break
                except Exception:
                    continue
            if not stem:
                stem = await self.text_of(root)
            stem = re.sub(r"^\d+[\.、\s]*", "", stem).strip()
            stem = self.decode(stem)
            if not stem:
                continue

            opts: list[str] = []
            nodes: list[Any] = []
            try:
                opt_loc = root.locator(self.QUESTION_OPTION)
                for j in range(await opt_loc.count()):
                    o = opt_loc.nth(j)
                    t = await self.text_of(o)
                    if t and len(t) < 200:
                        opts.append(self.decode(t))
                        nodes.append(o)
            except Exception:
                pass

            qtype = await self._detect_type(stem, opts, root)
            questions.append(Question(index=i, stem=stem, qtype=qtype,
                                      options=opts, root=root, option_nodes=nodes))
        return questions

    async def _detect_type(self, stem: str, options: list[str], root) -> str:
        head = stem[:20]
        if "填空" in head:
            return "fill"
        if "多选" in head:
            return "multiple"
        if "判断" in head:
            return "judge"
        joined = "".join(options)
        if len(options) == 2 and ("正确" in joined or "错误" in joined):
            return "judge"
        try:
            if await root.locator("input[type=checkbox]").count() > 0:
                return "multiple"
        except Exception:
            pass
        return "single"

    async def answer_question(self, page, q: Question) -> None:
        if not q.answer:
            return
        if q.qtype == "fill":
            try:
                inputs = q.root.locator("input[type=text], textarea")
                for k, val in enumerate(q.answer):
                    if k < await inputs.count():
                        await inputs.nth(k).fill(str(val), timeout=3000)
            except Exception as e:
                log.debug("填空失败: " + str(e))
            return

        for idx in q.answer:
            if not isinstance(idx, int) or idx >= len(q.option_nodes):
                continue
            node = q.option_nodes[idx]
            ok = await self.safe_click(node)
            if not ok:
                try:
                    await node.locator("input").first.click(force=True, timeout=2000)
                except Exception:
                    log.warning("选项点击失败: 第" + str(idx) + "项")
            await page.wait_for_timeout(200)

    async def submit_quiz(self, page) -> bool:
        """先点提交；被风控拦下就退而点「暂时保存」，至少别丢答案。"""
        ok = await self._try_submit(page)
        if not ok:
            log.warning("提交被拦（/work/validate 判账号异常），改为点「暂时保存」")
            await self._try_save(page)
        return ok

    async def _try_submit(self, page) -> bool:
        for sel in self.SUBMIT_BTN.split(","):
            sel = sel.strip()
            if not sel:
                continue
            try:
                btn = page.locator(sel).first
                if not await btn.count():
                    continue
                if not await self.safe_click(btn):
                    log.warning("提交按钮点不动: " + sel)
                    continue
                await page.wait_for_timeout(3000)
                for csel in ("button:has-text('确定')", "a:has-text('确定')",
                             "button:has-text('确认')", "a:has-text('确认')",
                             ".layui-layer-btn0", ".popDiv a", ".btnSure", ".sureBtn"):
                    try:
                        c = page.locator(csel).first
                        if await c.count() and await c.is_visible():
                            await self.safe_click(c)
                            await page.wait_for_timeout(2000)
                            break
                    except Exception:
                        continue
                # 被判账号异常时按钮还在、页面也不跳
                try:
                    body = (await page.inner_text("body"))[:4000]
                    if "账号异常" in body:
                        return False
                except Exception:
                    pass
                try:
                    return not await btn.is_visible()
                except Exception:
                    return True
            except Exception:
                continue
        return False

    async def _try_save(self, page) -> bool:
        # 兜底：提交被拦时至少把答案存下来，下次能接着答
        for sel in (".btnSave, .workBtnIndex", ".btnSave"):
            try:
                btn = page.locator(sel).first
                if await btn.count() and await btn.is_visible():
                    await self.safe_click(btn)
                    await page.wait_for_timeout(1500)
                    log.info("已保存草稿")
                    return True
            except Exception:
                continue
        return False

    async def read_quiz_result(self, page, questions: list[Question]) -> None:
        try:
            body = await page.inner_text("body", timeout=3000)
        except Exception:
            return
        m = re.search(r"(得分|成绩)[:：]?\s*(\d+(?:\.\d+)?)", body)
        if m:
            log.info("测验得分: " + m.group(2))
