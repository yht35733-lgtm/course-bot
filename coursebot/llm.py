"""OpenAI 兼容接口的兜底答题器。

只在题库未命中时调用；返回选项下标（0-based），失败返回空列表。
"""
from __future__ import annotations

import json
import re
from typing import Any

SYSTEM_PROMPT = (
    "你是一个严谨的答题引擎。只输出 JSON，不要任何解释、不要 markdown 代码块。"
    '格式：{"answer": [0], "confidence": 0.0}。'
    "answer 是正确选项的 0-based 下标数组；判断题 0=正确 1=错误。"
    "填空题 answer 为字符串数组，元素顺序与空格一致。"
)


class LLMQuizzer:
    def __init__(self, cfg: dict[str, Any]):
        self.cfg = cfg or {}
        self.client = None
        if not self.cfg.get("enabled"):
            return
        key = (self.cfg.get("api_key") or "").strip()
        if not key:
            return
        try:
            from openai import OpenAI
            self.client = OpenAI(
                api_key=key,
                base_url=self.cfg.get("base_url") or None,
                timeout=self.cfg.get("timeout", 60),
            )
        except Exception:
            self.client = None

    @property
    def available(self) -> bool:
        return self.client is not None

    def answer(self, question: str, qtype: str, options: list[str]) -> list[Any]:
        if not self.client:
            return []
        opts = "\n".join(str(i) + ". " + o for i, o in enumerate(options))
        user = (
            "题型：" + qtype + "\n"
            "题干：" + question + "\n"
            + ("选项：\n" + opts + "\n" if options else "")
            + "请给出答案。"
        )
        try:
            resp = self.client.chat.completions.create(
                model=self.cfg.get("model", "gpt-4o-mini"),
                temperature=self.cfg.get("temperature", 0),
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user},
                ],
            )
            raw = resp.choices[0].message.content or ""
        except Exception:
            return []
        return self._parse(raw, len(options))

    @staticmethod
    def _parse(raw: str, n_options: int) -> list[Any]:
        raw = raw.strip()
        try:
            data = json.loads(raw)
        except Exception:
            m = re.search(r"\{.*\}", raw, re.S)
            if not m:
                return []
            try:
                data = json.loads(m.group(0))
            except Exception:
                return []
        ans = data.get("answer") if isinstance(data, dict) else data
        if isinstance(ans, (str, int)):
            ans = [ans]
        if not isinstance(ans, list):
            return []
        letters = "ABCDEFGH"
        out: list[Any] = []
        for a in ans:
            if isinstance(a, int):
                if 0 <= a < max(n_options, 1):
                    out.append(a)
            elif isinstance(a, str):
                s = a.strip()
                if s.isdigit():
                    i = int(s)
                elif len(s) == 1 and s.upper() in letters[: max(n_options, 1)]:
                    i = letters.index(s.upper())
                else:
                    out.append(s)
                    continue
                if 0 <= i < max(n_options, 1):
                    out.append(i)
        return out
