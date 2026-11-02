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
import os
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
    sp.add_argument("--task", action="append", default=None,
                    help="只跑标题里含这些关键词的小节，可重复，例 --task 3.3.1.2")
    sp.add_argument("--force", action="store_true",
                    help="忽略本地「已完成」标记，强制重跑（漏做/做失败时用）")
    sp.add_argument("--dry", action="store_true", help="只列课程与任务")
    sp.add_argument("--no-check", action="store_true",
                    help="跳过开跑前的平台状态核对")

    sp = sub.add_parser("check", parents=[common],
                        help="逐节核对平台任务点状态，可写回进度")
    sp.add_argument("--course", action="append", default=[], help="只看这些课程")
    sp.add_argument("--fix", action="store_true", help="把核对结果写回 progress.json")
    sp.add_argument("--task", action="append", default=None,
                    help="只核对标题含这些关键词的小节")

    sp = sub.add_parser("audit", parents=[common],
                        help="对账：列出平台上还没做完的小节")
    sp.add_argument("--course", action="append", default=[], help="只看这些课程")

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


JS_PENDING = """
() => Array.from(document.querySelectorAll('.chapter_item')).map(function (it) {
  const t = it.querySelector('.catalog_title');
  if (!t) return null;
  const n = it.querySelector('.catalog_points_yi');
  const nameEl = it.querySelector('.catalog_name');
  return {
    title: (t.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 34),
    pending: n ? parseInt((n.innerText || '0').trim(), 10) : 0,
    done: /ans-job-finished/.test(it.className || '')
  };
}).filter(Boolean)
"""


JS_JOB_STATUS = """
() => Array.from(document.querySelectorAll('.ans-job-icon')).map(function (el, i) {
  const box = el.closest('.ans-attach-ct') || el.parentElement;
  const cls = (el.className || '').toString();
  const ifr = box ? box.querySelector('iframe') : null;
  const src = ifr ? (ifr.getAttribute('src') || '') : '';
  let kind = 'video';
  if (cls.indexOf('ans-job-work') >= 0 || src.indexOf('modules/work') >= 0) kind = 'work';
  else if (cls.indexOf('ans-job-doc') >= 0) kind = 'doc';
  return { i: i, kind: kind,
           done: /ans-job-finished/.test(box ? (box.className || '') : '') };
})
"""


async def cmd_check(cfg, args) -> int:
    """逐节进页面读任务点级状态，和本地 progress 对比。返回未完成小节数。"""
    from coursebot.browser import BrowserSession
    from coursebot.platforms import get_adapter
    from coursebot.store import Progress

    prog = Progress(ROOT / "data" / "progress.json")
    bad_total = 0
    async with BrowserSession(cfg["browser"], headless=False) as sess:
        page = await sess.new_page()
        adapter = get_adapter(cfg["platform"])(page, cfg)
        await adapter.goto_home()
        if not await adapter.is_logged_in():
            print("未登录，先跑 python main.py login")
            return 0
        courses = await adapter.list_courses()
        if args.course:
            courses = [c for c in courses if any(k in c.name for k in args.course)]

        for c in courses:
            await adapter.open_course(c)
            tasks = await adapter.list_tasks(c)
            picks = getattr(args, "task", None) or []
            if picks:
                tasks = [t for t in tasks if any(k in t.title for k in picks)]
            print("")
            print("课程: " + c.name + " (" + str(len(tasks)) + " 节)")
            bad = 0
            for t in tasks:
                try:
                    # open_task 依赖章节列表 iframe，每节前要回到课程页重新取
                    await adapter.open_course(c)
                    target = await adapter.open_task(t)
                    if target is None:
                        print("   [打不开] " + t.title[:26])
                        continue
                    await asyncio.sleep(1.0)
                    jobs = await target.evaluate(JS_JOB_STATUS)
                except Exception as e:
                    print("   [读取失败] " + t.title[:26] + " :: " + str(e)[:40])
                    continue
                if not jobs:
                    print("   [无任务点] " + t.title[:26])
                    continue
                pending = [j for j in jobs if not j["done"]]
                if args.fix:
                    for j in jobs:
                        prog.mark(cfg["platform"], c.cid, t.tid,
                                  "done" if j["done"] else "failed",
                                  "平台核对", job=j["i"])
                    if not pending:
                        prog.mark(cfg["platform"], c.cid, t.tid, "done", "平台核对")
                if pending:
                    bad += 1
                    kinds = "/".join(j["kind"] for j in pending)
                    local = prog.status(cfg["platform"], c.cid, t.tid) or "无"
                    print("   [还差 %d] %-26s %-10s 本地=%s" %
                          (len(pending), t.title[:24], kinds, local))
            if not bad:
                print("   全部完成")
            bad_total += bad
    print("")
    print("合计还有 " + str(bad_total) + " 个小节没做完")
    return bad_total


async def cmd_audit(cfg, args) -> None:
    """读章节列表上的「待完成任务点」计数，列出还没做完的小节。"""
    from coursebot.browser import BrowserSession
    from coursebot.platforms import get_adapter

    async with BrowserSession(cfg["browser"], headless=False) as sess:
        page = await sess.new_page()
        adapter = get_adapter(cfg["platform"])(page, cfg)
        await adapter.goto_home()
        if not await adapter.is_logged_in():
            print("未登录，先跑 python main.py login")
            return
        courses = await adapter.list_courses()
        if args.course:
            courses = [c for c in courses if any(k in c.name for k in args.course)]

        total_pending = 0
        for c in courses:
            await adapter.open_course(c)
            frame = await adapter.catalog_frame()
            if frame is None:
                print("[跳过]", c.name, "- 拿不到章节列表")
                continue
            await frame.wait_for_timeout(1500)
            rows = await frame.evaluate(JS_PENDING)
            left = [r for r in rows if r["pending"] > 0]
            finished = len(rows) - len(left)
            print("")
            print("课程: " + c.name + "  (" + str(finished) + "/" + str(len(rows)) + " 已完成)")
            for r in left:
                total_pending += r["pending"]
                print("   [还差 %d] %s" % (r["pending"], r["title"]))
            if not left:
                print("   全部完成")
        print("")
        print("合计还差 " + str(total_pending) + " 个任务点")


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
    if args.task:
        cfg["runner"]["only_task"] = list(args.task)
    if args.force:
        cfg["runner"]["ignore_done"] = True

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

    # 默认先对账一次：本地记录被改过很多轮，跑之前用平台真实状态校准
    if not getattr(args, "no_check", False):
        log.info("先核对平台任务点状态（--no-check 可跳过）")
        try:
            args.fix = True          # 核对结果直接写回进度
            await cmd_check(cfg, args)
        except Exception as e:
            log.warning("核对失败，按本地进度继续: " + str(e))

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
    elif args.cmd == "check":
        asyncio.run(cmd_check(cfg, args))
    elif args.cmd == "audit":
        asyncio.run(cmd_audit(cfg, args))
    elif args.cmd == "course":
        asyncio.run(cmd_course(cfg, args))
    elif args.cmd == "run":
        asyncio.run(cmd_run(cfg, args))
    elif args.cmd == "serve":
        import socket
        import uvicorn
        from coursebot.state import LogBuffer
        from coursebot.webui import create_app

        # 单实例保护：端口被占用说明已经有一个在跑，别再起第二个把机器拖死
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.settimeout(1)
        in_use = probe.connect_ex((args.host, args.port)) == 0
        probe.close()
        if in_use:
            print("端口 " + str(args.port) + " 已被占用，控制台可能已经在跑了：")
            print("  http://" + args.host + ":" + str(args.port))
            print("要重启用 stop.bat，或换个端口 --port 8001")
            return

        pid_file = ROOT / "data" / "serve.pid"
        pid_file.parent.mkdir(parents=True, exist_ok=True)
        pid_file.write_text(str(os.getpid()), encoding="utf-8")

        buffer = LogBuffer()
        log.add(buffer.sink, level="DEBUG")
        print("控制台: http://" + args.host + ":" + str(args.port))
        print("PID " + str(os.getpid()) + "，停止请用 stop.bat 或 Ctrl+C")
        try:
            uvicorn.run(create_app(cfg, buffer), host=args.host, port=args.port,
                        log_level="warning")
        finally:
            try:
                pid_file.unlink(missing_ok=True)
            except Exception:
                pass


if __name__ == "__main__":
    main()
