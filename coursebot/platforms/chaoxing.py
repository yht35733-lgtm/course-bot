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
                await self.page.wait_for_timeout(1200)
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
                    await self.page.wait_for_timeout(1200)
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

    TABS_SEL = "#prev_tab li, .prev_tab li, #prev_list li"

    async def switch_job_tab(self, index: int) -> bool:
        """点分组标签。

        两个坑：缓存的 _tabs_frame 换小节后会失效（element is not visible），
        有些皮肤把标签条整条隐藏，Playwright 的 click 判不可见直接超时。
        所以每次都重新找 frame，点不动就退回 JS 的 el.click()。
        """
        for _ in range(3):
            for f in self.page.frames:
                try:
                    li = f.locator(self.TABS_SEL).nth(index)
                    if not await li.count():
                        continue
                except Exception:
                    continue
                try:
                    await li.click(timeout=3000)
                except Exception:
                    try:
                        await li.evaluate("el => el.click()")
                    except Exception:
                        continue
                self._tabs_frame = f
                await self.page.wait_for_timeout(2500)
                return True
            await self.page.wait_for_timeout(800)
        log.warning("切换任务点标签失败: index=" + str(index) + "（找不到可点的 li）")
        return False

    async def _wait_learning_frame(self, seconds: int):
        """轮询等 knowledge/cards 出现。

        目录点击会整页跳到 studentstudy，frame 是新的，必须每秒重扫。
        """
        for _ in range(max(1, seconds)):
            await self.page.wait_for_timeout(1000)
            for f in self.page.frames:
                if "knowledge/cards" in (f.url or ""):
                    return f
        return None

    async def open_task(self, task: Task) -> Optional[Any]:
        """点击小节标题，返回真正承载播放器/题目的 frame。

        坑：Playwright 的 click 会等"点击引发的导航"结束才算完。超星目录点一下
        就是整页跳 studentstudy，8 秒预算常常只等到一半，于是报 Timeout——
        其实页面早就打开了。改用 JS 点击（不等导航），自己轮询目标 frame。
        """
        frame = await self.catalog_frame()
        if frame is None:
            return None
        idx = int(task.meta.get("index", 0))
        node = frame.locator(self.TASK_ITEM).nth(idx)

        fired = False
        try:
            await node.evaluate("el => el.click()")
            fired = True
        except Exception as e:
            log.warning("JS 点击小节失败: " + str(e).split("\n")[0])

        target = await self._wait_learning_frame(6)
        if target is None:
            try:
                # no_wait_after 让 Playwright 别等导航，点完立刻返回
                await node.click(timeout=8000, no_wait_after=True)
                fired = True
            except Exception as e:
                log.warning("点击小节超时: " + str(e).split("\n")[0])
            target = await self._wait_learning_frame(8)

        if target is None:
            if not fired:
                return None
            log.warning("小节已点击但没等到 learning frame，按当前页面继续")
            return await self.content_frame(self.page)
        return target

    # ---------------- 答题 ----------------

    def decode(self, text: str) -> str:
        """把字体反爬的乱码还原成真字。

        映射表里给的是康熙部首（⼀ ⽇ ⽯），和真字看着一样但编码不同，
        模型对这两套字符的理解有偏差，统一转成普通汉字。
        """
        from ..fontdecode import KX_TAB
        m = getattr(self, "font_map", None)
        if not m or not text:
            return text
        return "".join(m.get(ch, ch) for ch in text).translate(KX_TAB)

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

    # 直接调超星自己的提交函数：点按钮会被顶层遮罩（章节详情顶栏 / #workpop）
    # 拦住，Playwright 一直重试到超时，最后什么都没发出去。
    # 提交的真正开关在 submitCheckTimes()：它把 form1 POST 到 addStudentWorkNewWeb。
    # 直接调它会报「无效的参数：code-1」，因为 toadd() 会先把已答题目 id 写进
    # #answerwqbid；而 toadd 只在 btnBlueSubmit() -> /work/validate -> 确认框
    # 这条链里被调用，确认框又是 top.workPop() 弹出的，抓不到也点不到。
    # 所以这里手动补齐 toadd 做的那几个字段，再直接提交。
    JS_SUBMIT = """
() => {
  try {
    ['setClozeTextAnswers', 'setConnLineAnswer', 'setSortQuesAnswer', 'setAllClozeAnswer',
     'setReadComprehensionAnswer', 'setProceduralAnswer', 'updateAllFillinBlanksData'
    ].forEach(function (n) {
      try { if (typeof window[n] === 'function') window[n](); } catch (e) {}
    });
    var ids = Array.prototype.slice.call(document.querySelectorAll('[id^="answertype"]'))
      .map(function (n) { return n.id.replace('answertype', ''); })
      .filter(function (s) { return /^[0-9]+$/.test(s); });
    if (!ids.length) return '';
    var wq = document.getElementById('answerwqbid');
    if (wq) wq.value = ids.join(',') + ',';
    var pf = document.getElementById('pyFlag');
    if (pf) pf.value = '';
    if (typeof submitCheckTimes === 'function') { submitCheckTimes(); return 'direct'; }
  } catch (e) {}
  return '';
}
"""

    JS_SAVE = """
() => {
  try { if (typeof noSubmit === 'function') { noSubmit(); return 'js'; } } catch (e) {}
  const el = document.querySelector('.btnSave');
  if (el) { el.click(); return 'el'; }
  return '';
}
"""

    # 视频弹题（ananas 播放器自带的那套）没有 submitCheckTimes / btnBlueSubmit，
    # 只有自己的提交按钮，点完弹层就收。用章测那套提交必然扑空并反复暂停视频。
    POPUP_SUBMIT = (".ans-videoquiz-submit, .ans-videoquiz-btn, .ans-videoquiz .sub-btn,"
                    " .videoquiz-submit, .ans-videoquiz input[type=submit]")

    JS_POPUP_SUBMIT = """
() => {
  const box = document.querySelector('.ans-videoquiz, .videoquiz, #videoquiz');
  const scope = box || document;
  const nodes = Array.prototype.slice.call(
    scope.querySelectorAll('a, button, input[type=submit], span, div'));
  for (let i = 0; i < nodes.length; i++) {
    const n = nodes[i];
    if (n.children.length) continue;
    const t = (n.innerText || n.value || '').replace(/\s+/g, '');
    if (t === '提交' || t === '确定' || t === '提交答案') { n.click(); return 'text:' + t; }
  }
  const el = scope.querySelector('.ans-videoquiz-submit, .ans-videoquiz-btn');
  if (el) { el.click(); return 'sel'; }
  if (box) {
    // 有些版本选完选项就自动交卷，直接触发一次 change 兜底
    const last = box.querySelector('input:checked');
    if (last) { last.dispatchEvent(new Event('change', {bubbles: true})); return 'change'; }
  }
  return '';
}
"""

    async def submit_popup_quiz(self, page) -> bool:
        await self.clear_mask(page)
        fired = ""
        try:
            fired = await page.evaluate(self.JS_POPUP_SUBMIT)
        except Exception as e:
            log.debug("弹题提交 JS 失败: " + str(e))
        if not fired:
            for sel in self.POPUP_SUBMIT.split(","):
                sel = sel.strip()
                if not sel:
                    continue
                try:
                    btn = page.locator(sel).first
                    if await btn.count() and await self.safe_click(btn):
                        fired = sel
                        break
                except Exception:
                    continue
        if not fired:
            return False
        log.info("弹题已提交: " + str(fired))
        await page.wait_for_timeout(1500)
        return True

    async def submit_quiz(self, page) -> bool:
        """先提交；提交不成功就退而点「暂时保存」，至少别丢答案。"""
        ok = await self._try_submit(page)
        if not ok:
            log.warning("提交没成功，改为点「暂时保存」保住答案")
            await self._try_save(page)
        # 提交/保存后都会弹层，遮罩不清掉页面就滑不动了
        await self.clear_mask(page)
        return ok

    async def _confirm_dialog(self, page) -> None:
        """交卷确认 / 保存提示 / 验证码弹层，挨个点「确定」。"""
        for sel in ("#submitBackOk", ".layui-layer-btn0", ".bluebtn",
                    "a:has-text('确定')", "button:has-text('确定')",
                    "a:has-text('确认')", "button:has-text('确认')"):
            try:
                c = page.locator(sel).first
                if await c.count() and await c.is_visible():
                    await self.safe_click(c)
                    await page.wait_for_timeout(1500)
                    return
            except Exception:
                continue

    async def _try_submit(self, page) -> bool:
        await self.clear_mask(page)
        fired = ""
        try:
            fired = await page.evaluate(self.JS_SUBMIT)
        except Exception as e:
            log.debug("JS 提交调用失败: " + str(e))
        if not fired:
            # 兜底：走超星自己的确认链（有些皮肤 JS 函数名不一样）
            try:
                fired = await page.evaluate(
                    "() => { try { btnBlueSubmit(); return 'btn'; } catch (e) { return ''; } }")
            except Exception:
                fired = ""
        if not fired:
            for sel in (".btnSubmit", ".completeBtn"):
                try:
                    btn = page.locator(sel).first
                    if await btn.count() and await self.safe_click(btn):
                        fired = "click"
                        break
                except Exception:
                    continue
        if not fired:
            log.warning("提交入口没找到（submitCheckTimes / btnBlueSubmit 都不可用）")
            return False
        log.info("已触发提交: " + fired)

        await page.wait_for_timeout(3500)
        await self._confirm_dialog(page)
        await page.wait_for_timeout(2500)

        # 提交成功会跳成绩页，按钮也就消失了
        try:
            url = (getattr(page, "url", "") or "")
        except Exception:
            url = ""
        if "selectWorkQuestion" in url or "viewWork" in url:
            return True
        try:
            body = (await page.inner_text("body"))[:6000]
        except Exception:
            body = ""
        if "账号异常" in body:
            return False
        if "无效的参数" in body:
            log.warning("提交被服务端拒绝：无效的参数")
            return False
        try:
            btn = page.locator(".btnSubmit").first
            if await btn.count():
                return not await btn.is_visible()
        except Exception:
            pass
        return True

    async def _try_save(self, page) -> bool:
        # 兜底：提交没成功时至少把答案存下来，下次能接着答
        try:
            if await page.evaluate(self.JS_SAVE):
                await page.wait_for_timeout(1500)
                log.info("已保存草稿")
                return True
        except Exception:
            pass
        try:
            btn = page.locator(".btnSave").first
            if await btn.count() and await self.safe_click(btn):
                await page.wait_for_timeout(1500)
                log.info("已保存草稿")
                return True
        except Exception:
            pass
        return False

    async def read_quiz_result(self, page, questions: list[Question]) -> None:
        try:
            body = await page.inner_text("body", timeout=3000)
        except Exception:
            return
        m = re.search(r"(得分|成绩)[:：]?\s*(\d+(?:\.\d+)?)", body)
        if m:
            log.info("测验得分: " + m.group(2))
