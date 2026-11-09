"""平台状态核对 + 任务点统计。

两个坑，都是"明明没做完却一路跳过"的来源：

1. 一节的测验藏在「3章节测验」分组标签后面，只看默认标签（视频）会把
   "只差测验"的节误判成已完成 —— 所以读任务点必须遍历所有分组标签。
2. 读不到任务点时不能当成"没有任务点=完成"。目录上的 pending 计数才是
   平台自己的口径，两者冲突时以目录为准。
"""
from __future__ import annotations

import asyncio

from .logger import log

# 目录每节右侧的"待完成任务点"计数。has=False 表示这一节没有计数元素，
# 此时 pending 不能当 0 用，只能算"未知"。
JS_PENDING = """
() => Array.from(document.querySelectorAll('.catalog_title:not(.chapter_Thats_bnt)'))
  .map(function (t, i) {
    const it = t.closest('.chapter_item') || t.parentElement;
    const n = it ? it.querySelector('.catalog_points_yi, .catalog_points_ing') : null;
    const nameEl = it ? it.querySelector('.catalog_name') : null;
    return {
      i: i,
      title: (nameEl ? nameEl.innerText : t.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 40),
      pending: n ? (parseInt((n.innerText || '0').trim(), 10) || 0) : 0,
      has: !!n
    };
  })
"""

# 任务点类型主要看 iframe 模块名：modules/work=测验，modules/video=视频
JS_JOB_STATUS = """
() => Array.from(document.querySelectorAll('.ans-job-icon')).map(function (el, i) {
  const box = el.closest('.ans-attach-ct') || el.parentElement;
  const cls = (el.className || '').toString();
  const ifr = box ? box.querySelector('iframe') : null;
  const src = ifr ? (ifr.getAttribute('src') || '') : '';
  let kind = 'video';
  if (cls.indexOf('ans-job-work') >= 0 || src.indexOf('modules/work') >= 0) kind = 'work';
  else if (cls.indexOf('ans-job-doc') >= 0 || src.indexOf('modules/pdf') >= 0
           || src.indexOf('modules/doc') >= 0) kind = 'doc';
  else if (cls.indexOf('ans-job-video') >= 0 || src.indexOf('modules/video') >= 0) kind = 'video';
  else if (box && box.querySelector('.ans-attach-online, video')) kind = 'video';
  return {
    i: i, kind: kind,
    done: /ans-job-finished/.test(box ? (box.className || '') : '')
  };
})
"""


async def scan_sections(adapter, course) -> list[dict]:
    """读章节列表，返回 [{i, title, pending, has}]。"""
    await adapter.open_course(course)
    frame = await adapter.catalog_frame()
    if frame is None:
        return []
    await frame.wait_for_timeout(1200)
    try:
        return await frame.evaluate(JS_PENDING)
    except Exception as e:
        log.warning("读章节列表失败: " + str(e))
        return []


async def collect_jobs(adapter, page, settle: float = 1.0) -> list[dict]:
    """遍历本节所有分组标签，收集全部任务点。

    返回 [{key, tab, text, i, kind, done, frame}]。
    key 是 progress 的 job 键（"<tab>:<i>"），不同标签的同序号任务点不会串。
    """
    try:
        tabs = await adapter.job_tabs() or []
    except Exception:
        tabs = []
    if not tabs:
        tabs = [{"i": -1, "text": "默认"}]

    out: list[dict] = []
    for t in tabs:
        idx = int(t.get("i", -1))
        stop_after = False
        if len(tabs) > 1 and idx >= 0:
            try:
                if not await adapter.switch_job_tab(idx):
                    log.warning("切不到分组「" + str(t.get("text")) + "」，按当前页面读一次")
                    stop_after = True
            except Exception as e:
                log.warning("切分组异常 " + str(t.get("text")) + ": " + str(e))
                stop_after = True
            await asyncio.sleep(settle)
        frame = await adapter.content_frame(page)
        try:
            jobs = await frame.evaluate(JS_JOB_STATUS)
        except Exception as e:
            log.debug("读任务点失败 " + str(t.get("text")) + ": " + str(e))
            if stop_after:
                break
            continue
        for j in jobs or []:
            j = dict(j)
            j["tab"] = idx
            j["text"] = t.get("text") or "-"
            j["key"] = str(idx) + ":" + str(j["i"])
            j["frame"] = frame
            out.append(j)
        if stop_after:
            break
    return out


async def audit_course(adapter, course) -> tuple[list[dict], list[dict]]:
    """只看目录计数，返回 (全部小节, 还有待完成的)。"""
    rows = await scan_sections(adapter, course)
    return rows, [r for r in rows if r.get("pending", 0) > 0]


async def sync_catalog(adapter, course, prog, platform: str,
                       picks: list[str] | None = None) -> int:
    """用目录口径校准 progress（不打开小节，秒级）。

    返回被改回"待完成"的小节数。旧版本把只差测验的节标成 done，
    这里负责把它们翻回来，否则 run 永远跳过。
    """
    rows = await scan_sections(adapter, course)
    if not rows:
        return 0
    if picks:
        rows = [r for r in rows if any(k in r["title"] for k in picks)]
    fixed = 0
    for r in rows:
        tid = "ch" + str(r["i"])
        local_done = prog.is_done(platform, course.cid, tid)
        if r.get("pending", 0) > 0:
            if local_done:
                prog.mark(platform, course.cid, tid, "stale",
                          "平台还差 " + str(r["pending"]) + " 个任务点")
                fixed += 1
                log.warning("校准 " + r["title"][:24] + "：本地记完成，平台还差 "
                            + str(r["pending"]) + " 个任务点")
            continue
        if not r.get("has"):
            continue          # 没有计数元素 = 未知，别乱动
        if not local_done:
            prog.mark(platform, course.cid, tid, "done", "目录无待完成任务点")
    return fixed


async def check_course(adapter, course, prog, platform: str, fix: bool = True,
                       picks: list[str] | None = None, deep: bool = True) -> list[dict]:
    """核对任务点。deep=False 只对目录，deep=True 每节打开读明细。

    返回 [{title, kind, pending, local, tid}]，只含平台认为还差的小节。
    """
    rows = await scan_sections(adapter, course)
    if not rows:
        return []
    if picks:
        rows = [r for r in rows if any(k in r["title"] for k in picks)]

    if not deep:
        fixed = await sync_catalog(adapter, course, prog, platform, picks)
        if fixed:
            log.info("按目录校准了 " + str(fixed) + " 个小节（本地标记不可信）")
        return [{"title": r["title"], "kind": "-", "pending": r["pending"],
                 "local": prog.status(platform, course.cid, "ch" + str(r["i"])) or "无",
                 "tid": "ch" + str(r["i"])}
                for r in rows if r.get("pending", 0) > 0]

    tasks = await adapter.list_tasks(course)
    by_index = {}
    for t in tasks:
        try:
            idx = int(t.tid.replace("ch", "")) if t.tid.startswith("ch") else -1
        except Exception:
            idx = -1
        if idx >= 0:
            by_index[idx] = t

    out: list[dict] = []
    for r in rows:
        task = by_index.get(int(r["i"]))
        if task is None:
            continue
        pending = int(r.get("pending") or 0)
        local_done = prog.is_done(platform, course.cid, task.tid)

        if pending <= 0:
            if not r.get("has"):
                if local_done:
                    continue
            elif fix and not local_done:
                prog.mark(platform, course.cid, task.tid, "done", "目录无待完成任务点")
            else:
                continue
        elif fix and local_done:
            # 平台说还差、本地说完成 —— 旧版本误标，先翻回来
            prog.mark(platform, course.cid, task.tid, "stale",
                      "平台还差 " + str(pending) + " 个任务点")
            log.warning("校准 " + r["title"][:24] + "：本地记完成，平台还差 "
                        + str(pending) + " 个任务点")

        try:
            await adapter.open_course(course)
            target = await adapter.open_task(task)
            if target is None:
                continue
            page = getattr(target, "page", target)
            await asyncio.sleep(0.6)
            jobs = await collect_jobs(adapter, page)
        except Exception as e:
            log.debug("读任务点失败 " + r["title"][:20] + ": " + str(e))
            continue

        if not jobs:
            # 读不到不等于做完；保留目录计数，让 run 去处理
            out.append({"title": r["title"], "kind": "?", "pending": max(pending, 1),
                        "local": prog.status(platform, course.cid, task.tid) or "无",
                        "tid": task.tid})
            continue

        left = [j for j in jobs if not j["done"]]
        if fix:
            for j in jobs:
                prog.mark(platform, course.cid, task.tid,
                          "done" if j["done"] else "failed", "平台核对", job=j["key"])
            if not left:
                prog.mark(platform, course.cid, task.tid, "done", "平台核对")
        out.append({
            "title": r["title"],
            "kind": "/".join(sorted({j["kind"] for j in left})) or "-",
            "pending": len(left),
            "local": prog.status(platform, course.cid, task.tid) or "无",
            "tid": task.tid,
        })
    return out
