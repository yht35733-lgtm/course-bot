"""视频弹题：一次完整处理，带身份与失败上限。

检测在 video.py（每 tick 看一次有没有弹层），作答在 quiz.py，
提交在 adapter。这一层只管两件事：

1. 这道题处理过没有 —— 用题干签名认，不用时间窗口
2. 处理失败了几次 —— 到上限就关掉所有后续弹题，别让视频卡死
"""
from __future__ import annotations

import hashlib

from .logger import log

POPUP_TEXT = """
() => {
  const box = document.querySelector('.ans-videoquiz, .videoquiz, #videoquiz');
  const scope = box || document.body;
  return (scope.innerText || '').replace(/\\s+/g, ' ').trim().slice(0, 200);
}
"""


class PopupHandler:
    """一个视频内的弹题处理者。"""

    MAX_FAIL = 3

    def __init__(self, adapter, quiz):
        self.adapter = adapter
        self.quiz = quiz
        self._decoded = False        # 字体只解一次，整个视频复用
        self._seen: set[str] = set() # 已处理过的题干签名
        self.fails = 0
        self.give_up = False

    async def signature(self, frame) -> str:
        """题干 + 选项文本的短 hash，用来认同一道弹题。"""
        try:
            text = await frame.evaluate(POPUP_TEXT) or ""
        except Exception:
            return ""
        if not text:
            return ""
        return hashlib.md5(text.encode()).hexdigest()[:10]

    async def _ensure_font(self, frame) -> None:
        if self._decoded:
            return
        self._decoded = True
        try:
            from .fontdecode import FontDecoder
            dec = FontDecoder()
            n = await dec.build_from_page(frame)
            self.adapter.font_map = dec.map if n > 0 else {}
        except Exception as e:
            log.warning("弹题字体解码异常: " + str(e))

    async def _dismiss(self, frame) -> None:
        try:
            await self.adapter.dismiss_quiz(frame)
        except Exception as e:
            log.debug("关闭弹题异常: " + str(e))

    async def handle(self, frame) -> bool:
        """处理当前弹题。返回 True 表示这一轮可以放行视频。"""
        if self.give_up:
            await self._dismiss(frame)
            return True

        sig = await self.signature(frame)
        if sig and sig in self._seen:
            log.info("弹题 " + sig + " 已处理过，直接关掉")
            await self._dismiss(frame)
            return True

        await self._ensure_font(frame)

        try:
            owner = getattr(frame, "page", None) or frame
            frames = list(owner.frames)
        except Exception:
            frames = []

        outcome = await self.quiz.run_page(self.adapter, frame, frames=frames, popup=True)
        if outcome.submitted or (outcome.total and outcome.answered >= outcome.total):
            if sig:
                self._seen.add(sig)
            self.fails = 0
            return True

        self.fails += 1
        log.warning("弹题未通过（" + outcome.detail + "，第 " + str(self.fails) + " 次）")
        await self._dismiss(frame)
        if self.fails >= self.MAX_FAIL:
            log.error("弹题连续 " + str(self.fails) + " 次未通过，本节后续弹题一律关闭")
            self.give_up = True
        return False
