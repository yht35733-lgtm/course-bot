"""验证模型答题链路：连通性 + 一次批量作答。

    python scripts/test_llm.py

读 config.yaml 的 llm 段；不给参数就跑内置的三道样题。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from coursebot.config import load_config  # noqa: E402
from coursebot.llm import LLMQuizzer  # noqa: E402

SAMPLES = [
    {"i": 0, "qtype": "single", "stem": "TCP 三次握手中第二个报文段的标志位是",
     "options": ["SYN", "SYN+ACK", "ACK", "FIN"]},
    {"i": 1, "qtype": "judge", "stem": "HTTP 协议是无状态协议",
     "options": ["正确", "错误"]},
    {"i": 2, "qtype": "multiple", "stem": "下列哪些属于面向对象的三大特性",
     "options": ["封装", "继承", "多态", "编译"]},
]


def main() -> int:
    cfg = load_config().get("llm") or {}
    print("enabled   :", cfg.get("enabled"))
    print("base_url  :", cfg.get("base_url"))
    print("model     :", cfg.get("model"))
    key = (cfg.get("api_key") or "").strip()
    print("api_key   :", (key[:6] + "..." + key[-4:]) if len(key) > 12 else "(未填)")

    if not cfg.get("enabled"):
        print("\nllm.enabled = false，先在 config.yaml 里打开")
        return 1
    if not key:
        print("\n没填 api_key（或环境变量 COURSEBOT_LLM_KEY 为空）")
        return 1

    quizzer = LLMQuizzer(cfg)
    if not quizzer.available:
        print("\n客户端初始化失败，检查 openai 包是否安装: pip install openai")
        return 1

    print("\n--- 发一次批量请求，3 道题 ---")
    got = quizzer.answer_batch(SAMPLES)
    if not got:
        print("没有拿到任何答案，检查网络 / base_url / model 名称")
        return 1

    for i, item in enumerate(SAMPLES):
        hit = got.get(i)
        if not hit:
            print("  第" + str(i + 1) + "题: 无答案")
            continue
        ans, conf = hit
        picked = []
        for a in ans:
            if isinstance(a, int) and a < len(item["options"]):
                picked.append(item["options"][a])
            else:
                picked.append(str(a))
        print("  第" + str(i + 1) + "题: " + str(ans) + " -> " + " / ".join(picked) +
              "   置信度 " + str(round(conf, 2)))

    print("\n链路正常。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
