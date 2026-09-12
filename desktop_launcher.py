"""Windows desktop entry point used by the packaged application.

The product remains a local web application, but a normal user starts it by
double-clicking one executable instead of running Python or a batch file.

关闭浏览器页面只会关掉界面，后台服务仍由本进程承载；因此常驻系统托盘：
托盘图标右键可「打开界面」或「退出」，退出前会停止 HTTP 服务并清理
遗留的 GPU 适配器进程。
"""
from __future__ import annotations

import os
import socket
import sys
import threading
import time
import traceback
import urllib.request
import webbrowser
from pathlib import Path

import uvicorn


def _log(message: str) -> None:
    """Keep a tiny first-start log for packaged-app diagnostics."""
    try:
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "afan Talking Video Agent"
        base.mkdir(parents=True, exist_ok=True)
        with (base / "launcher.log").open("a", encoding="utf-8") as output:
            output.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n")
    except OSError:
        pass


def _available_port() -> int:
    requested = os.environ.get("AFAN_PORT", "").strip()
    if requested.isdigit():
        port = int(requested)
        if 1 <= port <= 65535:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                if probe.connect_ex(("127.0.0.1", port)) != 0:
                    return port
    for port in (8000, 8001, 8002):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if probe.connect_ex(("127.0.0.1", port)) != 0:
                return port
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _write_current_url(url: str) -> None:
    """把实际访问地址写到固定位置，供 CLI 等工具自动发现随机端口。"""
    try:
        if getattr(sys, "frozen", False):
            base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "afan Talking Video Agent"
        else:
            base = Path(__file__).resolve().parent
        base.mkdir(parents=True, exist_ok=True)
        (base / "current_url.txt").write_text(url, encoding="utf-8")
    except OSError:
        pass


def _open_when_ready(url: str) -> None:
    if os.environ.get("AFAN_NO_BROWSER", "").lower() in {"1", "true", "yes"}:
        return
    for _ in range(100):
        try:
            with urllib.request.urlopen(url, timeout=0.5):
                webbrowser.open(url)
                return
        except OSError:
            time.sleep(0.15)


def _tray_icon_image():
    """生成托盘图标：青色圆角方块 + 白色圆点（与界面主色一致）。"""
    from PIL import Image, ImageDraw

    image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((4, 4, 60, 60), radius=14, fill=(22, 185, 207, 255))
    draw.ellipse((24, 24, 40, 40), fill=(255, 255, 255, 255))
    return image


def _run_with_tray(app, url: str, port: int) -> None:
    """托盘常驻模式：HTTP 服务在后台线程，主线程跑托盘消息循环。"""
    import pystray

    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_config=None, access_log=False))
    server_thread = threading.Thread(target=server.run, daemon=True)
    server_thread.start()

    def open_ui(icon=None, item=None):
        webbrowser.open(url)

    def quit_app(icon=None, item=None):
        _log("quit requested from tray")
        icon.stop()
        server.should_exit = True
        server_thread.join(timeout=8)
        try:
            from app.local_runtime import reap_orphan_adapters

            killed = reap_orphan_adapters()
            if killed:
                _log(f"quit: cleaned adapters {killed}")
        except Exception:
            traceback.print_exc(file=sys.stderr)
        os._exit(0)

    menu = pystray.Menu(
        pystray.MenuItem("打开界面", open_ui, default=True),
        pystray.MenuItem("退出", quit_app),
    )
    icon = pystray.Icon("afan-talking-video-agent", _tray_icon_image(), "afan Talking Video Agent", menu)
    _log("tray started")
    icon.run()


def main() -> None:
    _log("launcher started")
    port = _available_port()
    url = f"http://127.0.0.1:{port}"
    _write_current_url(url)
    _log(f"selected port {port}; importing app")
    # Import explicitly before starting Uvicorn. This makes frozen-build
    # failures visible in launcher.log and avoids fragile string imports.
    try:
        from app.main import app
    except Exception:
        _log("application import failed:\n" + traceback.format_exc())
        raise

    _log("app imported; starting local server")
    threading.Thread(target=_open_when_ready, args=(url,), daemon=True).start()
    try:
        # A windowed PyInstaller executable has no stdout/stderr handles.
        # Disabling Uvicorn's console formatter prevents it from trying to
        # call ``isatty`` on None during normal double-click launches.
        if os.environ.get("AFAN_NO_TRAY", "").lower() in {"1", "true", "yes"}:
            uvicorn.run(app, host="127.0.0.1", port=port, log_config=None, access_log=False)
            return
        _run_with_tray(app, url, port)
    except Exception:
        # 托盘不可用的环境（缺依赖等）退回旧行为：直接阻塞运行服务。
        _log("tray mode failed; falling back to plain server:\n" + traceback.format_exc())
        uvicorn.run(app, host="127.0.0.1", port=port, log_config=None, access_log=False)


if __name__ == "__main__":
    main()
