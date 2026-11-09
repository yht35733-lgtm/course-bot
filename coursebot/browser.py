"""Playwright 浏览器会话（持久化 profile + 基础反检测）。

profile 复用意味着登录态只做一次：先跑 login，之后 run 直接带着 cookie 进场。
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from .config import ROOT

# 抹掉 navigator.webdriver 等最容易被检测的指纹
STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
Object.defineProperty(navigator, 'languages', { get: () => ['zh-CN', 'zh', 'en'] });
Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
window.chrome = window.chrome || { runtime: {} };
"""

# 倍速守护：把 playbackRate 的 setter 固定到目标值，并拦截 ratechange 回弹
RATE_GUARD_JS = """
(rate) => {
  window.__cbTargetRate = rate;
  const proto = HTMLMediaElement.prototype;
  const desc = Object.getOwnPropertyDescriptor(proto, 'playbackRate');
  if (desc && desc.set && desc.set.__cbPatched) { return 'already'; }
  if (!desc || !desc.set) { return 'unsupported'; }
  const patched = function (v) { return desc.set.call(this, window.__cbTargetRate || v); };
  patched.__cbPatched = true;
  Object.defineProperty(proto, 'playbackRate', {
    get: desc.get, set: patched, configurable: true, enumerable: desc.enumerable,
  });
  document.addEventListener('ratechange', function (e) {
    const m = e.target;
    if (m && m.tagName === 'VIDEO' && Math.abs(m.playbackRate - window.__cbTargetRate) > 0.01) {
      try { m.playbackRate = window.__cbTargetRate; } catch (err) {}
    }
  }, true);
  document.querySelectorAll('video').forEach(function (m) {
    try { m.playbackRate = window.__cbTargetRate; } catch (err) {}
  });
  return 'patched';
}
"""


class BrowserSession:
    """异步上下文管理器。用法：

    async with BrowserSession(cfg["browser"]) as sess:
        page = await sess.new_page()
    """

    def __init__(self, cfg: dict[str, Any], headless: bool | None = None):
        self.cfg = cfg
        self.headless = cfg.get("headless", False) if headless is None else headless
        self._pw = None
        self._context = None
        # 配置里留空时固定落到项目下的 .profiles，避免依 cwd 漂移
        self.profile_dir = Path(cfg.get("profile_dir") or (ROOT / ".profiles"))

    async def __aenter__(self) -> "BrowserSession":
        from playwright.async_api import async_playwright

        self.profile_dir.mkdir(parents=True, exist_ok=True)
        if self.headless:
            # 实测：同一个视频，headless 上报接口返回 isPassed=false，
            # 有头模式返回 isPassed=true —— 超星会识别无头浏览器并判定无效
            from .logger import log as _log
            _log.warning("无头模式下平台可能判定学习记录无效（isPassed=false），"
                         "想正常记账请关掉「无窗口运行」")
        self._pw = await async_playwright().start()

        args = [
            "--disable-blink-features=AutomationControlled",
            "--mute-audio",
            "--no-first-run",
            "--no-default-browser-check",
            # 窗口被遮挡或切到后台时，Chromium 会节流定时器和渲染，
            # 播放器跟着停摆、弹题也不弹——这三个开关关掉节流
            "--disable-background-timer-throttling",
            "--disable-backgrounding-occluded-windows",
            "--disable-renderer-backgrounding",
        ]
        # 把窗口挪到屏幕外，免得跑的时候一直抢前台焦点；代价是拿不到焦点态
        if self.cfg.get("offscreen"):
            args += ["--window-position=-32000,-32000"]
        kwargs: dict[str, Any] = {
            "user_data_dir": str(self.profile_dir),
            "headless": self.headless,
            "args": args,
            "locale": self.cfg.get("locale", "zh-CN"),
            "timezone_id": self.cfg.get("timezone", "Asia/Shanghai"),
            "viewport": self.cfg.get("viewport", {"width": 1440, "height": 900}),
            "slow_mo": self.cfg.get("slow_mo", 0) or 0,
        }
        if self.cfg.get("channel"):
            kwargs["channel"] = self.cfg["channel"]
        if self.cfg.get("user_agent"):
            kwargs["user_agent"] = self.cfg["user_agent"]

        self._context = await self._launch(kwargs)
        self._context.set_default_timeout(self.cfg.get("navigation_timeout", 45000))
        # 超星动不动就 alert("请刷新页面后重试") / beforeunload 确认框。
        # 原生对话框一弹，渲染进程被冻住，之后所有 locator 操作都卡在
        # "performing click action" 直到超时。这里统一自动确认掉。
        self._context.on("dialog", self._on_dialog)
        await self._context.add_init_script(STEALTH_JS)
        await self.restore_state()
        return self

    @staticmethod
    async def _on_dialog(dialog) -> None:
        try:
            from .logger import log as _log
            _log.info("自动处理对话框: " + (dialog.message or "")[:60])
        except Exception:
            pass
        try:
            await dialog.accept()
        except Exception:
            try:
                await dialog.dismiss()
            except Exception:
                pass

    LOCK_NAMES = ("SingletonLock", "SingletonCookie", "SingletonSocket",
                  "lockfile", "DevToolsActivePort")

    def _clear_locks(self) -> int:
        """上一个浏览器被强杀时会留下 profile 锁，不清掉新实例就起不来（Edge 直接退出码 21）。"""
        removed = 0
        if not self.profile_dir.exists():
            return 0
        for name in self.LOCK_NAMES:
            for p in list(self.profile_dir.rglob(name)):
                try:
                    p.unlink()
                    removed += 1
                except Exception:
                    continue
        return removed

    async def _launch(self, kwargs: dict):
        """优先用内置 chromium；没装时回落到本机 Chrome / Edge。"""
        attempts = [dict(kwargs)]
        if not kwargs.get("channel"):
            for ch in ("chrome", "msedge"):
                k = dict(kwargs)
                k["channel"] = ch
                attempts.append(k)

        last: Exception | None = None
        for k in attempts:
            try:
                return await self._pw.chromium.launch_persistent_context(**k)
            except Exception as e:  # 浏览器缺失 / profile 被占用
                last = e
                hint = str(e).lower()
                stuck = ("has been closed" in hint or "singletonlock" in hint
                         or "exitcode=21" in hint or "profile" in hint and "lock" in hint)
                if stuck and self._clear_locks():
                    try:
                        return await self._pw.chromium.launch_persistent_context(**k)
                    except Exception as e2:
                        last = e2
        raise last if last else RuntimeError("无法启动浏览器")

    async def __aexit__(self, *exc) -> None:
        try:
            if self._context:
                await self._context.close()
        finally:
            if self._pw:
                await self._pw.stop()

    @property
    def state_file(self) -> Path:
        return self.profile_dir / "state.json"

    async def save_state(self) -> Path:
        """把 cookie / localStorage 导出成 JSON。Chrome 关闭时会丢弃 session cookie，
        有这份副本就不必每次重新扫码。"""
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        await self.context.storage_state(path=str(self.state_file))
        return self.state_file

    async def restore_state(self) -> int:
        if not self.state_file.exists():
            return 0
        try:
            data = json.loads(self.state_file.read_text(encoding="utf-8"))
        except Exception:
            return 0
        cookies = data.get("cookies") or []
        restored = 0
        for c in cookies:
            item = {k: c.get(k) for k in
                    ("name", "value", "domain", "path", "expires", "httpOnly", "secure", "sameSite")}
            if item.get("name") and item.get("domain"):
                try:
                    await self.context.add_cookies([item])
                    restored += 1
                except Exception:
                    continue
        return restored

    @property
    def context(self):
        if not self._context:
            raise RuntimeError("BrowserSession 未启动")
        return self._context

    async def new_page(self):
        page = await self.context.new_page()
        page.set_default_timeout(self.cfg.get("navigation_timeout", 45000))
        return page

    async def install_rate_guard(self, page, rate: float) -> str:
        try:
            return await page.evaluate(RATE_GUARD_JS, rate)
        except Exception:
            return "failed"

    async def goto(self, page, url: str, wait: str = "domcontentloaded") -> None:
        try:
            await page.goto(url, wait_until=wait)
        except Exception:
            # 平台常有长连接/埋点导致 load 事件不触发，忽略超时继续
            await asyncio.sleep(1.0)

    async def wait_ready(self, page, timeout: float = 15.0) -> None:
        try:
            await page.wait_for_load_state("networkidle", timeout=int(timeout * 1000))
        except Exception:
            pass
