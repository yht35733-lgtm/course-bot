@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo.
echo   正在启动 course-bot 控制台...
echo   浏览器会自动打开 http://127.0.0.1:8000
echo   关掉这个黑窗口 = 停止服务
echo.
start "" powershell -WindowStyle Hidden -Command "Start-Sleep -Seconds 4; Start-Process 'http://127.0.0.1:8000'"
python main.py serve --port 8000
pause