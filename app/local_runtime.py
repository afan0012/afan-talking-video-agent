"""Lifecycle manager for optional GPU model runtimes.

The desktop application is deliberately kept separate from PyTorch and model
weights.  Once a supported local engine has been installed, this module starts
its tiny loopback-only FastAPI adapter and remembers the ephemeral local URL.
The creation workflow never needs to ask an ordinary user for ports, commands,
Docker paths, or a remote machine address.

The installer/distribution of the engine itself is a separate release concern:
this manager only launches an engine that is already present on the user's
computer.  Keeping that boundary explicit avoids silently downloading unknown
executables or model files.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any, Mapping

from app.local_engine import resolve_ffmpeg


_LOCK = threading.RLock()
_PROCESSES: dict[str, subprocess.Popen[bytes] | subprocess.Popen[str]] = {}
_STATE: dict[str, dict[str, Any]] = {
    "musetalk": {"status": "stopped", "url": "", "error": "", "started_at": None},
    "qwen3_tts": {"status": "stopped", "url": "", "error": "", "started_at": None},
    "funasr": {"status": "stopped", "url": "", "error": "", "started_at": None},
}


def _port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _ready(url: str, *, timeout: float = 18) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"{url}/health", timeout=1.5) as response:
                if 200 <= response.status < 300:
                    return True
        except OSError:
            time.sleep(0.25)
    return False


def _health(url: str, *, timeout: float = 3.0) -> dict[str, Any] | None:
    """读取适配器的 /health 内容；不可达或旧版返回 None。"""
    try:
        with urllib.request.urlopen(f"{url}/health", timeout=timeout) as response:
            if 200 <= response.status < 300:
                return json.loads(response.read().decode("utf-8", "replace"))
    except (OSError, ValueError):
        return None
    return None


def _engine_python(root: Path, variable: str, settings: Mapping[str, str] | None = None) -> str:
    # 解释器优先取设置页保存的路径（导入模型包时写入），其次 OS 环境变量。
    configured = str((settings or {}).get(variable) or os.getenv(variable, "")).strip()
    candidates = [
        Path(configured) if configured else None,
        root / ".venv" / "Scripts" / "python.exe",
        root / "venv" / "Scripts" / "python.exe",
        root / ".venv" / "bin" / "python",
        root / "venv" / "bin" / "python",
    ]
    for candidate in candidates:
        if candidate and candidate.is_file():
            return str(candidate)
    # This is useful in development, where the current interpreter can be the
    # dedicated model environment.  A release engine should ship its own venv.
    return sys.executable


def _stop_dead(name: str) -> None:
    process = _PROCESSES.get(name)
    if process and process.poll() is not None:
        _PROCESSES.pop(name, None)
        _STATE[name].update(status="stopped", url="", error="本地模型服务已退出。")


def _require_fresh_environment(name: str, *, ffmpeg_ok: bool) -> None:
    """环境变化后重启仍在服役的旧适配器实例。

    适配器进程会存活很久，继承的是启动那一刻的环境；配置修复（例如
    ffmpeg 就位）后，旧实例会把每一次推理都带进同样的失败。这里用
    /health 暴露的 ffmpeg_ok 发现这种错位并重启实例；旧版适配器没有
    该字段时保持原行为，由复用逻辑照常接管。
    """
    with _LOCK:
        process = _PROCESSES.get(name)
    if process is None or process.poll() is not None:
        return
    url = str(_STATE[name].get("url") or "")
    if not url:
        return
    health = _health(url)
    if health is None or "ffmpeg_ok" not in health:
        return
    if bool(health["ffmpeg_ok"]) == ffmpeg_ok:
        return
    try:
        process.terminate()
    except OSError:
        pass
    with _LOCK:
        _PROCESSES.pop(name, None)
        _STATE[name].update(status="stopped", url="", error="本地模型环境已更新，正在以新配置重启。")


def status(name: str) -> dict[str, Any]:
    with _LOCK:
        _stop_dead(name)
        return dict(_STATE[name])


def _launch(name: str, command: list[str], environment: dict[str, str], log_path: Path) -> dict[str, Any]:
    with _LOCK:
        _stop_dead(name)
        existing = _PROCESSES.get(name)
        if existing and existing.poll() is None:
            return dict(_STATE[name])
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log = log_path.open("ab")
        try:
            process = subprocess.Popen(
                command,
                env=environment,
                stdout=log,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
            )
        except OSError as error:
            log.close()
            _STATE[name].update(status="failed", url="", error=f"无法启动本地模型服务：{error}")
            return dict(_STATE[name])
        # The child owns the duplicated OS handle.  Closing our copy prevents
        # keeping the log locked on Windows after the service exits.
        log.close()
        _PROCESSES[name] = process
        _STATE[name].update(status="starting", error="", started_at=time.time())
        return dict(_STATE[name])


def _start(
    name: str,
    *,
    command: list[str],
    url: str,
    environment: dict[str, str],
    log_path: Path,
) -> dict[str, Any]:
    _launch(name, command, environment, log_path)
    if _ready(url):
        with _LOCK:
            _STATE[name].update(status="running", url=url, error="")
            return dict(_STATE[name])
    with _LOCK:
        process = _PROCESSES.get(name)
        detail = "服务启动超时，请查看本地模型日志。"
        if process and process.poll() is not None:
            detail = f"服务启动后退出（退出码 {process.returncode}），请查看本地模型日志。"
        _STATE[name].update(status="failed", url="", error=detail)
        return dict(_STATE[name])


def muse_root(data_root: Path) -> Path:
    return data_root / "engines" / "musetalk-1.5"


def qwen_root(data_root: Path) -> Path:
    return data_root / "engines" / "qwen3-tts"


# 认可的目录命名：官方 HuggingFace 快照名和发布者资源包常用的短名都接受，
# 这样用户既可以把官方模型放进标准目录，也可以直接指向已下载的模型包。
QWEN_TOKENIZER_DIRS = ("Qwen3-TTS-Tokenizer-12Hz", "tokenizer")
QWEN_CUSTOM_DIRS = (
    "Qwen3-TTS-12Hz-0.6B-CustomVoice",
    "Qwen3-TTS-12Hz-1.7B-CustomVoice",
    "custom-voice-0.6b",
    "custom-voice-1.7b",
)
QWEN_BASE_DIRS = (
    "Qwen3-TTS-12Hz-0.6B-Base",
    "Qwen3-TTS-12Hz-1.7B-Base",
    "base-0.6b",
    "base-1.7b",
)


def _qwen_configured_root(data_root: Path, settings: Mapping[str, str] | None) -> Path:
    configured = str((settings or {}).get("QWEN_TTS_ENGINE_ROOT") or os.getenv("QWEN_TTS_ENGINE_ROOT", "")).strip()
    return Path(configured).expanduser() if configured else qwen_root(data_root)


def _first_config_dir(root: Path, names: tuple[str, ...]) -> Path | None:
    for name in names:
        if (root / name / "config.json").is_file():
            return root / name
    return None


def qwen_engine_status(data_root: Path, settings: Mapping[str, str] | None = None) -> dict[str, Any]:
    root = _qwen_configured_root(data_root, settings)
    tokenizer = _first_config_dir(root, QWEN_TOKENIZER_DIRS)
    custom = _first_config_dir(root, QWEN_CUSTOM_DIRS)
    base = _first_config_dir(root, QWEN_BASE_DIRS)
    return {
        "id": "qwen3-tts",
        "root": str(root),
        "custom": bool(str((settings or {}).get("QWEN_TTS_ENGINE_ROOT") or os.getenv("QWEN_TTS_ENGINE_ROOT", "")).strip()),
        "tokenizer": tokenizer is not None,
        "custom_voice": custom is not None,
        "voice_clone": base is not None,
        "custom_model_dir": str(custom or ""),
        "base_model_dir": str(base or ""),
        "installed": tokenizer is not None and (custom is not None or base is not None),
        "runtime": status("qwen3_tts"),
    }


def start_musetalk(data_root: Path, adapter_script: Path, *, engine_root: Path | None = None, settings: Mapping[str, str] | None = None) -> dict[str, Any]:
    root = (engine_root or muse_root(data_root)).expanduser().resolve()
    required = [root / "scripts" / "inference.py", root / "models" / "musetalkV15" / "unet.pth"]
    if not all(path.is_file() for path in required):
        return {"status": "missing", "url": "", "error": "MuseTalk 1.5 本地引擎尚未安装完整。"}
    port = _port()
    url = f"http://127.0.0.1:{port}"
    environment = os.environ.copy()
    ffmpeg = resolve_ffmpeg(data_root)
    if ffmpeg:
        environment["FFMPEG_PATH"] = ffmpeg
    _require_fresh_environment("musetalk", ffmpeg_ok=bool(ffmpeg))
    environment.update({
        "MUSETALK_ROOT": str(root),
        "MUSETALK_JOBS_ROOT": str(data_root / "runtime-jobs" / "musetalk"),
        "MUSETALK_PORT": str(port),
        "MUSETALK_HOST": "127.0.0.1",
    })
    # 把设置里的看门狗秒数透传给适配器（0/留空 = 不限制；不传则适配器也不限制）。
    timeout_value = str((settings or {}).get("MUSETALK_TIMEOUT") or "").strip()
    if timeout_value:
        environment["MUSETALK_TIMEOUT"] = timeout_value
    command = [_engine_python(root, "MUSETALK_PYTHON", settings), str(adapter_script)]
    return _start("musetalk", command=command, url=url, environment=environment, log_path=data_root / "logs" / "musetalk.log")


def start_qwen3_tts(data_root: Path, adapter_script: Path, *, settings: Mapping[str, str] | None = None) -> dict[str, Any]:
    root = _qwen_configured_root(data_root, settings)
    info = qwen_engine_status(data_root, settings)
    if not info["installed"]:
        return {"status": "missing", "url": "", "error": "Qwen3-TTS 本地模型尚未安装完整。"}
    custom = info.get("custom_model_dir") or str(root / "Qwen3-TTS-12Hz-0.6B-CustomVoice")
    base = info.get("base_model_dir") or str(root / "Qwen3-TTS-12Hz-0.6B-Base")
    port = _port()
    url = f"http://127.0.0.1:{port}"
    environment = os.environ.copy()
    environment.update({
        "QWEN_TTS_MODEL_ROOT": str(root),
        "QWEN_TTS_CUSTOM_MODEL": str(custom),
        "QWEN_TTS_BASE_MODEL": str(base),
        "QWEN_TTS_PORT": str(port),
        "QWEN_TTS_HOST": "127.0.0.1",
    })
    command = [_engine_python(root, "QWEN_TTS_PYTHON", settings), str(adapter_script)]
    return _start("qwen3_tts", command=command, url=url, environment=environment, log_path=data_root / "logs" / "qwen3-tts.log")


def funasr_root(data_root: Path) -> Path:
    return data_root / "engines" / "funasr"


def _has_config(path: Path) -> bool:
    return any((path / name).is_file() for name in ("config.json", "configuration.json"))


def _funasr_configured_root(data_root: Path, settings: Mapping[str, str] | None) -> Path:
    configured = str((settings or {}).get("FUNASR_ENGINE_ROOT") or os.getenv("FUNASR_ENGINE_ROOT", "")).strip()
    return Path(configured).expanduser() if configured else funasr_root(data_root)


def start_funasr(data_root: Path, adapter_script: Path, *, engine_root: Path | None = None, settings: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Start the loopback FunASR adapter inside the engine package's runtime.

    The interpreter comes from the model package (``FUNASR_PYTHON`` or a
    sibling runtime such as ``afan-voice-engines/runtime-py312-cu128``); the
    application process itself never imports funasr or torch.
    """
    from app.funasr_local import discover_runtime_python

    root = (engine_root or _funasr_configured_root(data_root, settings)).expanduser().resolve()
    has_model = any(
        (root / layout).is_dir() and _has_config(root / layout)
        for layout in ("SenseVoiceSmall", "paraformer-zh")
    )
    if not root.is_dir() or not has_model:
        return {"status": "missing", "url": "", "error": "本地 FunASR 模型尚未安装完整。"}
    python = discover_runtime_python(root, settings)
    if not python:
        return {
            "status": "missing",
            "url": "",
            "error": "没有找到引擎包自带的 Python 运行时；请先在「一键接入模型包」导入 voice 模型包。",
        }
    port = _port()
    url = f"http://127.0.0.1:{port}"
    environment = os.environ.copy()
    environment.update({
        "FUNASR_ENGINE_ROOT": str(root),
        "FUNASR_PORT": str(port),
        "FUNASR_HOST": "127.0.0.1",
    })
    device = str((settings or {}).get("FUNASR_DEVICE") or os.getenv("FUNASR_DEVICE", "")).strip()
    if device:
        environment["FUNASR_DEVICE"] = device
    command = [python, str(adapter_script)]
    return _start("funasr", command=command, url=url, environment=environment, log_path=data_root / "logs" / "funasr.log")


def reap_orphan_adapters() -> list[int]:
    """清理上次运行遗留的适配器进程（服务崩溃/重启后它们会成为孤儿）。

    每个适配器占用 2~4 GB 内存提交额度；堆积会耗尽 Windows 页面文件
    （os error 1455）。只在应用启动时调用一次；通过命令行特征识别
    funasr_server / qwen_tts_server / musetalk_remote_server，不会误伤主程序。
    """
    if os.name != "nt":
        return []
    try:
        result = subprocess.run(
            [
                "powershell", "-NoProfile", "-Command",
                "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
                "Where-Object { $_.CommandLine -match 'funasr_server|qwen_tts_server|musetalk_remote_server' } | "
                "ForEach-Object { $_.ProcessId }",
            ],
            capture_output=True,
            text=True,
            timeout=40,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        return []
    killed: list[int] = []
    for line in (result.stdout or "").split():
        try:
            pid = int(line.strip())
        except ValueError:
            continue
        if pid == os.getpid():
            continue
        try:
            os.kill(pid, 9)
            killed.append(pid)
        except OSError:
            pass
    return killed


def shutdown() -> None:
    """Best-effort shutdown for the desktop process; never kill unrelated apps."""
    with _LOCK:
        processes = list(_PROCESSES.items())
        _PROCESSES.clear()
    for name, process in processes:
        if process.poll() is None:
            try:
                process.terminate()
            except OSError:
                pass
        with _LOCK:
            _STATE[name].update(status="stopped", url="", error="")
