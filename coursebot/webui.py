"""Web 控制台后端：课程选择 / 开始 / 暂停 / 继续 / 停止 / 实时状态。

启动: python main.py serve --port 8000
"""
from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path

import yaml
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

from .bank import Bank
from .browser import BrowserSession
from .config import ROOT
from .logger import log
from .platforms import get_adapter
from .runner import Runner
from .state import LogBuffer, RunState

STATIC = Path(__file__).resolve().parent / "static"


def create_app(cfg: dict, log_sink: LogBuffer | None = None) -> FastAPI:
    app = FastAPI(title="course-bot console")
    buffer = log_sink or LogBuffer()
    run_state = RunState()

    # 有 state.json 说明之前登录过，先按已登录展示，真正失效时运行阶段会报错
    state_file = Path(cfg["browser"].get("profile_dir") or (ROOT / ".profiles")) / "state.json"
    logged_before = state_file.exists()

    session = {
        "runner": None,
        "task": None,
        "courses": [],
        "busy": False,
        "login": ({"running": False, "ok": None, "msg": "登录态待验证"}
                  if logged_before else {"running": False, "ok": False, "msg": "未登录"}),
        "note": "",
    }

    # 同一个 profile 目录同一时刻只能有一个浏览器，否则 Edge/Chrome 直接退出（exitCode 21）
    browser_lock = asyncio.Lock()

    # ---------------- 页面 ----------------

    @app.get("/", response_class=HTMLResponse)
    async def index():
        return (STATIC / "index.html").read_text(encoding="utf-8")

    # ---------------- 登录 ----------------

    @app.post("/api/login")
    async def api_login(request: Request):
        body = await _json(request)
        headless = bool(body.get("headless", False))
        if session["login"]["running"]:
            return JSONResponse({"ok": False, "msg": "登录窗口已经开着"})
        if session["busy"]:
            return JSONResponse({"ok": False, "msg": "正在运行任务，先停止"})

        session["login"] = {"running": True, "ok": False, "msg": "等待扫码…"}

        async def job():
            ok = False
            try:
                async with browser_lock:
                    async with BrowserSession(cfg["browser"], headless=headless) as sess:
                        page = await sess.new_page()
                        adapter = get_adapter(cfg["platform"])(page, cfg)
                        ok = await adapter.login(wait_seconds=int(
                            cfg["browser"].get("login_wait") or 900))
                        if ok:
                            await sess.save_state()
            except Exception as e:
                log.exception("登录失败: " + str(e))
                session["login"] = {"running": False, "ok": False,
                                    "msg": "登录异常: " + str(e)}
                return

            if not ok:
                session["login"] = {"running": False, "ok": False, "msg": "登录超时"}
                return

            session["login"] = {"running": False, "ok": True, "msg": "登录成功"}
            # 浏览器已经关掉了，现在再拉课程才不会撞 profile 锁
            await _fetch_courses()

        asyncio.create_task(job())
        return JSONResponse({"ok": True, "msg": "已打开登录窗口，请扫码"})

    @app.get("/api/login/status")
    async def api_login_status():
        return JSONResponse(session["login"])

    # ---------------- 课程 ----------------

    async def _fetch_courses() -> tuple[list[dict], str]:
        if session["busy"]:
            return session["courses"], "任务运行中"
        session["busy"] = True
        try:
            async with browser_lock:
                async with BrowserSession(cfg["browser"], headless=True) as sess:
                    page = await sess.new_page()
                    adapter = get_adapter(cfg["platform"])(page, cfg)
                    await adapter.goto_home()
                    if not await adapter.is_logged_in():
                        session["login"] = {"running": False, "ok": False,
                                            "msg": "未登录，请点「登录」"}
                        return [], "未登录，请先点「登录」"
                    if not await adapter.verify_login():
                        # 票据还在但服务端不认——上一轮登录没存完整
                        session["login"] = {"running": False, "ok": False,
                                            "msg": "登录态已失效，请重新登录"}
                        return [], "登录态已失效，请重新登录"
                    courses = await adapter.list_courses()
                    session["courses"] = [
                        {"cid": c.cid, "name": c.name, "url": c.url} for c in courses
                    ]
                    session["login"] = {"running": False, "ok": True, "msg": "已登录"}
                    return session["courses"], ""
        except Exception as e:
            log.exception("拉取课程失败: " + str(e))
            return session["courses"], str(e)
        finally:
            session["busy"] = False

    @app.post("/api/courses/refresh")
    async def api_courses_refresh():
        if run_state.running:
            return JSONResponse({"ok": False, "courses": session["courses"],
                                 "msg": "正在运行，先停止再刷新课程"})
        courses, err = await _fetch_courses()
        return JSONResponse({"ok": err == "", "courses": courses, "msg": err})

    @app.get("/api/courses")
    async def api_courses():
        return JSONResponse({"courses": session["courses"]})

    # ---------------- 运行控制 ----------------

    @app.post("/api/run/start")
    async def api_run_start(request: Request):
        body = await _json(request)
        if run_state.running:
            return JSONResponse({"ok": False, "msg": "已经在跑了"})
        if session["busy"]:
            return JSONResponse({"ok": False, "msg": "浏览器被占用，稍后再试"})

        ids = [str(x) for x in (body.get("course_ids") or []) if str(x).strip()]
        if not ids:
            return JSONResponse({"ok": False, "msg": "至少选一门课"})

        run_cfg = copy.deepcopy(cfg)
        run_cfg["runner"]["course_ids"] = ids
        run_cfg["runner"]["max_tasks"] = int(body.get("max_tasks") or 0)
        run_cfg["runner"]["loop_interval"] = 0
        if body.get("rate"):
            run_cfg["video"]["rate"] = float(body["rate"])
        if body.get("submit") is not None:
            run_cfg["quiz"]["submit"] = bool(body["submit"])
        if body.get("headless") is not None:
            run_cfg["browser"]["headless"] = bool(body["headless"])

        mode = body.get("mode") or "all"
        only_kind = None if mode == "all" else mode

        run_state.control.reset()  # 清掉上一轮的停止标志，否则新任务起不来
        runner = Runner(run_cfg, only_kind=only_kind, state=run_state)
        session["runner"] = runner
        log.info("控制台启动: 课程 " + str(len(ids)) + " 门, 模式 " + mode)

        async def job():
            try:
                await runner.loop()
            except Exception as e:
                log.exception("运行异常: " + str(e))
                run_state.finish("error", str(e))
            finally:
                if run_state.phase != "error":
                    run_state.finish("done")

        session["task"] = asyncio.create_task(job())
        return JSONResponse({"ok": True, "msg": "已开始"})

    @app.post("/api/run/pause")
    async def api_run_pause():
        runner = session.get("runner")
        if not runner or not run_state.running:
            return JSONResponse({"ok": False, "msg": "没在运行"})
        runner.pause()
        log.info("已暂停（当前视频会先停下）")
        return JSONResponse({"ok": True})

    @app.post("/api/run/resume")
    async def api_run_resume():
        runner = session.get("runner")
        if not runner or not run_state.running:
            return JSONResponse({"ok": False, "msg": "没在运行"})
        runner.resume()
        log.info("已继续")
        return JSONResponse({"ok": True})

    @app.post("/api/run/stop")
    async def api_run_stop():
        runner = session.get("runner")
        if not runner:
            return JSONResponse({"ok": False, "msg": "没在运行"})
        runner.stop()
        log.info("收到停止指令，正在收尾")
        return JSONResponse({"ok": True})

    # ---------------- 状态 ----------------

    def _pending_count() -> int:
        p = ROOT / "data" / "pending.jsonl"
        if not p.exists():
            return 0
        try:
            with open(p, encoding="utf-8") as f:
                return sum(1 for _ in f)
        except Exception:
            return 0

    @app.get("/api/status")
    async def api_status(since: int = 0):
        return JSONResponse({
            "run": run_state.snapshot(),
            "log": buffer.since(since),
            "seq": buffer.seq,
            "login": session["login"],
            "courses": session["courses"],
            "bank": _bank_stats(),
            "pending": _pending_count(),
        })

    # ---------------- 题库 ----------------

    _bank_cache = {"at": 0.0, "data": {}}

    def _bank_stats() -> dict:
        import time as _t
        if _t.time() - _bank_cache["at"] < 4 and _bank_cache["data"]:
            return _bank_cache["data"]
        b = Bank(cfg["quiz"]["bank_path"])
        try:
            st = b.stats()
            st.pop("file", None)
            st["weak"] = [
                {"question": r["question"][:80], "misses": r["misses"]}
                for r in b.weak(8)
            ]
            _bank_cache["at"] = _t.time()
            _bank_cache["data"] = st
            return st
        finally:
            b.close()

    # ---------------- 模型设置 ----------------

    LLM_KEYS = ("enabled", "base_url", "model", "temperature", "timeout",
                "retries", "max_tokens", "min_confidence")

    def _persist_llm(llm: dict) -> None:
        """只改 config.yaml 里的 llm 段，其它内容原样保留。"""
        path = Path(cfg.get("_source") or (ROOT / "config.yaml"))
        data: dict = {}
        if path.exists():
            try:
                data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            except Exception:
                data = {}
        block = {k: llm.get(k) for k in LLM_KEYS if k in llm}
        # api_key 不在 LLM_KEYS 里，之前漏写了，导致重启后模型失效
        if llm.get("api_key"):
            block["api_key"] = llm["api_key"]
        data["llm"] = block
        path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
                        encoding="utf-8")

    @app.get("/api/llm")
    async def api_llm_get():
        llm = dict(cfg.get("llm") or {})
        key = str(llm.pop("api_key", "") or "")
        llm["has_key"] = bool(key)
        llm["key_hint"] = (key[:4] + "****" + key[-4:]) if len(key) > 10 else ""
        return JSONResponse(llm)

    @app.post("/api/llm")
    async def api_llm_set(request: Request):
        body = await _json(request)
        llm = cfg.setdefault("llm", {})
        for k in LLM_KEYS:
            if k in body and body[k] is not None:
                llm[k] = body[k]
        key = (body.get("api_key") or "").strip()
        if key:
            llm["api_key"] = key
        elif body.get("clear_key"):
            llm["api_key"] = ""
        try:
            _persist_llm(llm)
        except Exception as e:
            return JSONResponse({"ok": False, "msg": "写入 config.yaml 失败: " + str(e)})

        from .llm import LLMQuizzer
        probe = LLMQuizzer(llm)
        msg = "已保存"
        if llm.get("enabled") and not probe.available:
            msg = "已保存，但客户端没起来，检查 api_key 与 openai 包"
        return JSONResponse({"ok": True, "msg": msg, "ready": probe.available})

    @app.post("/api/llm/test")
    async def api_llm_test():
        from .llm import LLMQuizzer
        sample = [{
            "i": 0, "qtype": "single",
            "stem": "TCP 三次握手中第二个报文段的标志位是",
            "options": ["SYN", "SYN+ACK", "ACK", "FIN"],
        }]
        quizzer = LLMQuizzer(dict(cfg.get("llm") or {}))
        if not quizzer.available:
            return JSONResponse({"ok": False, "msg": "模型未启用或缺少 api_key"})
        got = await asyncio.to_thread(quizzer.answer_batch, sample)
        hit = got.get(0)
        if not hit:
            return JSONResponse({"ok": False, "msg": "调用失败，查看日志里的具体报错"})
        ans, conf = hit
        return JSONResponse({"ok": True,
                             "msg": "答题正常：" + str(ans) + " 置信度 " + str(round(conf, 2))})

    @app.get("/api/bank")
    async def api_bank():
        return JSONResponse(_bank_stats())

    @app.post("/api/bank/import")
    async def api_bank_import(request: Request):
        body = await _json(request)
        path = body.get("path") or ""
        if not path or not Path(path).exists():
            return JSONResponse({"ok": False, "msg": "文件不存在"})
        b = Bank(cfg["quiz"]["bank_path"])
        try:
            added, updated = b.import_file(path)
            return JSONResponse({"ok": True, "msg": "新增 " + str(added) + " 更新 " + str(updated)})
        finally:
            b.close()

    return app


async def _json(request: Request) -> dict:
    try:
        data = await request.json()
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}
