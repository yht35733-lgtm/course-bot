"""诊断单个课程的章节 DOM。python scripts/probe_course.py 电工基础"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from coursebot.browser import BrowserSession  # noqa: E402
from coursebot.config import load_config  # noqa: E402
from coursebot.platforms.chaoxing import ChaoxingAdapter  # noqa: E402

JS_CLASSES = """
() => {
  const m = {};
  document.querySelectorAll('div,li,ul,a,span').forEach(function (n) {
    const c = n.className;
    if (typeof c === 'string' && c && /chapter|catalog|pos|unit|know/i.test(c)) {
      m[c.trim()] = (m[c.trim()] || 0) + 1;
    }
  });
  return m;
}
"""

JS_CLICKABLE = """
() => {
  const out = [];
  document.querySelectorAll('a,div,li,span').forEach(function (n) {
    const h = n.getAttribute && (n.getAttribute('href') || '');
    const oc = n.getAttribute && (n.getAttribute('onclick') || '');
    const dt = n.getAttribute && (n.getAttribute('data-url') || '');
    if (/chapterId|knowledge|toOld|stucoursemiddle|work/i.test(h + oc + dt)) {
      const t = (n.innerText || '').trim().replace(/\s+/g, ' ').slice(0, 40);
      if (t) { out.push({ tag: n.tagName, cls: (n.className || '').toString().slice(0, 50), text: t, href: h.slice(0, 80), onclick: oc.slice(0, 80) }); }
    }
  });
  return out.slice(0, 25);
}
"""

JS_FRAMES = """
() => Array.from(document.querySelectorAll('iframe')).map(function (f) {
  return { id: f.id, name: f.name, src: (f.getAttribute('src') || '').slice(0, 140) };
})
"""

JS_BODY_SNIP = """
() => (document.body.innerText || '').slice(0, 1500)
"""

JS_ALL = """
() => Array.from(document.querySelectorAll('a,button')).slice(0, 70).map(function (a) {
  return { t: (a.innerText || '').trim().replace(/ +/g, ' ').slice(0, 30),
           h: (a.getAttribute('href') || '').slice(0, 90) };
})
"""


async def main() -> int:
    key = sys.argv[1] if len(sys.argv) > 1 else "电工基础"
    cfg = load_config()
    out_dir = ROOT / "data" / "debug"
    out_dir.mkdir(parents=True, exist_ok=True)

    async with BrowserSession(cfg["browser"], headless=True) as sess:
        page = await sess.new_page()
        adapter = ChaoxingAdapter(page, cfg)
        await adapter.goto_home()
        courses = await adapter.list_courses()
        target = next((c for c in courses if key in c.name), None)
        if not target:
            print("没找到课程:", key)
            return 1
        print("目标课程:", target)
        await adapter.open_course(target)
        await page.wait_for_timeout(4000)
        print("URL:", page.url)
        print("TITLE:", await page.title())
        print("HTML LEN:", len(await page.content()))

        # 课程门户默认停在"任务"tab，章节列表要点"章节"才加载
        print("\n--- 点击「章节」 ---")
        clicked = False
        for label in ("章节",):
            try:
                loc = page.get_by_text(label, exact=True).first
                if await loc.count():
                    await loc.click(timeout=6000)
                    clicked = True
                    break
            except Exception as e:
                print("   click failed:", e)
        print("   clicked =", clicked)
        await page.wait_for_timeout(4000)
        print("   url now:", page.url)

        print("\n--- iframes ---")
        for f in await page.evaluate(JS_FRAMES):
            print("  ", f)

        print("\n--- 含 chapter/catalog/pos 的 class ---")
        cls = await page.evaluate(JS_CLASSES)
        for k, v in sorted(cls.items(), key=lambda kv: -kv[1])[:30]:
            print("  %-55s %s" % (k, v))

        print("\n--- 可点击且带 chapterId/knowledge 的元素 ---")
        for it in await page.evaluate(JS_CLICKABLE):
            print("  ", it["tag"], "|", it["cls"], "|", it["text"], "|", it["href"], "|", it["onclick"])

        print("\n--- 章节 iframe 内部 ---")
        frame = None
        for f in page.frames:
            if "studentcourse" in (f.url or ""):
                frame = f
                break
        if frame is None:
            print("   没找到 studentcourse frame")
        else:
            print("   frame url:", frame.url[:120])
            await frame.wait_for_timeout(2500)
            try:
                fc = await frame.evaluate(JS_CLASSES)
                for k, v in sorted(fc.items(), key=lambda kv: -kv[1])[:25]:
                    print("   class %-50s %s" % (k, v))
            except Exception as e:
                print("   class scan failed:", e)
            try:
                fl = await frame.evaluate(JS_ALL)
                print("   links (%d):" % len(fl))
                for it in fl[:25]:
                    print("      %-30s %s" % (it["t"], it["h"]))
            except Exception as e:
                print("   link scan failed:", e)
            try:
                txt = await frame.evaluate("() => (document.body.innerText || '').slice(0, 1200)")
                print("   --- frame text ---")
                print(txt)
            except Exception as e:
                print("   text failed:", e)
            try:
                (out_dir / "chapter_frame.html").write_text(await frame.content(), encoding="utf-8")
                print("   saved ->", out_dir / "chapter_frame.html")
            except Exception:
                pass

        print("\n--- 全部链接/按钮 ---")
        for it in await page.evaluate(JS_ALL):
            print("   %-28s %s" % (it["t"], it["h"]))

        print("\n--- body 文本前 1500 字 ---")
        print(await page.evaluate(JS_BODY_SNIP))

        html = await page.content()
        (out_dir / "course_page.html").write_text(html, encoding="utf-8")
        print("\nsaved ->", out_dir / "course_page.html")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
