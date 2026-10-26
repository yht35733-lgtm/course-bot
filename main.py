#!/usr/bin/env python
"""course-bot 命令行入口。

常用命令
--------
    python main.py login  --platform chaoxing
    python main.py run    --platform chaoxing --course 高等数学 --kind video
    python main.py run    --platform chaoxing --duration 6h
    python main.py bank import --file data/seed.jsonl
    python main.py bank stats
    python main.py serve  --port 8000
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from coursebot.bank import Bank                       # noqa: E402
from coursebot.config import load_config, save_example  # noqa: E402
from coursebot.logger import log, setup_logger       # noqa: E402
from coursebot.runner import Runner                  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="course-bot", description="课程自动化引擎")
    p.add_argument("-c", "--config", default=str(ROOT / "config.yaml"))
    p.add_argument("--platform", default=None,
                   help="chaoxing | zhihuishu（覆盖配置文件）")
    p.add_argument("--log-level", default=None)

    # 让 --platform / -c 写在子命令后面也能识别（SUPPRESS 保证不覆盖顶层值）
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-c", "--config", default=argparse.SUPPRESS)
    common.add_argument("--platform", default=argparse.SUPPRESS)
    common.add_argument("--log-level", default=argparse.SUPPRESS)

    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", parents=[common], help="生成 config.example.yaml 与目录")
    sub.add_parser("doctor", parents=[common], help="环境自检")

    sp = sub.add_parser("login", parents=[common], help="打开浏览器人工登录并保存登录态")
    sp.add_argument("--wait", type=int, default=180)
    sp.add_argument("--headless", action="store_true")

    sp = sub.add_parser("run", parents=[common], help="开始刷课/答题")
    sp.add_argument("--course", action="append", default=[], help="课程名关键词，可重复")
    sp.add_argument("--kind", choices=["video", "quiz"], default=None)
    sp.add_argument("--loop", type=float, default=None, help="常驻轮询间隔（分钟）")
    sp.add_argument("--duration", default=None, help="运行时长如 30m / 6h，到点自动停")
    sp.add_argument("--window", default=None, help="允许时间段如 00:30-06:30")
    sp.add_argument("--max-courses", type=int, default=None)
    sp.add_argument("--max-tasks", type=int, default=None, help="每门课最多跑几个小节")
    sp.add_argument("--reset", action="store_true", help="清空进度记录")
    sp.add_argument("--dry", action="store_true", help="只列课程与任务")

    sp = sub.add_parser("course", parents=[common], help="列出课程")
    sp.add_argument("--headless", action="store_true")

    sp = sub.add_parser("bank", parents=[common], help="题库管理")
    bsub = sp.add_subparsers(dest="bank_cmd", required=True)
    bi = bsub.add_parser("import")
    bi.add_argument("--file", required=True)
    be = bsub.add_parser("export")
    be.add_argument("--file", default=str(ROOT / "data" / "bank_export.jsonl"))
    be.add_argument("--verified", action="store_true")
    bsub.add_parser("stats")
    bw = bsub.add_parser("weak")
    bw.add_argument("-n", type=int, default=20)

    sp = sub.add_parser("serve", parents=[common], help="Web 控制台")
    sp.add_argument("--port", type=int, default=8000)
    sp.add_argument("--host", default="127.0.0.1")
    return p


def parse_duration(text: str) -> float:
    text = (text or "").strip().lower()
    if not text:
        return 0.0
    unit = text[-1]
    val = float(text[:-1]) if unit.isalpha() else float(text)
    return val * {"s": 1, "m": 60, "h": 3600}.get(unit, 60)


def cmd_init(cfg) -> None:
    save_example(ROOT / "config.example.yaml")
    for d in ("data", "logs", ".profiles"):
        (ROOT / d).mkdir(parents=True, exist_ok=True)
    target = ROOT / "config.yaml"
    if not target.exists():
        target.write_text((ROOT / "config.example.yaml").read_text(encoding="utf-8"),
                          encoding="utf-8")
    print("已生成:", target)
    print("下一步: python main.py login --platform chaoxing")


def cmd_doctor(cfg) -> None:
    print("Python:", sys.version.split()[0])
    checks = []
    try:
        import playwright  # noqa: F401
        checks.append(("playwright", True, ""))
    except Exception as e:
        checks.append(("playwright", False, str(e)))
    try:
        from playwright.async_api import async_playwright  # noqa: F401
        checks.append(("playwright.async_api", True, ""))
    except Exception as e:
        checks.append(("playwright.async_api", False, str(e)))
    bank = Bank(cfg["quiz"]["bank_path"])
    st = bank.stats()
    checks.append(("bank", True, str(st["total"]) + " 道题"))
    for name, ok, info in checks:
        print(("[OK]  " if ok else "[MISS]") + " " + name + ("  " + info if info else ""))
    print("Playwright 安装浏览器: python -m playwright install chromium")


async def cmd_login(cfg, args) -> None:
    from coursebot.browser import BrowserSession
    from coursebot.platforms import get_adapter

    async with BrowserSession(cfg["browser"], headless=args.headless) as sess:
        page = await sess.new_page()
        adapter = get_adapter(cfg["platform"])(page, cfg)
        ok = await adapter.login(wait_seconds=args.wait)
        if ok:
            path = await sess.save_state()
            names = sorted(await adapter.session_cookies()) if hasattr(adapter, "session_cookies") else []
            print("登录态已保存:", path)
            print("已获取票据 cookie:", ", ".join(names) or "(无)")
        else:
            print("登录未完成，请重新运行 login")


async def cmd_course(cfg, args) -> None:
    from coursebot.browser import BrowserSession
    from coursebot.platforms import get_adapter

    async with BrowserSession(cfg["browser"], headless=args.headless) as sess:
        page = await sess.new_page()
        adapter = get_adapter(cfg["platform"])(page, cfg)
        if not await adapter.is_logged_in():
            print("未登录，先运行: python main.py login")
            return
        await adapter.goto_home()
        for c in await adapter.list_courses():
            print(c.cid, "|", c.name, "|", c.url[:80])


async def cmd_run(cfg, args) -> None:
    if args.course:
        cfg["runner"]["courses"] = args.course
    if args.loop is not None:
        cfg["runner"]["loop_interval"] = args.loop
    if args.window:
        cfg["runner"]["daily_window"] = [x.strip() for x in args.window.split(",") if x.strip()]
    if args.max_courses is not None:
        cfg["runner"]["max_courses"] = args.max_courses
    if args.max_tasks is not None:
        cfg["runner"]["max_tasks"] = args.max_tasks

    from coursebot.store import Progress
    if args.reset:
        Progress(ROOT / "data" / "progress.json").reset()
        log.info("进度已清空")

    if args.dry:
        from coursebot.browser import BrowserSession
        from coursebot.platforms import get_adapter
        async with BrowserSession(cfg["browser"], headless=True) as sess:
            page = await sess.new_page()
            adapter = get_adapter(cfg["platform"])(page, cfg)
            await adapter.goto_home()
            courses = await adapter.list_courses()
            wanted = cfg["runner"].get("courses") or []
            if wanted:
                courses = [c for c in courses if any(w in c.name for w in wanted)]
            cap = int(cfg["runner"].get("max_courses") or 0)
            if cap:
                courses = courses[:cap]
            print("待侦察课程数:", len(courses))
            for c in courses:
                print("")
                print("课程:", c)
                await adapter.open_course(c)
                await page.wait_for_timeout(2500)
                print("   当前 URL:", page.url)
                tasks = await adapter.list_tasks(c)
                if not tasks:
                    print("   (!) 没识别到任务，章节选择器需要调整")
                    print("   页面标题:", await page.title())
                for t in tasks:
                    print("   ", t.kind, "|", t.title)
        return

    runner = Runner(cfg, only_kind=args.kind)
    duration = parse_duration(args.duration) if args.duration else 0
    stopper = None
    if duration > 0:
        async def _stopper():
            await asyncio.sleep(duration)
            log.info("到达设定时长，停止")
            runner.stop()
        stopper = asyncio.create_task(_stopper())
    try:
        await runner.loop()
    except KeyboardInterrupt:
        runner.stop()
        log.info("已中断")
    finally:
        if stopper and not stopper.done():
            stopper.cancel()


def cmd_bank(cfg, args) -> None:
    bank = Bank(cfg["quiz"]["bank_path"])
    if args.bank_cmd == "import":
        a, u = bank.import_file(args.file)
        print("新增", a, "更新", u)
    elif args.bank_cmd == "export":
        n = bank.export_file(args.file, only_verified=args.verified)
        print("导出", n, "条 ->", args.file)
    elif args.bank_cmd == "stats":
        print(json.dumps(bank.stats(), ensure_ascii=False, indent=2))
    elif args.bank_cmd == "weak":
        for r in bank.weak(args.n):
            print("[miss", r["misses"], "hit", r["hits"], "]", r["question"][:60], "->", r["answer"])


def main() -> None:
    args = build_parser().parse_args()
    cfg = load_config(args.config)
    platform = getattr(args, "platform", None)
    if platform:
        cfg["platform"] = platform
    if args.log_level:
        cfg["log"]["level"] = args.log_level
    setup_logger(cfg["log"]["level"], cfg["log"].get("file"))

    if args.cmd == "init":
        cmd_init(cfg)
    elif args.cmd == "doctor":
        cmd_doctor(cfg)
    elif args.cmd == "bank":
        cmd_bank(cfg, args)
    elif args.cmd == "login":
        asyncio.run(cmd_login(cfg, args))
    elif args.cmd == "course":
        asyncio.run(cmd_course(cfg, args))
    elif args.cmd == "run":
        asyncio.run(cmd_run(cfg, args))
    elif args.cmd == "serve":
        import uvicorn
        from coursebot.state import LogBuffer
        from coursebot.webui import create_app

        buffer = LogBuffer()
        log.add(buffer.sink, level="DEBUG")
        print("控制台: http://" + args.host + ":" + str(args.port))
        uvicorn.run(create_app(cfg, buffer), host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
