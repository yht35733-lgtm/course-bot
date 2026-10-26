"""运行态：暂停闸门、状态快照、日志环形缓冲。

前端只靠一个 /api/run/status 轮询，所有可观测信息都收在这里。
"""
from __future__ import annotations

import asyncio
import threading
from collections import deque
from datetime import datetime
from typing import Any


class Control:
    """开始 / 暂停 / 继续 / 停止 的唯一闸门。"""

    def __init__(self) -> None:
        self._event = asyncio.Event()
        self._event.set()
        self.stopped = False

    def pause(self) -> None:
        self._event.clear()

    def resume(self) -> None:
        self._event.set()

    def stop(self) -> None:
        self.stopped = True
        self._event.set()

    def reset(self) -> None:
        self.stopped = False
        self._event.set()

    def active(self) -> bool:
        return self._event.is_set()

    async def wait(self) -> None:
        """暂停时阻塞在这里；停止时立即放行，由调用方检查 stopped。"""
        await self._event.wait()


class LogBuffer:
    """loguru sink，把日志留在内存里供前端增量拉取。"""

    def __init__(self, maxlen: int = 1000) -> None:
        self.lines: deque[dict[str, Any]] = deque(maxlen=maxlen)
        self._seq = 0
        self._lock = threading.Lock()

    def sink(self, message) -> None:
        rec = message.record
        with self._lock:
            self._seq += 1
            self.lines.append({
                "seq": self._seq,
                "t": rec["time"].strftime("%H:%M:%S"),
                "level": rec["level"].name,
                "msg": rec["message"],
            })

    def since(self, seq: int) -> list[dict[str, Any]]:
        with self._lock:
            return [x for x in self.lines if x["seq"] > seq]

    def tail(self, n: int = 200) -> list[dict[str, Any]]:
        with self._lock:
            return list(self.lines)[-n:]

    @property
    def seq(self) -> int:
        return self._seq


class RunState:
    """Runner 写、前端读。字段全部可 JSON 序列化。"""

    def __init__(self) -> None:
        self.control = Control()
        self.running = False
        self.phase = "idle"          # idle | preparing | course | task | video | quiz | paused | done | error
        self.course = ""
        self.course_index = 0
        self.course_total = 0
        self.task = ""
        self.task_kind = ""
        self.task_index = 0
        self.task_total = 0
        self.progress = 0.0           # 当前视频 0~1
        self.stats: dict[str, int] = {"video_ok": 0, "video_fail": 0,
                                      "quiz_ok": 0, "quiz_fail": 0, "skipped": 0}
        self.last_error = ""
        self.started_at = ""
        self.finished_at = ""

    def begin(self) -> None:
        self.running = True
        self.phase = "preparing"
        self.progress = 0.0
        self.last_error = ""
        self.started_at = datetime.now().strftime("%H:%M:%S")
        self.finished_at = ""
        self.control.reset()
        for k in self.stats:
            self.stats[k] = 0

    def finish(self, phase: str = "done", error: str = "") -> None:
        self.running = False
        self.phase = phase
        self.last_error = error
        self.finished_at = datetime.now().strftime("%H:%M:%S")
        self.progress = 0.0

    def snapshot(self) -> dict[str, Any]:
        return {
            "running": self.running,
            "paused": self.running and not self.control.active(),
            "phase": self.phase,
            "course": self.course,
            "course_index": self.course_index,
            "course_total": self.course_total,
            "task": self.task,
            "task_kind": self.task_kind,
            "task_index": self.task_index,
            "task_total": self.task_total,
            "progress": round(self.progress, 4),
            "stats": dict(self.stats),
            "last_error": self.last_error,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }
