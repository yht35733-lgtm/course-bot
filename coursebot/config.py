"""配置加载与深合并。"""
from __future__ import annotations

import copy
import os
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent

DEFAULTS: dict[str, Any] = {
    "platform": "chaoxing",
    "browser": {
        "headless": False,
        "channel": "",          # "chrome" / "msedge" 可选，留空用内置 chromium
        "slow_mo": 0,
        "profile_dir": "",       # 留空 = 项目下 .profiles（便于整体拷贝）
        "user_agent": "",
        "locale": "zh-CN",
        "timezone": "Asia/Shanghai",
        "viewport": {"width": 1440, "height": 900},
        "navigation_timeout": 45000,
    },
    "video": {
        "rate": 1.5,              # 目标倍速
        "force_rate": True,       # 注入 playbackRate 守护，对抗平台重置
        "mute": True,
        "settle_ratio": 0.98,     # 单集进度达到该比例即视为完成
        "tick": 3.0,              # 轮询间隔（秒）
        "stall_limit": 40,        # 连续多少 tick 无进度则重载
        "max_reload": 3,
        "skip_quiz": False,       # True = 弹题直接关掉（有风险，部分平台会倒扣）
        "question_poll": 2.0,
    },
    "quiz": {
        "bank_path": "",         # 留空 = 项目下 data/bank.db
        "min_similarity": 0.82,   # 题库模糊匹配阈值
        "submit": True,           # False = 只打印答案不提交，用于人工核对
        "retry_wrong": 1,
        "delay_between": 1.2,
    },
    "llm": {
        "enabled": False,
        "base_url": "https://api.openai.com/v1",
        "api_key": "",
        "model": "gpt-4o-mini",
        "temperature": 0,
        "timeout": 60,
    },
    "runner": {
        "courses": [],            # 留空 = 全部课程
        "max_courses": 0,         # 0 = 不限
        "max_tasks": 0,           # 每门课最多跑几个小节，0 = 不限
        "order": "video_then_quiz",
        "cooldown": 0.6,          # 任务间冷却（秒）
        "course_interval": 3.0,
        "loop_interval": 0,       # >0 表示常驻轮询，单位分钟
        "daily_window": [],       # 例 ["00:30-06:30"]
        "reopen_course": True,    # 每个任务前重开课程页（播放完会离开，章节索引会失效）
        "stop_on_error": False,
    },
    "log": {"level": "INFO", "file": ""},   # 留空 = 项目下 logs/coursebot.log
}


def _deep_merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path: str | os.PathLike | None = None) -> dict[str, Any]:
    """读取 yaml 并覆盖默认值；不存在则返回默认值。"""
    path = Path(path) if path else ROOT / "config.yaml"
    cfg = copy.deepcopy(DEFAULTS)
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            cfg = _deep_merge(cfg, yaml.safe_load(f) or {})
    # 环境变量兜底（避免明文写 key）
    env_key = os.environ.get("COURSEBOT_LLM_KEY")
    if env_key:
        cfg["llm"]["api_key"] = env_key
    _apply_defaults(cfg)
    cfg["_source"] = str(path)
    return cfg


def _apply_defaults(cfg: dict) -> None:
    """空路径统一解析到项目目录，整个文件夹拷到别的机器也能直接用。"""
    if not cfg["browser"].get("profile_dir"):
        cfg["browser"]["profile_dir"] = str(ROOT / ".profiles")
    if not cfg["quiz"].get("bank_path"):
        cfg["quiz"]["bank_path"] = str(ROOT / "data" / "bank.db")
    if not cfg["log"].get("file"):
        cfg["log"]["file"] = str(ROOT / "logs" / "coursebot.log")


def save_example(path: str | os.PathLike | None = None) -> Path:
    path = Path(path) if path else ROOT / "config.example.yaml"
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(DEFAULTS, f, allow_unicode=True, sort_keys=False)
    return path
