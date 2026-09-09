$ErrorActionPreference = 'Stop'

# 自动探测"真的能运行"的 Python。
# Windows 自带的 Microsoft Store 占位 python.exe 无法执行任何命令，
# 必须实际运行一次验证；否则 uvicorn 会以各种诡异的方式失败。
$python = $null
foreach ($name in @('python', 'py')) {
    $cmd = Get-Command $name -ErrorAction SilentlyContinue
    if (-not $cmd) { continue }
    & $cmd.Source -c "import sys" 2>$null
    if ($LASTEXITCODE -eq 0) { $python = $cmd.Source; break }
}
if (-not $python) {
    $launcher = Get-Command py -ErrorAction SilentlyContinue
    if ($launcher) {
        py -3 -c "import sys" 2>$null
        if ($LASTEXITCODE -eq 0) { $python = 'py -3' }
    }
}
if (-not $python) {
    # 与「一键启动.bat」一致：可自动下载华为云镜像的官方 Python。
    Write-Host "[!] 没有找到可用的 Python 3.10+（Microsoft Store 占位程序不算）。" -ForegroundColor Yellow
    Write-Host "    建议直接双击「一键启动.bat」，它会引导自动安装 Python、"
    Write-Host "    依赖和 FFmpeg（下载源均为国内可直连镜像）。"
    Write-Host "    手动安装：https://mirrors.huaweicloud.com/python/"
    exit 1
}

if ($python -eq 'py -3') {
    py -3 -m uvicorn app.main:app --host 127.0.0.1 --port 8000
} else {
    & $python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
}
