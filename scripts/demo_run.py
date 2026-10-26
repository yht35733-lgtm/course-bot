"""端到端演练：对本地模拟平台跑完整的 Runner 流程。

    python scripts/demo_run.py

覆盖链路：
    登录态检测 -> 课程列表 -> 章节 -> 视频(iframe 内) -> 弹题拦截 -> 题库命中
    -> 继续播放 -> 进度达标 -> 测验页 -> 三题作答 -> 提交 -> 判分 -> 进度落盘

不联网、不需要真实账号，跑完打印平台侧收到的事件流。
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from mock_platform import EVENTS, serve  # noqa: E402

from coursebot.bank import Bank  # noqa: E402
from coursebot.config import load_config  # noqa: E402
from coursebot.logger import log, setup_logger  # noqa: E402
from coursebot.platforms import REGISTRY  # noqa: E402
from coursebot.platforms.chaoxing import ChaoxingAdapter  # noqa: E402
from coursebot.runner import Runner  # noqa: E402
from coursebot.store import Progress  # noqa: E402

PORT = 8799
BASE = "http://127.0.0.1:" + str(PORT) + "/"


class DemoAdapter(ChaoxingAdapter):
    """只改地址和少量选择器，其余逻辑完全复用真实超星适配器。"""

    name = "demo"
    home_url = BASE
    login_url = BASE
    COURSE_URL = BASE + "visit/interaction"
    IFRAME_TIMEOUT = 2500

    COURSE_CARD = ".course"
    CHAPTER_ITEM = ".posCatalog_select"
    CONTENT_IFRAME = "iframe#iframe"
    VIDEO_QUESTION_POPUP = ".ans-videoquiz-opt"
    QUESTION_ITEM = ".questionLi"
    QUESTION_STEM = ".Zy_TItle"
    QUESTION_OPTION = "li"
    SUBMIT_BTN = ".completeBtn"

    # 模拟站点没有「章节」tab，也没有 frame_content-zj，列表就在主页面
    TASK_ITEM = ".posCatalog_select"

    async def open_chapter_tab(self) -> None:
        return None

    async def catalog_frame(self):
        return self.page

    async def is_logged_in(self) -> bool:
        # 本地模拟站点没有超星票据 cookie，直接放行
        return True


def build_config() -> dict:
    cfg = load_config()
    cfg["platform"] = "demo"
    cfg["browser"]["headless"] = True
    cfg["browser"]["profile_dir"] = str(ROOT / ".profiles-demo")
    cfg["video"].update({
        "rate": 2.0,
        "force_rate": True,
        "mute": True,
        "settle_ratio": 0.9,
        "tick": 0.2,
        "stall_limit": 40,
        "max_reload": 1,
    })
    cfg["quiz"].update({"submit": True, "delay_between": 0.2, "min_similarity": 0.82})
    cfg["runner"].update({
        "courses": [],
        "order": "video_then_quiz",
        "cooldown": 0.2,
        "course_interval": 0.2,
        "loop_interval": 0,
        "stop_on_error": False,
    })
    return cfg


async def main() -> int:
    setup_logger("INFO", None)
    srv = serve(PORT)
    log.info("模拟平台已启动: " + BASE)
    REGISTRY["demo"] = DemoAdapter

    import os
    if not os.environ.get("DEMO_KEEP_PROGRESS"):
        Progress(ROOT / "data" / "progress.json").reset()
        log.info("已清空进度记录")
    else:
        log.info("保留已有进度，用于验证断点续跑")

    cfg = build_config()
    runner = Runner(cfg)
    log.info("开始执行完整流程")
    await runner.loop()

    bank = Bank(cfg["quiz"]["bank_path"])
    stats = bank.stats()
    await asyncio.sleep(0.5)
    srv.shutdown()

    print("")
    print("=" * 62)
    print("平台侧收到的事件流")
    print("=" * 62)
    for e in EVENTS:
        print("  " + e)

    print("")
    print("=" * 62)
    print("引擎侧结果")
    print("=" * 62)
    print("任务统计:", json.dumps(runner.stats, ensure_ascii=False))
    print("题库统计:", json.dumps({k: v for k, v in stats.items() if k != "file"}, ensure_ascii=False))
    prog = json.loads((ROOT / "data" / "progress.json").read_text(encoding="utf-8"))
    for k, v in prog.items():
        print("进度:", k, "->", v["status"], v["detail"])
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
