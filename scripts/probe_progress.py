"""读平台侧的任务点状态，核对本地进度有没有真的同步过去。

    python scripts/probe_progress.py 中国古建筑欣赏与设计 3.3
"""
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from coursebot.browser import BrowserSession  # noqa: E402
from coursebot.config import load_config  # noqa: E402
from coursebot.platforms.chaoxing import ChaoxingAdapter  # noqa: E402

JS_STATE = """
() => {
  const out = { summary: '', total: 0, items: [] };
  const jd = document.querySelector('.catalog_jindu');
  if (jd) out.summary = (jd.innerText || '').replace(/\\s+/g, ' ').trim();
  const names = document.querySelectorAll('.catalog_name');
  out.total = names.length;
  names.forEach(function (n) {
    const t = n.querySelector('.catalog_title');
    if (!t) return;
    const tasks = Array.from(n.querySelectorAll('.catalog_task'));
    out.items.push({
      title: (t.innerText || '').replace(/\\s+/g, ' ').trim().slice(0, 26),
      n: tasks.length,
      done: tasks.filter(function (x) {
        const c = (x.className || '').toString();
        return /finish|done|complete/i.test(c);
      }).length
    });
  });
  return out;
}
"""


async def main() -> int:
    key = sys.argv[1] if len(sys.argv) > 1 else "中国古建筑欣赏与设计"
    needle = sys.argv[2] if len(sys.argv) > 2 else ""
    cfg = load_config()
    async with BrowserSession(cfg["browser"], headless=True) as sess:
        page = await sess.new_page()
        adapter = ChaoxingAdapter(page, cfg)
        await adapter.goto_home()
        courses = await adapter.list_courses()
        target = next((c for c in courses if key in c.name), None)
        if not target:
            print("没找到课程", key)
            return 1
        await adapter.open_course(target)
        await page.wait_for_timeout(2500)
        frame = await adapter.catalog_frame()
        if frame is None:
            print("拿不到章节 iframe")
            return 1
        await frame.wait_for_timeout(2500)
        print("frame:", (frame.url or "")[:100])
        data = await frame.evaluate(JS_STATE)
        print("总进度:", data["summary"] or "(空)")
        print("章节条目:", data["total"])
        shown = 0
        for it in data["items"]:
            if needle and needle not in it["title"]:
                continue
            print("  %-28s %d/%d 完成" % (it["title"], it["done"], it["n"]))
            shown += 1
            if shown >= 12:
                break
        if shown == 0:
            print("  (没有匹配条目，打印前 8 条)")
            for it in data["items"][:8]:
                print("  %-28s %d/%d 完成" % (it["title"], it["done"], it["n"]))
        try:
            await page.screenshot(path=str(ROOT / "data" / "platform-progress.png"), full_page=False)
            print("截图: data/platform-progress.png")
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
