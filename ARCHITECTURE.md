# 架构说明

改功能之前先看这张表，能少翻一半代码。

## 模块职责

| 模块 | 管什么 | 改这里会牵动 |
| --- | --- | --- |
| `main.py` | 只做 CLI 参数解析和分发 | 加命令要同时改子命令注册和 `main()` 里的 elif |
| `checkup.py` | 平台状态核对：扫目录 → 只对未完成小节读任务点 | `check` / `audit` 两个命令，`run` 开跑前的自动核对 |
| `runner.py` | 编排：课程 → 小节 → 任务点，含点卡片、播视频、做题、重进核对 | 最核心，改任务点流程只动这里 |
| `video.py` | 播放器驱动：倍速、卡死重载、完成判定、弹题拦截 | 播放不动的 bug 基本在这里 |
| `quiz.py` | 答题编排：题库 → 模型 → 猜测，提交与回写 | 答案不对先看这里 |
| `bank.py` | SQLite 题库，hash 精确 + 相似度回捞 | 题库匹配不准调阈值 |
| `llm.py` | OpenAI 兼容接口，批量答题 + 置信度 | 换模型改 config 即可，一般不动代码 |
| `fontdecode.py` | 超星字体反爬解码，字形比对建映射表 | 题干乱码才需要动 |
| `browser.py` | Playwright 会话、反检测、profile 锁清理 | 浏览器起不来 / 被检测先查这里 |
| `store.py` | `progress.json`，key 支持任务点级 | 进度错乱看这里 |
| `state.py` | 运行态：暂停闸门、日志缓冲、状态快照 | 控制台的启停逻辑 |
| `webui.py` | FastAPI 控制台后端 | 前端要新数据就加接口 |
| `static/index.html` | 单页控制台，零构建 | 改 UI 只动这一个文件 |
| `platforms/base.py` | 适配器抽象：选择器常量 + 通用动作（清遮罩、关弹题） | 新平台从这继承 |
| `platforms/chaoxing.py` | 超星的所有选择器和 URL | **平台改版只改这一个文件** |

## 一次运行的数据流

```text
main.py run
  └─ cmd_check()            开跑前核对平台状态，写回 progress
  └─ Runner.run_once()
       └─ BrowserSession    一个浏览器，整门课共用
       └─ adapter.list_courses()
       └─ run_course()
            └─ adapter.open_course()   回到课程页
            └─ adapter.list_tasks()    读目录
            └─ do_mixed()
                 └─ adapter.job_tabs()       「1视频 / 2章节测验」
                 └─ _drain_jobs()            逐个任务点
                      └─ _click_job()             点卡片，返回它自己的 iframe
                      └─ play_target()            VideoEngine 播视频
                      └─ _do_work_job()           测验：点开 → 字体解码 → 答题 → 提交
                 └─ reconcile()              重进小节，读平台真实状态
```

## 常见改动的落点

| 想改什么 | 动哪里 |
| --- | --- |
| 平台改版，找不到任务点 | `platforms/chaoxing.py` 的选择器常量 |
| 视频不播 / 播不完 | `video.py` 的 `JS_DRIVE`、`settle_ratio` |
| 两个视频只播一个 | `runner.py` 的 `_click_job`（必须返回各自 iframe） |
| 点击被遮罩挡住 | `platforms/base.py` 的 `JS_CLEAR_MASK` |
| 答案不对 | `quiz.py` 的 `solve` 顺序、`bank.py` 的阈值 |
| 题干乱码 | `fontdecode.py` |
| 进度错乱 | `store.py` 的 key 结构 + `runner.py` 的 mark 调用 |
| 控制台界面 | `static/index.html` 一个文件 |

## 关键约束

1. **必须 1.0 倍速**。平台按真实在线时长记账，加速播完任务点不通过。
2. **必须开窗口**。无头模式会被判定 `isPassed:false`。
3. **同一时刻只能一个浏览器用 `.profiles`**，否则 Edge 直接退出码 21。
4. **任务点级进度**的 key 是 `platform:cid:tid:jobIndex`，小节级是不带 job 的那条。
5. **提交可能被风控拦**（`/work/validate` 返回 status=1），拦了就退成保存，记 `submit_blocked`。

## 测试

```bash
python -m pytest -q              # 13 个，3 个需要浏览器
python scripts/demo_run.py       # 本地模拟平台完整链路，不需要账号
```
