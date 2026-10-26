"""视频引擎端到端测试。

用一个可编程的假 video 元素模拟平台播放器，验证：
* 倍速守护能否把平台改回 1.0 的操作拦截住；
* 引擎能否把进度推到 settle_ratio 并正常返回；
* 无 video 的页面能否在有限次数内放弃而不是死循环。

浏览器优先级：内置 chromium -> 本机 Chrome -> 本机 Edge；都没有则 skip。
"""
import asyncio
from pathlib import Path

import pytest

from coursebot.models import Task

FIXTURE = Path(__file__).resolve().parent / "fixture_player.html"


async def _launch(pw):
    for kwargs in ({}, {"channel": "chrome"}, {"channel": "msedge"}):
        try:
            return await pw.chromium.launch(headless=True, **kwargs)
        except Exception:
            continue
    return None


async def _with_page(uri: str, fn):
    from playwright.async_api import async_playwright

    pw = await async_playwright().start()
    browser = await _launch(pw)
    if browser is None:
        await pw.stop()
        return "NO_BROWSER"
    try:
        page = await browser.new_page()
        await page.goto(uri)
        return await fn(page)
    finally:
        await browser.close()
        await pw.stop()


BASE_CFG = {
    "rate": 2.0,
    "force_rate": True,
    "mute": True,
    "settle_ratio": 0.9,
    "tick": 0.1,
    "stall_limit": 60,
    "max_reload": 0,
}


def test_video_reaches_settle_ratio():
    from coursebot.video import VideoEngine

    async def fn(page):
        engine = VideoEngine(page, dict(BASE_CFG))
        return await engine.play(Task(tid="t1", kind="video", title="demo"))

    res = asyncio.run(_with_page(FIXTURE.as_uri(), fn))
    if res == "NO_BROWSER":
        pytest.skip("没有可用浏览器")
    assert res.ok, res.detail
    assert res.score is not None and res.score >= 0.9


def test_rate_guard_blocks_reset():
    from coursebot.browser import RATE_GUARD_JS

    async def fn(page):
        await page.evaluate(RATE_GUARD_JS, 2.0)
        return await page.evaluate(
            "() => { const v = document.querySelector('video');"
            " v.playbackRate = 1.0; return v.playbackRate; }"
        )

    got = asyncio.run(_with_page(FIXTURE.as_uri(), fn))
    if got == "NO_BROWSER":
        pytest.skip("没有可用浏览器")
    assert abs(got - 2.0) < 0.01


def test_missing_video_gives_up():
    from coursebot.video import VideoEngine

    async def fn(page):
        await page.set_content("<html><body><p>no video here</p></body></html>")
        engine = VideoEngine(page, {"tick": 0.01, "stall_limit": 2})
        return await engine.play(Task(tid="t2", kind="video", title="empty"))

    res = asyncio.run(_with_page("about:blank", fn))
    if res == "NO_BROWSER":
        pytest.skip("没有可用浏览器")
    assert res.ok is False
    assert "video" in res.detail
