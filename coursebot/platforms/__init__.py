"""平台适配器注册表。"""
from __future__ import annotations

from .base import PlatformAdapter
from .chaoxing import ChaoxingAdapter
from .zhihuishu import ZhihuishuAdapter

REGISTRY = {
    "chaoxing": ChaoxingAdapter,
    "xuexitong": ChaoxingAdapter,
    "zhihuishu": ZhihuishuAdapter,
    "zhs": ZhihuishuAdapter,
}


def get_adapter(name: str) -> type[PlatformAdapter]:
    key = (name or "").strip().lower()
    if key not in REGISTRY:
        raise KeyError("未知平台: " + name + "，可选: " + ", ".join(sorted(REGISTRY)))
    return REGISTRY[key]


def list_platforms() -> list[str]:
    return sorted(set(REGISTRY))


__all__ = ["PlatformAdapter", "REGISTRY", "get_adapter", "list_platforms",
           "ChaoxingAdapter", "ZhihuishuAdapter"]
