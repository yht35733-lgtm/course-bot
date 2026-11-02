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
  // 只在值真的需要改时才写：每轮都设一次会不停触发 ratechange / volumechange，
  // 平台的播放器把这些事件当成异常播放，学习时长就记不上，任务点永远不变绿
  if (opt.mute && !v.muted) { try { v.muted = true; v.volume = 0; } catch (e) {} }
  if (Math.abs((v.playbackRate || 1) - opt.rate) > 0.01) {
    try { v.playbackRate = opt.rate; } catch (e) {}
  }
  if (v.paused && !v.ended) { v.play().catch(function () {}); }
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

# 视频卡片的 data 属性里写着这门课的限制：doublespeed=1 就是不许倍速，
# 硬加速会被判违规，学习时长直接作废
JS_LIMIT = """
() => {
  const el = document.querySelector('.ans-attach-online');
  if (!el) return { rate: 0, drag: true, switchwin: true };
  let d = {};
  try { d = JSON.parse(el.getAttribute('data') || '{}'); } catch (e) { d = {}; }
  return {
    rate: Number(d.doublespeed || 0),
    drag: d.fastforward !== 'false',
    switchwin: d.switchwindow !== 'false'
  };
}
"""

# 用播放器自己的按钮起播：超星的上报逻辑（multimedia/log）绑在它自己的播放流程上，
# 直接拿 video 元素调 play() 只会让画面动，学习时长一条都不会发出去
JS_CLICK_PLAY = """
() => {
  // 一、优先走 Video.js 的 player API：播放状态、timeupdate、上报插件才会联动。
  // 直接对 <video> 调 play() 只有画面动，player 还以为自己是 vjs-paused。
  try {
    if (window.videojs) {
      let p = null;
      try { p = window.videojs.getPlayer('video'); } catch (e) {}
      if (!p) {
        const el = document.querySelector('.video-js');
        if (el && window.videojs) { p = window.videojs(el); }
      }
      if (p && typeof p.play === 'function') { p.play(); return 'videojs-api'; }
    }
  } catch (e) {}

  // 二、退而求其次：点播放按钮
  const sels = ['.vjs-big-play-button', '.vjs-play-control', '.prism-big-play-btn',
                '.dvp-play-btn', '.play-btn', '.videojs-big-play-button'];
  for (const s of sels) {
    const el = document.querySelector(s);
    if (el) {
      try { el.click(); return 'click:' + s; } catch (e) {}
    }
  }

  // 三、最后才用原生 play()
  const v = document.querySelector('video');
  if (v) { try { v.play(); return 'native'; } catch (e) {} }
  return '';
}
"""


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
        settled_at = 0.0
        started = time.time()
        max_wall = 3 * 3600

        # 平台记的是真实在线时长，倍速播完 wall-clock 只走了一半，任务点照样不通过。
        # 读不到倍速限制也按 1.0 保守处理。
        try:
            lim = await self._eval(JS_LIMIT)
            cap = float((lim or {}).get("rate") or 1.0)
            if rate > cap:
                log.info("按平台限制把倍速从 " + str(rate) + " 降到 " + str(cap))
                rate = cap
                force = force and cap >= 1.5
        except Exception:
            pass

        if force:
            await self._install_guard(rate)

        # 先点播放器的播放按钮，让它的初始化 + 上报流程正常跑起来
        if self.cfg.get("click_play", True):
            try:
                hit = await self._eval(JS_CLICK_PLAY)
                if hit:
                    log.info("已点击播放器按钮起播: " + str(hit))
                    await asyncio.sleep(1.5)
            except Exception:
                pass

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
            # 超星按 playingTime >= duration 判定，差一秒都可能不算。
            # 所以要么到比例阈值，要么已经贴到结尾
            # 平台按 playingTime >= duration 判通过，差半秒都可能不算，必须贴到结尾
            if d and (t / d >= settle or (d - t) <= 0.2):
                # 别急着收工：让播放器把最后一段心跳发完，平台那边才算得满学习时长
                if not settled_at:
                    settled_at = time.time()
                    log.info("已达完成阈值 " + str(round(t / d * 100, 1)) +
                             "%，等待平台同步学习时长")
                if time.time() - settled_at < 12:
                    try:
                        await self._eval(JS_DRIVE, {"rate": rate, "mute": mute})
                    except Exception:
                        pass
                    await asyncio.sleep(1.0)
                    continue
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
                # 课程写了"不可拖拽"时，nudge 反而会让这一节作废，宁可就地重载
                if stalls % 3 == 0 and self.cfg.get("allow_nudge", False):
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
