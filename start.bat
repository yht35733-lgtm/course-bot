@echo off
chcp 65001 >nul
cd /d "%~dp0"

rem 先清掉上一次可能残留的服务和浏览器，避免越开越多把内存吃满
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\stop.ps1" >nul 2>nul

echo.
echo   正在启动 course-bot 控制台...
echo   浏览器会自动打开 http://127.0.0.1:8000
echo   关掉这个黑窗口 = 停止服务
echo   如果窗口关了但还有残留，双击 stop.bat
echo.
start "" powershell -WindowStyle Hidden -Command "Start-Sleep -Seconds 4; Start-Process 'http://127.0.0.1:8000'"
python main.py serve --port 8000
pause