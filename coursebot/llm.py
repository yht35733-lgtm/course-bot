"""联网模型答题器（OpenAI 兼容协议）。

支持两类后端，配置里只改 base_url 和 model：
* 通用大模型（DeepSeek / 通义 / 智谱 / Kimi / OpenAI ...）—— 靠模型自带知识作答
* 原生联网模型（Perplexity sonar 等）—— 会先检索再作答，冷门课更稳

一次请求答一整页题，比一题一次快得多，也更省 token。
"""
from __future__ import annotations

import json
import re
import time
from typing import Any

from .logger import log

SYSTEM_PROMPT = """你是一个严谨的答题引擎，正在作答中国高校课程的客观题。

规则：
1. 只输出 JSON，不要解释过程，不要 markdown 代码块。
2. 输出格式固定为：{"answers":[{"i":0,"answer":[1],"confidence":0.9}]}
3. 字段含义：
   - i：题号，必须和输入里的题号一致
   - answer：正确选项的 0-based 下标数组
     单选题 / 判断题 -> 恰好 1 个下标
     多选题 -> 所有正确项的下标
     填空题 -> 按空格顺序的字符串数组
   - confidence：0 到 1 的把握程度，拿不准就如实给低分
4. 判断题的选项通常是「正确/错误」或「对/错」，对应下标 0 和 1。
5. 必须为每一道题都给出答案，不允许漏题；实在不确定也要给最可能的选项，但把 confidence 压低。
6. 题干是程序从页面解码来的，可能有个别形近错字（例如「斋官」实为「斋宫」、「顸」实为「顶」），
   请按上下文语义理解，不要因为个别错字就放弃作答。
"""


def _fmt_type(qtype: str) -> str:
    return {
        "single": "单选题（选 1 项）",
        "multiple": "多选题（可多选）",
        "judge": "判断题（选 1 项）",
        "fill": "填空题（给文字答案）",
    }.get(qtype, qtype)


class LLMQuizzer:
    def __init__(self, cfg: dict[str, Any]):
        self.cfg = cfg or {}
        self.client = None
        self.model = self.cfg.get("model") or "gpt-4o-mini"
        self.min_conf = float(self.cfg.get("min_confidence", 0.0))
        if not self.cfg.get("enabled"):
            return
        key = (self.cfg.get("api_key") or "").strip()
        if not key:
            log.warning("llm.enabled 为真但没填 api_key，模型答题不可用")
            return
        try:
            from openai import OpenAI
            self.client = OpenAI(
                api_key=key,
                base_url=self.cfg.get("base_url") or None,
                timeout=float(self.cfg.get("timeout", 90)),
                max_retries=0,  # 重试自己控制，避免双重退避
            )
            log.info("模型答题已启用: " + self.model + " @ " +
                     str(self.cfg.get("base_url") or "openai-default"))
        except Exception as e:
            log.warning("模型客户端初始化失败: " + str(e))
            self.client = None

    @property
    def available(self) -> bool:
        return self.client is not None

    # ---------------- 单题（兼容旧接口） ----------------

    def answer(self, question: str, qtype: str, options: list[str]) -> list[Any]:
        got = self.answer_batch([{"i": 0, "stem": question, "qtype": qtype,
                                  "options": list(options or [])}])
        item = got.get(0)
        return list(item[0]) if item else []

    # ---------------- 批量 ----------------

    def answer_batch(self, items: list[dict]) -> dict[int, tuple[list[Any], float]]:
        """items: [{i, stem, qtype, options}] -> {i: (answer, confidence)}"""
        if not self.client or not items:
            return {}

        user = self._build_user(items)
        retries = int(self.cfg.get("retries", 2))
        for attempt in range(retries + 1):
            try:
                resp = self.client.chat.completions.create(
                    model=self.model,
                    temperature=float(self.cfg.get("temperature", 0)),
                    max_tokens=int(self.cfg.get("max_tokens", 2000)),
                    messages=[
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": user},
                    ],
                )
                raw = (resp.choices[0].message.content or "").strip()
            except Exception as e:
                log.warning("模型调用失败（第 " + str(attempt + 1) + " 次）: " + str(e))
                if attempt < retries:
                    time.sleep(min(2 ** attempt, 6))
                continue

            parsed = self._parse_batch(raw, items)
            if parsed:
                log.info("模型返回 " + str(len(parsed)) + "/" + str(len(items)) + " 题")
                return parsed
            log.warning("模型输出无法解析，重试中")
        return {}

    def _build_user(self, items: list[dict]) -> str:
        lines: list[str] = []
        for it in items:
            lines.append("【第 " + str(it["i"]) + " 题】" + _fmt_type(it.get("qtype", "single")))
            lines.append("题干：" + str(it.get("stem", "")).strip())
            opts = it.get("options") or []
            if opts:
                for j, o in enumerate(opts):
                    lines.append("  " + str(j) + ". " + str(o).strip())
            else:
                lines.append("  （填空题，直接给答案文本）")
            lines.append("")
        lines.append("以上共 " + str(len(items)) + " 题，请一次给出全部答案。")
        return chr(10).join(lines)

    def _parse_batch(self, raw: str, items: list[dict]) -> dict[int, tuple[list[Any], float]]:
        data = self._loads(raw)
        if data is None:
            return {}

        raw_list: list[dict] = []
        if isinstance(data, dict) and isinstance(data.get("answers"), list):
            raw_list = [x for x in data["answers"] if isinstance(x, dict)]
        elif isinstance(data, dict) and "answer" in data:
            raw_list = [data]
        elif isinstance(data, list):
            raw_list = [x for x in data if isinstance(x, dict)]
        if not raw_list:
            return {}

        by_index = {int(it["i"]): it for it in items if "i" in it}
        out: dict[int, tuple[list[Any], float]] = {}

        for pos, rec in enumerate(raw_list):
            idx = rec.get("i", rec.get("index", pos))
            try:
                idx = int(idx)
            except Exception:
                idx = pos
            if idx not in by_index and len(raw_list) == 1 and len(items) == 1:
                idx = items[0]["i"]
            if idx not in by_index:
                continue

            q = by_index[idx]
            n_opt = len(q.get("options") or [])
            ans = self._normalize(rec.get("answer", rec.get("answers")), n_opt)
            if not ans:
                continue
            try:
                conf = float(rec.get("confidence", 0.6))
            except Exception:
                conf = 0.6
            conf = max(0.0, min(1.0, conf))
            if conf < self.min_conf:
                log.info("第 " + str(idx) + " 题模型置信度 " + str(round(conf, 2)) + " 低于阈值，弃用")
                continue
            out[idx] = (ans, conf)

        return out

    @staticmethod
    def _loads(raw: str) -> Any:
        text = raw.strip()
        if text.startswith(chr(96) * 3):
            text = re.sub("^" + chr(96) * 3 + "(?:json)?|" + chr(96) * 3 + "$", "", text,
                          flags=re.M).strip()
        try:
            return json.loads(text)
        except Exception:
            pass
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            return None
        try:
            return json.loads(m.group(0))
        except Exception:
            return None

    @staticmethod
    def _normalize(ans: Any, n_options: int) -> list[Any]:
        if ans is None:
            return []
        if isinstance(ans, (str, int)):
            ans = [ans]
        if not isinstance(ans, list):
            return []

        letters = "ABCDEFGH"
        out: list[Any] = []
        for a in ans:
            if isinstance(a, bool):
                continue
            if isinstance(a, int):
                if 0 <= a < max(n_options, 1):
                    out.append(a)
                continue
            s = str(a).strip()
            if not s:
                continue
            if s.isdigit():
                i = int(s)
                if 0 <= i < max(n_options, 1):
                    out.append(i)
                continue
            if n_options and len(s) == 1 and s.upper() in letters[:n_options]:
                out.append(letters.index(s.upper()))
                continue
            out.append(s)  # 填空题：原样保留文字

        if n_options and out and all(isinstance(x, int) for x in out):
            out = sorted(set(out))
        return out
