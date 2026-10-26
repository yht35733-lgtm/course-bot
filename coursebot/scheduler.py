"""极简定时器：每天固定时间跑一轮。"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Callable

from .logger import log


def next_run(at: str) -> datetime:
    hh, mm = [int(x) for x in at.split(":")]
    now = datetime.now()
    target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return target


async def run_daily(make_runner: Callable, at: str = "01:00") -> None:
    """每天 at 时刻跑一轮；异常不退出，等明天。"""
    while True:
        nxt = next_run(at)
        wait = (nxt - datetime.now()).total_seconds()
        log.info("下次执行: " + nxt.strftime("%Y-%m-%d %H:%M") +
                 "（" + str(round(wait / 60, 1)) + " 分钟后）")
        await asyncio.sleep(max(wait, 5))
        runner = make_runner()
        try:
            await runner.run_once()
        except Exception as e:
            log.exception("定时任务异常: " + str(e))
        await asyncio.sleep(65)
