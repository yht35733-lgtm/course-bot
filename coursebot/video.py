"""视频进度引擎。

策略
----
1. 注入倍速守护（对抗平台把 playbackRate 改回 1.0）。
2. 轮询取最大的那个 video 元素的状态，paused 就 play()，卡住就 reload。
3. 每个 tick 检查弹题容器；命中则回调 on_quiz，由答题引擎接管。
4. 用 currentTime / duration 判断完成，而不是等 ended（很多平台播完就跳转）。
5. 播放器常在 iframe 里，reload 后 frame 会失效；target_getter 负责重新取。
"""
from __future__ import annotations

import asyncio
import time
from typing import Optional

from .logger import log
from .models import Task, TaskResult

JS_STATE = """
() => {
  const vs = Array.from(document.querySelectorAll('video'));
  if (!vs.length) return null;
  const v = vs.sort(function (a, b) { return (b.duration || 0) - (a.duration || 0); })[0];
  return {
    t: v.currentTime || 0,
    d: v.duration || 0,
    paused: !!v.paused,
    ended: !!v.ended,
    ready: v.readyState,
    rate: v.playbackRate,
    err: v.error ? String(v.error.code) : null
  };
}
"""

JS_DRIVE = """
(opt) => {
  const vs = Array.from(document.querySelectorAll('video'));
  if (!vs.length) return false;
  const v = vs.sort(function (a, b) { return (b.duration || 0) - (a.duration || 0); })[0];
  if (opt.mute) { try { v.muted = true; v.volume = 0; } catch (e) {} }
  try { v.playbackRate = opt.rate; } catch (e) {}
  if (v.paused) { v.play().catch(function () {}); }
  return true;
}
"""

JS_NUDGE = """
() => {
  const vs = Array.from(document.querySelectorAll('video'));
  if (!vs.length) return false;
  const v = vs.sort(function (a, b) { return (b.duration || 0) - (a.duration || 0); })[0];
  const cur = v.currentTime + 1.0;
  if (v.duration && cur < v.duration - 1) { v.currentTime = cur; }
  v.play().catch(function () {});
  return true;
}
"""

JS_PAUSE = "() => { document.querySelectorAll('video').forEach(function (v) { v.pause(); }); }"


class VideoEngine:
    def __init__(self, page, cfg: dict, on_quiz=None, question_selector=None,
                 target_getter=None, gate=None, on_progress=None):
        self.page = page
        self.cfg = cfg or {}
        self.on_quiz = on_quiz
        self.question_selector = question_selector
        self.target_getter = target_getter
        self.gate = gate
        self.on_progress = on_progress
        self._guard_installed = False

    async def _eval(self, js: str, arg=None):
        """在目标上执行；失败且能重新取 frame 时刷新一次再试。"""
        try:
            if arg is None:
                return await self.page.evaluate(js)
            return await self.page.evaluate(js, arg)
        except Exception:
            if not self.target_getter:
                raise
            try:
                fresh = await self.target_getter()
            except Exception:
                raise
            if fresh is None or fresh is self.page:
                raise
            self.page = fresh
            self._guard_installed = False
            if arg is None:
                return await self.page.evaluate(js)
            return await self.page.evaluate(js, arg)

    async def _install_guard(self, rate: float) -> None:
        from .browser import RATE_GUARD_JS
        try:
            await self._eval(RATE_GUARD_JS, rate)
            self._guard_installed = True
        except Exception:
            self._guard_installed = False

    async def play(self, task: Task, rate: Optional[float] = None) -> TaskResult:
        rate = float(rate or self.cfg.get("rate", 1.5))
        settle = float(self.cfg.get("settle_ratio", 0.98))
        tick = float(self.cfg.get("tick", 3.0))
        stall_limit = int(self.cfg.get("stall_limit", 40))
        max_reload = int(self.cfg.get("max_reload", 3))
        mute = bool(self.cfg.get("mute", True))
        force = bool(self.cfg.get("force_rate", True))

        reloads = 0
        stalls = 0
        last_t = -1.0
        started = time.time()
        max_wall = 3 * 3600

        if force:
            await self._install_guard(rate)

        while True:
            if self.gate is not None:
                if not self.gate.active():
                    # 暂停：先把播放器按停，再等在闸门上
                    try:
                        await self._eval(JS_PAUSE)
                    except Exception:
                        pass
                await self.gate.wait()
                if self.gate.stopped:
                    return TaskResult(task, False, "已手动停止")

            if time.time() - started > max_wall:
                return TaskResult(task, False, "超时（3h），疑似被限速")

            if self.on_quiz and self.question_selector:
                visible = False
                try:
                    visible = await self.page.locator(self.question_selector).first.is_visible(timeout=500)
                except Exception:
                    visible = False
                if visible:
                    log.info("检测到弹题，暂停视频并交给答题引擎")
                    try:
                        await self._eval(JS_PAUSE)
                    except Exception:
                        pass
                    ok = await self.on_quiz()
                    log.info("弹题结果: " + ("通过" if ok else "未通过"))

            state = None
            try:
                state = await self._eval(JS_STATE)
            except Exception:
                state = None

            if not state:
                await asyncio.sleep(tick)
                stalls += 1
                if stalls > 8:
                    return TaskResult(task, False, "页面无 video 元素")
                continue

            t, d = state["t"], state["d"]
            if self.on_progress and d:
                try:
                    self.on_progress(min(t / d, 1.0))
                except Exception:
                    pass
            if d and t / d >= settle:
                ratio = t / d
                return TaskResult(task, True, "进度 " + str(round(ratio * 100, 1)) + "%", ratio)
            if state.get("ended"):
                return TaskResult(task, True, "已播放结束", 1.0)

            try:
                await self._eval(JS_DRIVE, {"rate": rate, "mute": mute})
            except Exception:
                pass

            if abs(t - last_t) < 0.05:
                stalls += 1
                if stalls % 5 == 0:
                    log.debug("进度停滞 " + str(stalls) + " 次，当前 " + str(round(t, 1)) + "s")
                if stalls % 3 == 0:
                    try:
                        await self._eval(JS_NUDGE)
                    except Exception:
                        pass
                if stalls >= stall_limit:
                    reloads += 1
                    if reloads > max_reload:
                        return TaskResult(task, False, "反复卡住已放弃，进度 " + str(round(t, 1)) + "s")
                    log.warning("重载页面续播，第 " + str(reloads) + " 次")
                    stalls = 0
                    reloader = getattr(self.page, "page", self.page)
                    try:
                        await reloader.reload(wait_until="domcontentloaded")
                    except Exception:
                        pass
                    await asyncio.sleep(2.0)
                    if self.target_getter:
                        try:
                            fresh = await self.target_getter()
                            if fresh is not None:
                                self.page = fresh
                        except Exception:
                            pass
                    if force:
                        await self._install_guard(rate)
            else:
                stalls = 0
                last_t = t
                if d:
                    log.debug(
                        "进度 " + str(round(t, 1)) + "/" + str(round(d, 1)) + "s (" +
                        str(round(t / d * 100, 1)) + "%)"
                    )

            await asyncio.sleep(tick)
