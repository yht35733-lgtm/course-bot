# 清理 course-bot 留下的进程：控制台服务 + 它启动的浏览器
# 用法：stop.bat，或 powershell -File scripts\stop.ps1
$ErrorActionPreference = 'SilentlyContinue'
$root = Split-Path -Parent (Split-Path -Parent $PSCommandPath)
$n = 0

# 1) PID 文件里记录的控制台进程
$pidFile = Join-Path $root 'data\serve.pid'
if (Test-Path $pidFile) {
    $v = (Get-Content $pidFile -Raw).Trim()
    if ($v) {
        $proc = Get-Process -Id ([int]$v) -ErrorAction SilentlyContinue
        if ($proc) { Stop-Process -Id ([int]$v) -Force; $n++ }
    }
    Remove-Item $pidFile -Force
}

# 2) 兜底：任何命令行里带 main.py serve 的 python
Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object {
    $_.CommandLine -and $_.CommandLine -like '*main.py*serve*'
} | ForEach-Object {
    Stop-Process -Id $_.ProcessId -Force
    $n++
}

# 3) 用本项目 profile 目录启动的浏览器（Playwright 拉起的那些）
Get-CimInstance Win32_Process | Where-Object {
    $_.Name -match '^(chrome|chromium|msedge)' -and
    $_.CommandLine -and $_.CommandLine -like '*course-bot*'
} | ForEach-Object {
    Stop-Process -Id $_.ProcessId -Force
    $n++
}

Start-Sleep -Milliseconds 500
$left = (Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object {
    $_.CommandLine -and $_.CommandLine -like '*main.py*serve*'
} | Measure-Object).Count
Write-Host ("已清理 " + $n + " 个进程，残留服务 " + $left + " 个")
