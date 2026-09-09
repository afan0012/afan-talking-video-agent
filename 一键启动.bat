@echo off
chcp 65001 >nul
cd /d "%~dp0"
title afan Talking Head Agent

echo ============================================
echo   afan Talking Head Agent - Local Startup
echo ============================================
echo.

set "PYCMD="

rem [1/5] 找到"真的能运行"的 Python。
rem 注意：Windows 自带的 Microsoft Store 占位 python.exe 无法执行任何命令，
rem 必须实际运行一次来验证，否则后面会以各种诡异的方式失败。
where python >nul 2>nul
if not errorlevel 1 (
    python -c "import sys" >nul 2>nul
    if not errorlevel 1 set "PYCMD=python"
)
if not defined PYCMD (
    where py >nul 2>nul
    if not errorlevel 1 (
        py -3 -c "import sys" >nul 2>nul
        if not errorlevel 1 set "PYCMD=py -3"
    )
)
if defined PYCMD goto :python-ready

echo [!] 没有找到可用的 Python 3.10+。
echo     （已安装却仍提示？请确认安装时勾选了 "Add Python to PATH"。）
echo.
echo     可以自动下载并安装官方 Python 3.11.9：用户级安装、无需管理员，
echo     下载源为华为云镜像，国内可直连。
echo.
choice /c YN /m "是否现在自动安装 Python"
if errorlevel 2 goto :no-python

set "PYSETUP=%TEMP%\python-3.11.9-amd64.exe"
echo 正在下载 Python 3.11.9（约 25MB）...
curl -L -# -o "%PYSETUP%" "https://mirrors.huaweicloud.com/python/3.11.9/python-3.11.9-amd64.exe"
if errorlevel 1 (
    echo 下载失败。请手动下载安装：
    echo   https://mirrors.huaweicloud.com/python/3.11.9/python-3.11.9-amd64.exe
    goto :no-python
)
echo 正在静默安装（约 1 分钟，请稍候）...
"%PYSETUP%" /quiet InstallAllUsers=0 PrependPath=1 Include_test=0
if errorlevel 1 (
    echo 安装失败。请手动安装后重新运行本脚本。
    del "%PYSETUP%" >nul 2>nul
    goto :no-python
)
del "%PYSETUP%" >nul 2>nul
set "PYCMD=%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
if not exist "%PYCMD%" set "PYCMD=%ProgramFiles%\Python311\python.exe"
if exist "%PYCMD%" goto :python-ready

:no-python
echo.
echo [错误] 没有 Python 无法启动。请安装 Python 3.10+ 后重新运行本脚本。
echo   华为云镜像：https://mirrors.huaweicloud.com/python/
echo   官方下载：  https://www.python.org/downloads/
echo.
pause
exit /b 1

:python-ready
echo [1/4] 使用 Python：%PYCMD%

rem [2/4] 首次运行自动安装依赖（清华镜像优先，失败回退官方源）
echo [2/4] 检查依赖（首次运行需要几分钟，请耐心等待）...
%PYCMD% -c "import fastapi, uvicorn, dashscope, httpx, multipart" >nul 2>nul
if errorlevel 1 (
    echo       正在安装依赖，请勿关闭窗口...
    %PYCMD% -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
    if errorlevel 1 %PYCMD% -m pip install -r requirements.txt
)

rem [3/4] FFmpeg 自动就位：缺失时从国内镜像下载到 tools\ffmpeg，不改系统 PATH
echo [3/4] 检查 FFmpeg（视频剪辑与导出必需）...
%PYCMD% scripts\ensure_ffmpeg.py
if errorlevel 1 echo [提示] FFmpeg 未就位，视频剪辑/导出暂不可用；其余功能不受影响。

rem [4/4] 启动服务
if not exist ".env" (
    echo [提示] 还没有配置 .env（API 密钥）。没有密钥也能打开页面，
    echo        但文案生成、云端配音等功能需要先在设置里填写密钥。
)
echo [4/4] 正在启动服务...
echo 浏览器将自动打开；如没有打开，请手动访问 http://127.0.0.1:8000
echo 关闭本窗口即停止程序。
echo.

start "" http://127.0.0.1:8000
%PYCMD% -m uvicorn app.main:app --host 127.0.0.1 --port 8000

pause
