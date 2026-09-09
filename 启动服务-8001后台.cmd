@echo off
chcp 65001 >nul
cd /d "%~dp0"
title afan Talking Head Agent - 8001
where python >nul 2>nul
if errorlevel 1 (
    echo 未找到 Python。请先双击「一键启动.bat」完成环境安装，
    echo 或者重新运行一次「一键启动.bat」后关闭它，再运行本脚本。
    pause
    exit /b 1
)
echo 正在后台启动服务（最小化窗口，关闭该窗口即停止服务）...
start "afan-talking-head-8001" /min cmd /c "python -m uvicorn app.main:app --host 127.0.0.1 --port 8001 > logs\server-8001.log 2>&1"
timeout /t 4 /nobreak >nul
curl -s -o nul -w "服务状态：HTTP %%{http_code}\n" --max-time 5 http://127.0.0.1:8001/api/health
echo 访问地址：http://127.0.0.1:8001
pause
