"""智慧树（知到）适配器。

结构与超星差异较大，因此单独实现；接口与基类一致，runner 无需感知。

* 登录      https://passport.zhihuishu.com/login
* 课程      https://online-web.zhihuishu.com/ 或 https://www.zhihuishu.com/learning
* 播放      video 直接在顶层页面（vjs_container），不需要切 frame
* 弹题      .topic-item / .dialog-read / .dialog-body
* 章节测验  .test-item / .exam-question
"""
from __future__ import annotations

import re
from typing import Any, Optional

from ..logger import log
from ..models import Course, Task
from ..quiz import Question
from .base import PlatformAdapter


class ZhihuishuAdapter(PlatformAdapter):
    name = "zhihuishu"
    home_url = "https://www.zhihuishu.com/"
    login_url = "https://passport.zhihuishu.com/login"

    COURSE_CARD = ".course-card, .clearfix.course, li.course"
    CHAPTER_ITEM = ".clearfix.video, .chapter-item, .studytime"
    CONTENT_IFRAME = ""
    VIDEO_QUESTION_POPUP = ".topic-item, .dialog-read, .videoquiz"
    QUESTION_ITEM = ".exam-question, .question-item, .test-item"
    QUESTION_STEM = ".question-title, .subject, .stem"
    QUESTION_OPTION = "li, .option, label"
    SUBMIT_BTN = ".submit-btn, .btn-submit, button:has-text('提交')"

    async def is_logged_in(self) -> bool:
        try:
            cookies = await self.page.context.cookies()
            for c in cookies:
                if c.get("name") in ("CASLOGC", "SESSION", "token") and c.get("value"):
                    return True
        except Exception:
            pass
        return "passport" not in (self.page.url or "")

    async def list_courses(self) -> list[Course]:
        try:
            await self.page.goto("https://www.zhihuishu.com/learning", wait_until="domcontentloaded")
        except Exception:
            pass
        await self.page.wait_for_timeout(3000)
        courses: list[Course] = []
        nodes = self.page.locator(self.COURSE_CARD)
        for i in range(await nodes.count()):
            node = nodes.nth(i)
            title = await self.text_of(node)
            if not title:
                continue
            href = ""
            try:
                href = await node.locator("a").first.get_attribute("href") or ""
            except Exception:
                pass
            cid = re.sub(r"\W+", "", href)[-16:] or ("zhs" + str(i))
            courses.append(Course(cid=cid, name=title.split("\n")[0][:60], url=href))
        log.info("发现 " + str(len(courses)) + " 门课程")
        return courses

    async def open_course(self, course: Course) -> None:
        try:
            if course.url:
                url = course.url if course.url.startswith("http") else "https:" + course.url
                await self.page.goto(url, wait_until="domcontentloaded")
            else:
                await self.page.get_by_text(course.name, exact=False).first.click(timeout=8000)
            await self.page.wait_for_timeout(3000)
        except Exception as e:
            log.error("打开课程失败: " + str(e))

    async def list_tasks(self, course: Course) -> list[Task]:
        tasks: list[Task] = []
        nodes = self.page.locator(self.CHAPTER_ITEM)
        for i in range(await nodes.count()):
            node = nodes.nth(i)
            title = await self.text_of(node)
            if not title:
                continue
            kind = "quiz" if ("测验" in title or "作业" in title or "考试" in title) else "video"
            tasks.append(Task(tid="z" + str(i), kind=kind, title=title.split("\n")[0][:80],
                              meta={"index": i}))
        log.info("课程「" + course.name + "」发现 " + str(len(tasks)) + " 个任务")
        return tasks

    async def open_task(self, task: Task) -> Optional[Any]:
        try:
            nodes = self.page.locator(self.CHAPTER_ITEM)
            await nodes.nth(int(task.meta.get("index", 0))).click(timeout=8000)
        except Exception as e:
            log.warning("点击任务失败: " + str(e))
        await self.page.wait_for_timeout(3000)
        return self.page

    async def extract_questions(self, page) -> list[Question]:
        questions: list[Question] = []
        try:
            items = page.locator(self.QUESTION_ITEM)
            n = await items.count()
        except Exception:
            return questions

        for i in range(n):
            root = items.nth(i)
            stem = await self.text_of(root.locator(self.QUESTION_STEM).first)
            if not stem:
                stem = await self.text_of(root)
            stem = re.sub(r"^\d+[\.、\s]*", "", stem).strip()
            if not stem:
                continue
            opts: list[str] = []
            nodes: list[Any] = []
            opt_loc = root.locator(self.QUESTION_OPTION)
            for j in range(await opt_loc.count()):
                t = await self.text_of(opt_loc.nth(j))
                if t and len(t) < 200:
                    opts.append(t)
                    nodes.append(opt_loc.nth(j))
            qtype = "multiple" if "多选" in stem else ("judge" if "判断" in stem else "single")
            questions.append(Question(index=i, stem=stem, qtype=qtype, options=opts,
                                      root=root, option_nodes=nodes))
        return questions

    async def answer_question(self, page, q: Question) -> None:
        for idx in q.answer:
            if isinstance(idx, int) and idx < len(q.option_nodes):
                await self.safe_click(q.option_nodes[idx])
                await page.wait_for_timeout(200)

    async def submit_quiz(self, page) -> bool:
        for sel in self.SUBMIT_BTN.split(","):
            sel = sel.strip()
            if not sel:
                continue
            try:
                btn = page.locator(sel).first
                if await btn.count():
                    await btn.click(timeout=5000)
                    await page.wait_for_timeout(2500)
                    return True
            except Exception:
                continue
        return False
