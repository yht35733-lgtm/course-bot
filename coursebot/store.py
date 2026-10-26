"""进度持久化：支持断点续跑。"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class Progress:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.data: dict[str, Any] = {}
        if self.path.exists():
            try:
                self.data = json.loads(self.path.read_text(encoding="utf-8"))
            except Exception:
                self.data = {}

    @staticmethod
    def key(platform: str, cid: str, tid: str) -> str:
        return platform + ":" + str(cid) + ":" + str(tid)

    def is_done(self, platform: str, cid: str, tid: str) -> bool:
        rec = self.data.get(self.key(platform, cid, tid))
        return bool(rec and rec.get("status") == "done")

    def mark(self, platform: str, cid: str, tid: str, status: str, detail: str = "") -> None:
        self.data[self.key(platform, cid, tid)] = {"status": status, "detail": detail}
        self.save()

    def save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def reset(self) -> None:
        self.data = {}
        self.save()
