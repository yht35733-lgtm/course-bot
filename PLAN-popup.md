# 计划书：弹题链路重构

状态：待批准，未动代码。
目标：让「检测弹题 / 识别文字 / 提交」三件事各归其位，消除反复弹题与卡顿。

---

## 一、现状与证据

### 1. 无法自主提交

`JS_CLEAR_MASK` 原本把 `#workpop`、`.popDiv`、`.layui-layer` 全部 `remove()`。

`#workpop` 是 `workPop()` 的容器，用户点「提交」走的是：

```
btnBlueSubmit() → validateTimeNew() → toadd() → top.workPop("确认提交？", …)
```

容器被删，确认框弹不出来，点击毫无反应。机器人走 `submitCheckTimes()` 不经过它，所以只有手动失效。

已改为「只隐藏可见遮罩，不删任何元素」，但**尚未在真实作业页上验证过**。

### 2. 反复弹题

日志实测（4.5 邓小平新时期军队建设思想）：

```
21:44:51 检测到弹题
21:44:53 弹题已提交: text:提交
21:44:54 弹题结果: 通过
21:44:57 检测到弹题          ← 3 秒后又来
21:45:04 检测到弹题
21:45:09 检测到弹题
```

一轮 6–8 秒，而 `video.py` 的 `tick = 3.0`。同一道题被处理 8 次，视频进度 50.0% → 50.8% 花了 24 秒。

### 3. 代码分布

| 模块 | 与弹题相关的行 |
| --- | --- |
| `runner.py` | 56（其中 `on_quiz` 内联 62 行，264–326） |
| `platforms/chaoxing.py` | 22 |
| `platforms/base.py` | 11 |
| `quiz.py` | 10 |
| `video.py` | 7 |
| `fontdecode.py` | 5 |

弹题处理的**状态**（处理过哪题、失败几次、字体映射）散在 `Runner` 的实例属性里，**流程**散在四个模块。

---

## 二、根因

| 问题 | 根因 |
| --- | --- |
| 反复弹题 | 去重靠「上次处理完的时间戳 + 15 秒」，不是「这道题已处理过」。弹层延迟关闭时，时间窗口挡不住 |
| 反复弹题 | 字体解码在 `on_quiz` 里每轮重跑（选字体、解析 TTF、算哈希），一轮多花 300–800ms，进一步拖长处理时间 |
| 卡顿 | 同一份 TTF 在弹题密集时被反复解析；页面主线程被 `evaluate` 占满 |
| 逻辑失衡 | `video.py` 负责检测、`runner.py` 负责处理、`chaoxing.py` 负责提交、`quiz.py` 负责作答，没有任何一层持有「这个弹题已经处理完了」的状态 |

---

## 三、目标结构

新增一个模块 `coursebot/popup.py`（约 150 行），把弹题的**状态和流程**收进一个对象：

```
PopupHandler
  ├─ .prime(frame)          小节开始时调用一次：解码字体，缓存 map
  ├─ .signature(frame)      从 DOM 抽题干 + 选项文本，算 8 位签名（不调模型）
  ├─ .handled(sig)          签名是否已处理过（内存 dict，按小节清理）
  ├─ .handle(frame) -> bool
  │     1. signature()
  │     2. handled → 强制关闭弹层，返回 True
  │     3. quiz.run_page(popup=True)  ← 复用方案里的字体 map
  │     4. 提交 + 断言弹层消失（最多 3 秒）
  │     5. 没关掉 → dismiss_quiz 强关
  │     6. 记签名 / 失败计数
  └─ .fail_count / .skip_remaining
```

调用方变成：

```python
# video.py 不变，只调 on_quiz
# runner.py
on_quiz = lambda: self.popup.handle(frame)
```

`runner.py` 少约 60 行。

---

## 四、实施步骤（每步可独立验证）

### 第 1 步：抽出 PopupHandler，行为不变

- 新建 `coursebot/popup.py`，把 `on_quiz` 的 62 行搬进去
- `runner.play_target` 改为构造 handler 并传 `handler.handle`
- **验收**：跑一节有弹题的视频，日志与改动前一致（能答、能提交、能通过）

### 第 2 步：字体解码上移到小节级

- `do_mixed` 开头调用 `handler.prime(frame)`，整个小节复用同一个 map
- 弹题内不再调 `build_from_page`；解码失败时才回退到现场解码
- **验收**：日志里 `字体解码命中缓存` 或每小节只出现一次解码，弹题阶段无重复解析

### 第 3 步：签名去重 + 强制关闭

- `signature()` 用 `document.querySelector('.ans-videoquiz')?.innerText` 前 120 字 + 选项数算 md5
- 已处理过的签名，直接 `dismiss_quiz` 并返回 True，不再抽题、不再调模型
- 提交断言失败时，立即强关，不等下一轮
- **验收**：日志出现 `弹题 <sig> 已处理过，关闭弹层`，同一题不再重复抽题

### 第 4 步：清理

- 删 `runner.py` 里残留的 `_popup_ok / _popup_at / _popup_fail`
- 删 `quiz.py` 里只为弹题存在的分支（若已被 handler 吸收）
- **验收**：`python -m compileall` 通过，行数下降

---

## 五、验证回路

没有自动化测试，用一条真实视频做回归。

```powershell
python main.py run --course 军事理论 --task 邓小平新时期军队建设思想 --max-tasks 1 --no-check
```

**通过标准**（日志）：

1. 每小节只出现一次 `选取字体「font-cxsecret」哈希命中 N/N`
2. 同一道弹题只出现一次 `第N题 … -> [答案]`，之后是 `已处理过，关闭弹层`
3. `弹题已承认提交: gone` 或 `hidden`
4. 相邻两行 `进度` 的时间差稳定在 3 秒，期间没有重复的 `检测到弹题`
5. 结尾 `重进核对: 本节 N 个任务点已全部变绿`

**手动提交验证**：跑到作业页时，在浏览器里点「提交」，应弹出「确认提交？」对话框。

---

## 六、风险与回滚

| 风险 | 应对 |
| --- | --- |
| 签名在不同皮肤下不稳定 | 签名为空时退回时间窗口去重 |
| 小节级解码拿到章测字体、弹题字体不同 | `prime()` 失败或命中率为 0 时，`handle()` 内回退现场解码 |
| 重构引入回归 | 每步单独提交，出问题 `git revert` 单个 commit |
| 用户正在跑任务 | 改动只在重启控制台后生效，不影响进行中的进程 |

---

## 七、不做的事

- 不改 `video.py` 的播放驱动逻辑（倍速、卡死重载、完成判定已验证可用）
- 不碰章测链路（`_do_work_job`）的提交逻辑
- 不引入测试框架（当前无 `tests/`，加框架属于另一件事）
- 不重写 `chaoxing.py` 的选择器体系
