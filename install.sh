#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"
echo "===== course-bot 安装 ====="

if ! command -v python3 >/dev/null 2>&1; then
  echo "[!] 没找到 python3，请先安装 Python 3.10+"
  exit 1
fi
python3 --version

echo "[1/3] 安装依赖"
python3 -m pip install -r requirements.txt

echo "[2/3] 初始化"
python3 main.py init

echo "[3/3] 浏览器内核（可选，失败会自动用本机 Chrome / Chromium）"
python3 -m playwright install chromium || true

echo "===== 完成 ====="
echo "下一步: python3 main.py serve --port 8000"
