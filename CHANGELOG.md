# Changelog

## 0.4.0

Web 控制台与答题可靠性。

- 新增 FastAPI 控制台：课程勾选、开始 / 暂停 / 继续 / 停止、实时日志与进度
- 新增 `RunState` / `Control`：真正的暂停（播放器 pause + 闸门阻塞），不是跳过任务
- 新增 `data/pending.jsonl`：无把握的题落盘，前端「待核对」计数
- 适配 2025-2026 版超星：课程门户「章节」tab + `iframe#frame_content-zj`
- 登录判定改为认 `_uid` 票据 cookie，修掉扫码后提前收工的问题
- 新增 `storage_state` 导出，Chrome 丢弃 session cookie 后仍可恢复
- 配置里的路径项留空即解析到项目目录，整个文件夹可整体迁移
- `--platform` 等全局参数现在写在子命令前后都生效
- 新增 `install.bat` / `install.sh` / `start-cli.bat`

## 0.3.0

- 视频引擎：倍速守护、卡死自愈重载、iframe 失效后重新取
- SQLite 题库：归一化 hash 精确命中 + 相似度回捞
- OpenAI 兼容接口兜底答题
- 断点续跑 `data/progress.json`
- 本地模拟平台 `scripts/mock_platform.py` + 端到端测试
