"""答题引擎：题库匹配 -> LLM 兜底 -> 回写题库。

平台适配器只负责"抽取"和"点选/提交"，判定与记忆都在这里。
"""
from __future__ import annotations

import asyncio
import json
import random
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from .bank import Bank
from .llm import LLMQuizzer
from .logger import log


@dataclass
class Question:
    index: int
    stem: str
    qtype: str = "single"          # single | multiple | judge | fill | short
    options: list[str] = field(default_factory=list)
    answer: list[Any] = field(default_factory=list)
    source: str = ""               # bank | llm | guess | manual
    similarity: float = 0.0
    root: Any = None               # 平台定位器（由适配器填充）
    option_nodes: list[Any] = field(default_factory=list)


@dataclass
class QuizOutcome:
    total: int = 0
    answered: int = 0
    from_bank: int = 0
    from_llm: int = 0
    guessed: int = 0
    submitted: bool = False
    detail: str = ""
    pending: list[dict] = field(default_factory=list)


class QuizEngine:
    def __init__(self, bank: Bank, cfg: dict, llm_cfg: Optional[dict] = None,
                 pending_path: Optional[Path] = None):
        self.bank = bank
        self.cfg = cfg or {}
        self.llm = LLMQuizzer(llm_cfg or {})
        self.threshold = float(self.cfg.get("min_similarity", 0.82))
        self.pending_path = Path(pending_path) if pending_path else None

    # ---------- 单题求解 ----------

    def solve(self, q: Question) -> Question:
        hit, score = self.bank.search(q.stem, q.qtype, self.threshold)
        if hit:
            q.answer = self._map_answer(hit["answer"], hit.get("options") or [], q)
            q.source = "bank"
            q.similarity = score
            self.bank.bump(q.stem, q.qtype, True)
            return q

        if self.llm.available:
            ans = self.llm.answer(q.stem, q.qtype, q.options)
            if ans:
                q.answer = self._map_answer(ans, [], q)
                q.source = "llm"
                q.similarity = 0.0
                return q

        q.answer = self._guess(q)
        q.source = "guess"
        self.bank.bump(q.stem, q.qtype, False)
        return q

    @staticmethod
    def _norm_opt(text: str) -> str:
        from .bank import normalize
        return normalize(text)

    def _map_answer(self, answer: list, stored_options: list, q: Question) -> list[Any]:
        """题库里的答案可能是下标、字母或选项原文，统一映射到当前题的下标。"""
        if q.qtype == "fill" or q.qtype == "short":
            return [str(a) for a in answer]

        # 判断题题库里常见 true/false/对/错/0/1，直接按语义映射。
        # 不能要求 len(q.options) >= 2 —— 弹题经常一个选项都抽不到，
        # 那样这里会被跳过，最后返回空答案，题目永远答不出来。
        if q.qtype == "judge" and answer:
            v = str(answer[0]).strip().lower()
            if v in ("true", "t", "对", "正确", "是", "√", "a", "0"):
                return [0]
            if v in ("false", "f", "错", "错误", "否", "×", "b", "1"):
                return [1]

        current = [self._norm_opt(o) for o in q.options]
        out: list[Any] = []
        for a in answer:
            if isinstance(a, int):
                if 0 <= a < len(q.options):
                    out.append(a)
                continue
            s = str(a).strip()
            if s.isdigit():
                i = int(s)
                if 0 <= i < len(q.options):
                    out.append(i)
                continue
            if len(s) == 1 and s.upper() in "ABCDEFGH" and len(q.options) <= 8:
                i = "ABCDEFGH".index(s.upper())
                if 0 <= i < len(q.options):
                    out.append(i)
                continue
            # 选项原文匹配（优先当前题，其次题库快照）
            na = self._norm_opt(s)
            for i, o in enumerate(current):
                if o == na or (o and na and (o in na or na in o)):
                    out.append(i)
                    break
            else:
                for i, o in enumerate(stored_options):
                    o = o if isinstance(o, str) else (o.get("text") or o.get("label") or "")
                    if self._norm_opt(o) == na:
                        out.append(i)
                        break
        seen: list[Any] = []
        for x in out:
            if x not in seen:
                seen.append(x)
        return seen

    def _guess(self, q: Question) -> list[Any]:
        """无题库无模型时的保守猜测：判断题押"正确"，单选押 A。"""
        if q.qtype == "judge":
            return [0]
        if q.qtype in ("single", "multiple") and q.options:
            return [0]
        return []

    def solve_local(self, q: Question) -> Question:
        """只查本地题库；没命中就标记 none，留给批量模型。"""
        hit, score = self.bank.search(q.stem, q.qtype, self.threshold)
        q.similarity = score
        if hit:
            mapped = self._map_answer(hit["answer"], hit.get("options") or [], q)
            if mapped:
                q.answer = mapped
                q.source = "bank"
                self.bank.bump(q.stem, q.qtype, True)
                return q
            # 命中了但映射不出下标（脏数据），当作没命中，交给模型
            log.debug("题库答案映射失败，转模型: " + q.stem[:30])
        q.answer = []
        q.source = "none"
        return q

    # ---------- 整页流程 ----------

    async def run_page(self, adapter, page, frames=None, popup: bool = False) -> QuizOutcome:
        """popup=True 表示视频弹题：走弹题自己的提交按钮，且不等待风控延时。"""
        out = QuizOutcome()
        questions: list[Question] = []
        target = page
        # 弹题可能不在播放器所在的 frame 里，逐个 frame 试一遍
        candidates = [page]
        for f in (frames or []):
            if f is not page:
                candidates.append(f)
        for cand in candidates:
            try:
                found = await adapter.extract_questions(cand)
            except Exception:
                found = []
            if found:
                questions = found
                target = cand
                break
        if not questions:
            out.detail = "未识别到题目"
            return out

        out.total = len(questions)
        log.info("本页识别到 " + str(len(questions)) + " 道题")

        # 第一轮：本地题库（快、确定性高）
        need_llm: list[Question] = []
        for q in questions:
            self.solve_local(q)
            if q.source == "none":
                need_llm.append(q)

        # 第二轮：把这一页没命中的题一次性丢给模型，避免一题一次往返
        if need_llm:
            got: dict[int, tuple] = {}
            if self.llm.available:
                items = [{"i": i, "stem": q.stem, "qtype": q.qtype, "options": q.options}
                         for i, q in enumerate(need_llm)]
                log.info("交给模型作答 " + str(len(items)) + " 题")
                try:
                    got = await asyncio.to_thread(self.llm.answer_batch, items)
                except Exception as e:
                    log.warning("模型批量作答异常: " + str(e))
                    got = {}

            for i, q in enumerate(need_llm):
                hit = got.get(i)
                if hit:
                    ans, conf = hit
                    q.answer = self._map_answer(ans, [], q)
                    q.source = "llm"
                    q.similarity = conf
                else:
                    q.answer = self._guess(q)
                    q.source = "guess"

        for q in questions:
            tag = q.source
            log.info(
                "第" + str(q.index + 1) + "题 [" + q.qtype + "/" + tag +
                " opt=" + str(len(q.options)) + "] " +
                q.stem[:40].replace("\n", " ") + " -> " + str(q.answer)
            )
            if q.answer:
                out.answered += 1
                if q.source == "bank":
                    out.from_bank += 1
                elif q.source == "llm":
                    out.from_llm += 1
                else:
                    out.guessed += 1
                    out.pending.append({
                        "stem": q.stem, "qtype": q.qtype, "options": q.options,
                        "guess": q.answer, "at": datetime.now().isoformat(timespec="seconds"),
                    })
            await adapter.answer_question(target, q)
            await asyncio.sleep(float(self.cfg.get("delay_between", 1.2)))

        if out.pending and self.pending_path:
            self._dump_pending(out.pending)

        if self.cfg.get("submit", True):
            if popup:
                # 弹题是播放器自己弹的，没有 /work/validate 那套风控，
                # 也不能等几十秒——视频会被一路暂停，整节卡死。
                ok = await adapter.submit_popup_quiz(target)
                out.submitted = ok
                out.detail = "弹题已提交" if ok else "弹题提交入口没找到"
            else:
                # 提交前会先打 /work/validate，返回 status=1 就判「账号异常」直接拦下。
                # 答题太快最容易触发，等久一点并带随机抖动。
                wait = float(self.cfg.get("submit_delay", 20))
                if wait > 0:
                    wait += random.uniform(0, wait * 0.3)
                    log.info("提交前等待 " + str(int(wait)) + " 秒，避开风控校验")
                    await asyncio.sleep(wait)
                ok = await adapter.submit_quiz(target)
                out.submitted = ok
                if ok:
                    await adapter.read_quiz_result(target, questions)
                    for q in questions:
                        if q.source == "llm" and q.answer:
                            self.bank.add(q.stem, [str(x) for x in q.answer], q.qtype,
                                          q.options, platform=adapter.name, source="llm")
                out.detail = "已提交" if ok else "提交没成功（按钮/风控）"
        else:
            out.detail = "仅计算，未提交"

        return out

    def _dump_pending(self, items: list[dict]) -> None:
        """无把握的题落盘，前端能看到，补一道少一道。"""
        try:
            self.pending_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.pending_path, "a", encoding="utf-8") as f:
                for it in items:
                    f.write(json.dumps(it, ensure_ascii=False) + chr(10))
        except Exception:
            pass
