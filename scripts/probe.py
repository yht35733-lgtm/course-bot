"""诊断：登录态下页面到底长什么样。

    python scripts/probe.py

只读，不点任何按钮。输出候选 URL 的标题、课程相关链接、class 名分布，
并把最像课程页的那份 HTML 存到 data/debug/ 方便对照。
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from coursebot.browser import BrowserSession  # noqa: E402
from coursebot.config import load_config  # noqa: E402

CANDIDATES = [
    "https://i.chaoxing.com/",
    "https://mooc1.chaoxing.com/visit/interaction",
    "https://mooc1.chaoxing.com/",
]

JS_LINKS = """
() => Array.from(document.querySelectorAll('a')).filter(function (a) {
    return /courseId|courseid|mycourse|knowledge/i.test(a.getAttribute('href') || '');
  }).slice(0, 15).map(function (a) {
    return { text: (a.innerText || '').trim().slice(0, 40), href: a.getAttribute('href') };
  })
"""

JS_CLASSES = """
() => {
    const m = {};
    document.querySelectorAll('div,li,span').forEach(function (n) {
        const c = n.className;
        if (typeof c === 'string' && c && /course|clazz/i.test(c)) { m[c] = (m[c] || 0) + 1; }
    });
    return m;
}
"""

JS_FRAMES = """
() => Array.from(document.querySelectorAll('iframe')).map(function (f) {
    return { id: f.id, src: (f.getAttribute('src') || '').slice(0, 120) };
})
"""


async def main() -> int:
    cfg = load_config()
    out_dir = ROOT / "data" / "debug"
    out_dir.mkdir(parents=True, exist_ok=True)

    headless = "--headless" in sys.argv
    print("headless =", headless)
    async with BrowserSession(cfg["browser"], headless=headless) as sess:
        page = await sess.new_page()

        for url in CANDIDATES:
            print("=" * 66)
            print("TRY", url)
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            except Exception as e:
                print("  goto failed:", e)
                continue
            await page.wait_for_timeout(3000)
            try:
                title = await page.title()
            except Exception:
                title = "?"
            html = await page.content()
            print("  final url :", page.url)
            print("  title     :", title)
            print("  html len  :", len(html))

            try:
                links = await page.evaluate(JS_LINKS)
                print("  course links (" + str(len(links)) + "):")
                for it in links[:10]:
                    print("     -", it["text"][:30], "|", (it["href"] or "")[:70])
            except Exception as e:
                print("  link scan failed:", e)

            try:
                classes = await page.evaluate(JS_CLASSES)
                top = sorted(classes.items(), key=lambda kv: -kv[1])[:12]
                print("  class names:", json.dumps(dict(top), ensure_ascii=False))
            except Exception as e:
                print("  class scan failed:", e)

            try:
                frames = await page.evaluate(JS_FRAMES)
                print("  iframes:", json.dumps(frames, ensure_ascii=False)[:400])
            except Exception as e:
                print("  frame scan failed:", e)

            name = url.replace("https://", "").replace("/", "_").strip("_") or "root"
            (out_dir / (name + ".html")).write_text(html, encoding="utf-8")
            print("  saved ->", out_dir / (name + ".html"))

        # 直接问接口要课程列表，这条路最稳
        print("=" * 66)
        print("API /mycourse/backclazzdata")
        try:
            resp = await page.request.get(
                "https://mooc1-api.chaoxing.com/mycourse/backclazzdata",
                timeout=30000,
            )
            body = await resp.text()
            print("  status:", resp.status, "len:", len(body))
            print("  head:", repr(body[:200]))
            data = json.loads(body)
            items = (data.get("channelList") or []) if isinstance(data, dict) else []
            print("  channelList:", len(items))
            for it in items[:10]:
                print("   *", it.get("cataName") or "", "|", it.get("content", {}).get("course", {}).get("data", [{}])[0].get("name", "") if isinstance(it.get("content"), dict) else "")
            (out_dir / "backclazzdata.json").write_text(
                json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
            print("  saved ->", out_dir / "backclazzdata.json")
        except Exception as e:
            print("  api failed:", e)

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
