"""播放一小段，看播放器到底有没有向超星上报学习时长。

    python scripts/probe_report.py 中国古建筑欣赏与设计 3.3.1

有上报请求（multimedia/log、fystat 之类）且返回成功，说明记账链路通；
一条都没有，就是播放器没把这次播放当回事，任务点自然不会变。
"""
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from coursebot.browser import BrowserSession  # noqa: E402
from coursebot.config import load_config  # noqa: E402
from coursebot.platforms.chaoxing import ChaoxingAdapter  # noqa: E402
from coursebot.video import VideoEngine  # noqa: E402
from coursebot.models import Task  # noqa: E402

KEYS = ("multimedia/log", "fystat", "stat-ans", "/log?", "/log/a/", "insertlog", "progress")


async def main() -> int:
    key = sys.argv[1] if len(sys.argv) > 1 else "中国古建筑欣赏与设计"
    needle = sys.argv[2] if len(sys.argv) > 2 else "3.1"
    cfg = load_config()
    hits: list[str] = []
    headless = "--headful" not in sys.argv
    print("headless =", headless)

    async with BrowserSession(cfg["browser"], headless=headless) as sess:
        page = await sess.new_page()

        def on_req(req):
            u = req.url
            if any(k in u for k in KEYS):
                hits.append("REQ  " + u[:180])
            elif req.method == "POST":
                hits.append("POST " + u[:170] + "  body=" + (req.post_data or "")[:120])

        def on_resp(resp):
            u = resp.url
            if any(k in u for k in KEYS):
                hits.append("RESP " + str(resp.status) + " " + u[:160])
            if "multimedia/log" in u:
                async def grab():
                    try:
                        body = await resp.text()
                        hits.append("BODY " + body[:260])
                    except Exception as e:
                        hits.append("BODY-ERR " + str(e)[:80])
                asyncio.create_task(grab())

        page.on("request", on_req)
        page.on("response", on_resp)

        adapter = ChaoxingAdapter(page, cfg)
        await adapter.goto_home()
        courses = await adapter.list_courses()
        target = next((c for c in courses if key in c.name), None)
        if not target:
            print("没找到课程", key)
            return 1
        await adapter.open_course(target)
        tasks = await adapter.list_tasks(target)
        pick = next((t for t in tasks if needle in t.title), None)
        if not pick:
            print("没找到小节", needle, "；前几个是:", [t.title[:12] for t in tasks[:6]])
            return 1
        print("试播:", pick.title, "index", pick.meta.get("index"))

        await adapter.open_course(target)
        frame = await adapter.open_task(pick)
        if frame is None:
            print("打不开任务")
            return 1
        await asyncio.sleep(2)

        # 在 cards 那一层点视频卡片，让播放器初始化（不然不会上报学习时长）
        for sel in (".ans-attach-online", ".ans-attach-ct", ".videoCard"):
            try:
                card = frame.locator(sel).first
                if await card.count():
                    await card.click(timeout=4000)
                    print("点了视频卡片:", sel)
                    await asyncio.sleep(3)
                    break
            except Exception as e:
                print("点卡片失败:", sel, str(e)[:60])

        # 真正带 <video> 的是 ananas 的 iframe，逐层找
        video_frame = None
        for f in page.frames:
            try:
                n = await f.evaluate("() => document.querySelectorAll('video').length")
            except Exception:
                n = 0
            if n:
                video_frame = f
                print("播放 frame:", (f.url or "")[:80], "videos=", n)
                break
        if video_frame is None:
            print("没找到 video 元素")
            return 1
        frame = video_frame

        rate = float(sys.argv[3]) if len(sys.argv) > 3 else 1.0
        mute = (sys.argv[4] != "loud") if len(sys.argv) > 4 else True
        engine = VideoEngine(frame, {"rate": rate, "mute": mute, "tick": 1.0,
                                     "settle_ratio": 0.99, "stall_limit": 30,
                                     "max_reload": 0,
                                     "force_rate": rate != 1.0,
                                     "click_play": True})
        print("rate =", rate, "mute =", mute)
        print("播放 45 秒观察上报…")
        try:
            await asyncio.wait_for(engine.play(Task(tid="probe", kind="mixed", title="probe")), 45)
        except asyncio.TimeoutError:
            pass
        except Exception as e:
            print("播放异常:", e)

        print("\n=== 上报相关请求", len(hits), "条 ===")
        for h in hits[:25]:
            print(" ", h)
        if not hits:
            print("  （一条都没有 —— 播放器没有向后端报学习时长）")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
