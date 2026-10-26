"""SQLite 题库：精确命中 + 相似度回捞 + 命中率统计。

设计要点
--------
* 归一化后再算 hash：忽略大小写、全半角、标点、空白，避免同一题因排版不同而漏命中。
* 两级检索：先 qhash 精确命中（O(1)），再 LIKE 粗筛 + SequenceMatcher 精排。
* 命中/失配计数用于统计题库质量，方便你只补低分题。
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import unicodedata
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Iterable

BACKTICK = chr(96)  # 反引号不在原始字符串里写，避免转义麻烦

_PUNCT = re.compile(
    r"[\s\u3000,，。.!！?？;；:：\"'“”‘’()（）\[\]【】<>《》\-—_/\\|*#~" + BACKTICK + r"]+"
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS questions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    qhash      TEXT NOT NULL UNIQUE,
    question   TEXT NOT NULL,
    qtype      TEXT NOT NULL DEFAULT 'single',
    options    TEXT NOT NULL DEFAULT '[]',
    answer     TEXT NOT NULL DEFAULT '[]',
    explain    TEXT DEFAULT '',
    platform   TEXT DEFAULT '',
    course     TEXT DEFAULT '',
    source     TEXT DEFAULT 'manual',
    hits       INTEGER NOT NULL DEFAULT 0,
    misses     INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_qtype  ON questions(qtype);
CREATE INDEX IF NOT EXISTS idx_source ON questions(source);
"""


def normalize(text: str | None) -> str:
    if not text:
        return ""
    t = unicodedata.normalize("NFKC", text).lower()
    t = _PUNCT.sub("", t)
    return t.strip()


def make_hash(question: str, qtype: str = "") -> str:
    return hashlib.md5((qtype + "|" + normalize(question)).encode("utf-8")).hexdigest()


def similarity(a: str, b: str) -> float:
    na, nb = normalize(a), normalize(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    if na in nb or nb in na:
        ratio = min(len(na), len(nb)) / max(len(na), len(nb))
        return 0.95 * (0.5 + 0.5 * ratio)
    return SequenceMatcher(None, na, nb).ratio()


class Bank:
    def __init__(self, db_path: str | Path):
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    # ---------------- 写入 ----------------

    def add(self, question: str, answer: Iterable[str], qtype: str = "single",
            options: Iterable[dict | str] | None = None, explain: str = "",
            platform: str = "", course: str = "", source: str = "manual") -> bool:
        """upsert。返回 True 表示新增，False 表示更新已有。"""
        now = datetime.now().isoformat(timespec="seconds")
        h = make_hash(question, qtype)
        opts = json.dumps(list(options or []), ensure_ascii=False)
        ans = json.dumps(sorted(str(a) for a in answer), ensure_ascii=False)
        row = self.conn.execute("SELECT id FROM questions WHERE qhash=?", (h,)).fetchone()
        if row:
            self.conn.execute(
                "UPDATE questions SET answer=?, options=?, explain=?, platform=?, "
                "course=?, updated_at=? WHERE id=?",
                (ans, opts, explain, platform, course, now, row["id"]),
            )
            self.conn.commit()
            return False
        self.conn.execute(
            "INSERT INTO questions(qhash,question,qtype,options,answer,explain,platform,"
            "course,source,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (h, question.strip(), qtype, opts, ans, explain, platform, course, source, now, now),
        )
        self.conn.commit()
        return True

    def bump(self, question: str, qtype: str, hit: bool) -> None:
        col = "hits" if hit else "misses"
        self.conn.execute(
            "UPDATE questions SET " + col + "=" + col + "+1, updated_at=? WHERE qhash=?",
            (datetime.now().isoformat(timespec="seconds"), make_hash(question, qtype)),
        )
        self.conn.commit()

    # ---------------- 查询 ----------------

    def exact(self, question: str, qtype: str = "single") -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM questions WHERE qhash=?", (make_hash(question, qtype),)
        ).fetchone()
        return self._row(row)

    def search(self, question: str, qtype: str = "single", threshold: float = 0.82,
               limit: int = 300) -> tuple[dict | None, float]:
        """返回 (最佳匹配, 相似度)。"""
        hit = self.exact(question, qtype)
        if hit:
            return hit, 1.0

        norm = normalize(question)
        if not norm:
            return None, 0.0

        head, tail = norm[:10], norm[-10:]
        rows = self.conn.execute(
            "SELECT * FROM questions WHERE question LIKE ? OR question LIKE ? LIMIT ?",
            ("%" + head + "%", "%" + tail + "%", limit),
        ).fetchall()
        if not rows:
            rows = self.conn.execute(
                "SELECT * FROM questions WHERE qtype=? LIMIT ?", (qtype, limit)
            ).fetchall()

        best, best_s = None, 0.0
        for r in rows:
            s = similarity(question, r["question"])
            if r["qtype"] == qtype:
                s += 0.02
            if s > best_s:
                best, best_s = self._row(r), s
        if best_s < threshold:
            return None, best_s
        return best, best_s

    def stats(self) -> dict[str, Any]:
        total = self.conn.execute("SELECT COUNT(*) c FROM questions").fetchone()["c"]
        by_type = {
            r["qtype"]: r["c"]
            for r in self.conn.execute(
                "SELECT qtype, COUNT(*) c FROM questions GROUP BY qtype"
            ).fetchall()
        }
        hits = self.conn.execute("SELECT COALESCE(SUM(hits),0) h FROM questions").fetchone()["h"]
        misses = self.conn.execute(
            "SELECT COALESCE(SUM(misses),0) m FROM questions"
        ).fetchone()["m"]
        return {"total": total, "by_type": by_type, "hits": hits,
                "misses": misses, "file": str(self.path)}

    def weak(self, n: int = 20) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM questions WHERE misses > 0 ORDER BY misses DESC, hits ASC LIMIT ?",
            (n,),
        ).fetchall()
        return [self._row(r) for r in rows]

    # ---------------- 导入导出 ----------------

    def import_file(self, path: str | Path) -> tuple[int, int]:
        """支持 .jsonl / .json（数组）/ .csv(question,answer,qtype)。"""
        path = Path(path)
        added = updated = 0
        if path.suffix.lower() == ".csv":
            import csv
            with open(path, encoding="utf-8-sig", newline="") as f:
                for rec in csv.DictReader(f):
                    q = (rec.get("question") or "").strip()
                    if not q:
                        continue
                    ans = [x.strip() for x in (rec.get("answer") or "").split("|") if x.strip()]
                    if self.add(q, ans, rec.get("qtype") or "single", source="import"):
                        added += 1
                    else:
                        updated += 1
            return added, updated

        text = path.read_text(encoding="utf-8")
        records: list[dict]
        if path.suffix.lower() == ".jsonl":
            records = [json.loads(ln) for ln in text.splitlines() if ln.strip()]
        else:
            data = json.loads(text)
            records = data if isinstance(data, list) else data.get("questions", [])
        for rec in records:
            q = (rec.get("question") or "").strip()
            if not q:
                continue
            ans = rec.get("answer") or rec.get("answers") or []
            if isinstance(ans, str):
                ans = [ans]
            ok = self.add(q, ans, rec.get("qtype") or "single",
                          rec.get("options"), rec.get("explain") or "",
                          rec.get("platform") or "", rec.get("course") or "",
                          rec.get("source") or "import")
            added += 1 if ok else 0
            updated += 0 if ok else 1
        return added, updated

    def export_file(self, path: str | Path, only_verified: bool = False) -> int:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        sql = "SELECT * FROM questions"
        if only_verified:
            sql += " WHERE hits > 0"
        rows = self.conn.execute(sql).fetchall()
        with open(path, "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(self._row(r), ensure_ascii=False) + "\n")
        return len(rows)

    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:
            pass

    @staticmethod
    def _row(r) -> dict | None:
        if r is None:
            return None
        d = dict(r)
        d["options"] = json.loads(d.get("options") or "[]")
        d["answer"] = json.loads(d.get("answer") or "[]")
        return d
