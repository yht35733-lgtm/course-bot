@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ===== course-bot 安装 =====
echo.
where python >nul 2>nul
if errorlevel 1 (
  echo [!] 没找到 python。请先安装 Python 3.10+ 并勾选 Add to PATH
  echo     https://www.python.org/downloads/
  pause
  exit /b 1
)
python --version
echo.
echo [1/3] 安装 Python 依赖...
python -m pip install -r requirements.txt
echo.
echo [2/3] 初始化配置和目录...
python main.py init
echo.
echo [3/3] 安装浏览器内核（可选）。
echo      失败也没关系，程序会自动用你本机的 Chrome / Edge。
python -m playwright install chromium
echo.
echo ===== 完成 =====
echo 下一步: 双击 start.bat 打开控制台，点右上角「登录」扫码
pause