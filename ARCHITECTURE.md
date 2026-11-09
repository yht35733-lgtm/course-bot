# 架构说明

改功能之前先看这张表，能少翻一半代码。

## 模块职责

| 模块 | 行数 | 管什么 | 改这里会牵动 |
| --- | --- | --- | --- |
| `main.py` | 402 | CLI 参数解析与分发 | 加命令要同时改子命令注册和 `main()` 里的 elif |
| `runner.py` | 746 | 编排：课程 → 小节 → 分组 → 任务点 | 最核心，任务点流程只动这里 |
| `checkup.py` | 225 | 平台状态核对：读目录计数、遍历分组标签收任务点、校准本地进度 | `check` / `audit` 命令，`run` 开跑前的校准 |
| `video.py` | 298 | 播放器驱动：倍速、卡死重载、完成判定、弹题拦截 | 播放不动的 bug 基本在这里 |
| `quiz.py` | 268 | 答题编排：题库 → 模型 → 猜测；章测与弹题两条提交分支 | 答案不对先看这里 |
| `fontdecode.py` | 378 | 字体反爬解码：候选字体择优、字形哈希匹配、康熙部首转换 | 题干乱码才需要动 |
| `bank.py` | 241 | SQLite 题库，hash 精确 + 相似度回捞 | 题库匹配不准调阈值 |
| `llm.py` | 230 | OpenAI 兼容接口，批量答题 + 置信度 | 换模型改 config 即可 |
| `browser.py` | 227 | Playwright 会话、反检测、profile 锁清理、原生弹窗自动确认 | 浏览器起不来 / 被检测先查这里 |
| `store.py` | 45 | `progress.json`，key 支持任务点级 | 进度错乱看这里 |
| `state.py` | 130 | 运行态：暂停闸门、日志缓冲、状态快照 | 控制台的启停逻辑 |
| `webui.py` | 356 | FastAPI 控制台后端 | 前端要新数据就加接口 |
| `static/index.html` | — | 单页控制台，零构建 | 改 UI 只动这一个文件 |
| `scheduler.py` | 33 | 时间窗口计算 | 定时相关 |
| `platforms/base.py` | 236 | 适配器抽象：选择器常量 + 通用动作（清遮罩、关弹题、弹题提交默认实现） | 新平台从这继承 |
| `platforms/chaoxing.py` | 657 | 超星的所有选择器、URL、提交链 | **平台改版只改这一个文件** |
| `platforms/zhihuishu.py` | 149 | 智慧树适配器（骨架） | 用不到可以不管 |

## 一次运行的数据流

```text
main.py run
  └─ cmd_check(deep=False)          按目录计数校准 progress，秒级
  └─ Runner.run_once()
       └─ BrowserSession            一个浏览器，整门课共用
       └─ run_course()
            ├─ sync_catalog()       平台还差几个 → 本地记着的 done 降级成 stale
            ├─ _pending_map()       目录里每节的待完成数
            └─ 逐个 task：do_mixed()
                 ├─ job_tabs()      「1学习目标 / 2视频 / 3章节测验」
                 ├─ 每组 _drain_jobs()
                 │    ├─ work → _do_work_job()
                 │    │    点开 → FontDecoder → quiz.run_page → submitCheckTimes()
                 │    └─ video → _click_job() + play_target()
                 │          └─ VideoEngine.play()
                 │               └─ on_quiz()          弹题
                 │                    ├─ 去重 / 失败上限
                 │                    ├─ FontDecoder.build_from_page()
                 │                    ├─ quiz.run_page(popup=True)
                 │                    └─ submit_popup_quiz() → 断言弹层消失
                 └─ reconcile()    重进小节，遍历所有分组读真实状态
```

## 两套答题链路（最容易踩的坑）

同一个 `modules/work` 页和播放器弹题，**DOM 不同、提交入口不同**，不能混用。

| | 章节测验 | 视频弹题 |
| --- | --- | --- |
| 容器 | `.questionLi / .TiMu` | `.ans-videoquiz` |
| 选项 | `li / .answer_p` | `.ans-videoquiz-opt li` |
| 提交 | `submitCheckTimes()`，需先填 `#answerwqbid` | `.ans-videoquiz` 内的「提交」按钮 |
| 风控延时 | 有（`/work/validate`） | 没有，等 20 秒就是纯浪费 |
| 状态断言 | 跳成绩页 / 按钮消失 | 弹层消失 |

`quiz.run_page(popup=True)` 就是这条分叉点。

## 常见改动的落点

| 想改什么 | 动哪里 |
| --- | --- |
| 平台改版，找不到任务点 | `platforms/chaoxing.py` 的选择器常量 |
| 视频不播 / 播不完 | `video.py` 的 `JS_DRIVE`、`settle_ratio` |
| 两个视频只播一个 | `runner.py` 的 `_click_job`（必须返回各自 iframe） |
| 点击被遮罩挡住 | `platforms/base.py` 的 `JS_CLEAR_MASK` |
| 题干乱码 | `fontdecode.py` 的 `build_from_page`（选错字体是首要原因） |
| 答案不对 | `quiz.py` 的 `_map_answer` 与 `solve_local` 顺序 |
| 弹题反复触发 | `runner.py` 的 `on_quiz` 去重与失败上限 |
| 进度错乱 | `store.py` 的 key 结构 + `runner.py` 的 mark 调用 |
| 控制台界面 | `static/index.html` 一个文件 |

## 关键约束

1. **必须 1.0 倍速**。平台按真实在线时长记账，加速播完任务点不通过。
2. **必须开窗口**。无头模式会被判定 `isPassed:false`。
3. **同一时刻只能一个浏览器用 `.profiles`**，否则 Edge 直接退出码 21。控制台在跑时别再开命令行。
4. **进度分两级**：`platform:cid:tid`（小节）与 `platform:cid:tid:tab:i`（任务点）。只写小节级会让"只差测验"的节被误判已完成。
5. **章节测验提交不走点击**。`btnBlueSubmit()` → `validateTimeNew` → `toadd` → `top.workPop` 确认框，确认框在顶层窗口且抓不到；直接调 `submitCheckTimes()` 又会因缺 `#answerwqbid` 被拒（无效的参数 code-1）。正解是补齐 `answerwqbid` 再调。
6. **清遮罩只能隐藏，不能删元素**。`#workpop` 是 `workPop()` 的容器，删掉后手动点提交毫无反应。
7. **字体不只一套**。一个页面里同时有播放器图标字体（`play/pause/fullscreen`，U+F101 起）和反爬字体，必须按字形哈希命中数择优，不能取第一个或最后一个。

## 运行

```bash
python main.py login    --platform chaoxing     # 扫码，登录态存 .profiles
python main.py audit    --course 课程名         # 只看目录计数，快
python main.py check    --course 课程名 --fix   # 逐节打开读任务点明细
python main.py run      --course 课程名         # 开跑（默认先按目录校准）
python main.py serve    --port 8000             # Web 控制台
```

没有自动化测试，验证靠真实跑一节并看日志：`分组「N」读到 X 个任务点`、`字体哈希精确匹配 x/y`、`已触发提交: direct`、`重进核对: 本节 N 个任务点已全部变绿`。
