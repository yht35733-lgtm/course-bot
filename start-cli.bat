@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PLATFORM=chaoxing
set FILTER=

:menu
echo.
echo ================ course-bot (CLI) ================
echo   1. 登录（浏览器里扫码，只需一次）
echo   2. 看看我有哪些课
echo   3. 刷视频
echo   4. 做测验
echo   5. 视频 + 测验 全流程
echo   6. 题库统计
echo   0. 退出
echo ===================================================
echo.
set CH=
set /p CH="输入数字后回车: "
echo.
if "%CH%"=="0" exit /b 0
if "%CH%"=="1" goto do_login
if "%CH%"=="2" goto do_list
if "%CH%"=="6" goto do_stats
if "%CH%"=="3" goto ask_course
if "%CH%"=="4" goto ask_course
if "%CH%"=="5" goto ask_course
echo 输入无效，回到菜单
goto menu

:do_login
python main.py login --platform %PLATFORM%
pause
goto menu

:do_list
python main.py course --platform %PLATFORM%
pause
goto menu

:do_stats
python main.py bank stats
pause
goto menu

:ask_course
echo.
set COURSE=
set /p COURSE="课程关键词（例如 电工基础，直接回车=全部课）: "
set FILTER=
if not "%COURSE%"=="" set FILTER=--course "%COURSE%"
echo.
if "%CH%"=="3" python main.py run --platform %PLATFORM% %FILTER% --kind video --duration 2h
if "%CH%"=="4" python main.py run --platform %PLATFORM% %FILTER% --kind quiz
if "%CH%"=="5" python main.py run --platform %PLATFORM% %FILTER%
echo.
pause
goto menu