"""管理可选的本地 MuseTalk 引擎包。

主程序本身不捆绑 GPU 模型和 PyTorch。这个模块只负责把一个经过发布者
准备的引擎压缩包下载/解压到用户指定目录，并检查关键文件是否齐全。
实际推理仍由 ``MuseTalkProvider`` 调用，因而下载器和推理适配器彼此独立。
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import tarfile
import threading
import time
import urllib.parse
import urllib.request
import uuid
import zipfile
from pathlib import Path
from typing import Any, Mapping


ENGINE_ID = "musetalk-1.5"
_STATE_LOCK = threading.Lock()
_STATE: dict[str, Any] = {
    "status": "idle",
    "progress": 0,
    "message": "尚未安装本地引擎。",
    "error": "",
}


def default_engine_root(data_root: Path) -> Path:
    return data_root / "engines" / ENGINE_ID


def tools_ffmpeg_dir(data_root: Path) -> Path:
    """FFmpeg 自动就位目录：启动脚本/用户把 ffmpeg 放这里，无需管理员权限，不依赖 PATH。"""
    return data_root / "tools" / "ffmpeg" / "bin"


def resolve_ffmpeg(data_root: Path | None = None, settings: Mapping[str, str] | None = None) -> str:
    """按用户可维护的顺序解析 FFmpeg 可执行文件路径。

    优先级：设置项/环境变量 → 数据目录 tools\ffmpeg\bin（开箱即用的自动
    下载位置）→ PATH。全部落空返回空字符串，由调用方决定如何提示。
    """
    candidates: list[Path] = []
    configured = str((settings or {}).get("FFMPEG_PATH") or os.getenv("FFMPEG_PATH", "")).strip()
    if configured:
        candidate = Path(configured).expanduser()
        candidates.append(candidate / "ffmpeg.exe" if candidate.is_dir() else candidate)
    if data_root is not None:
        candidates.append(tools_ffmpeg_dir(data_root) / "ffmpeg.exe")
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return shutil.which("ffmpeg") or ""


def _set_state(**updates: Any) -> None:
    with _STATE_LOCK:
        _STATE.update(updates)


def _state() -> dict[str, Any]:
    with _STATE_LOCK:
        return dict(_STATE)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_member_path(root: Path, name: str) -> Path:
    # zip/tar 中的绝对路径或 .. 路径都拒绝，避免压缩包覆盖用户文件。
    candidate = (root / name).resolve()
    if candidate != root.resolve() and root.resolve() not in candidate.parents:
        raise ValueError(f"引擎包包含不安全路径：{name}")
    return candidate


def _extract_archive(archive: Path, destination: Path) -> None:
    suffix = archive.name.lower()
    if suffix.endswith(".zip"):
        with zipfile.ZipFile(archive) as source:
            members = source.infolist()
            for member in members:
                _safe_member_path(destination, member.filename)
            source.extractall(destination)
        return
    if suffix.endswith((".tar.gz", ".tgz", ".tar")):
        with tarfile.open(archive) as source:
            members = source.getmembers()
            for member in members:
                _safe_member_path(destination, member.name)
                if member.issym() or member.islnk():
                    raise ValueError("引擎包不允许包含符号链接")
            source.extractall(destination)
        return
    raise ValueError("只支持 .zip、.tar.gz、.tgz 或 .tar 引擎包")


def _find_engine_root(staging: Path) -> Path | None:
    """允许压缩包外层多包一层目录。"""
    candidates = [staging, *[item for item in staging.iterdir() if item.is_dir()]]
    for candidate in candidates:
        if (candidate / "models" / "musetalkV15" / "unet.pth").is_file() and (
            candidate / "models" / "musetalkV15" / "musetalk.json"
        ).is_file():
            return candidate
    return None


def inspect_engine(root: Path, *, python: str | None = None, ffmpeg: str | None = None) -> dict[str, Any]:
    root = root.expanduser().resolve()
    model_dir = root / "models" / "musetalkV15"
    required = {
        "unet": model_dir / "unet.pth",
        "config": model_dir / "musetalk.json",
        "whisper": root / "models" / "whisper",
        "inference": root / "scripts" / "inference.py",
    }
    missing = [name for name, path in required.items() if not path.exists()]
    nvidia = shutil.which("nvidia-smi")
    nvidia_info = ""
    if nvidia:
        try:
            result = subprocess.run([nvidia, "--query-gpu=name,memory.total", "--format=csv,noheader"], capture_output=True, text=True, timeout=8)
            nvidia_info = result.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            nvidia_info = ""
    python_exe = python or os.getenv("MUSETALK_PYTHON") or shutil.which("python") or ""
    ffmpeg_exe = ffmpeg or os.getenv("FFMPEG_PATH") or shutil.which("ffmpeg") or ""
    return {
        "id": ENGINE_ID,
        "root": str(root),
        "installed": not missing,
        "missing": missing,
        "python": python_exe,
        "python_exists": bool(python_exe and Path(python_exe).exists()),
        "ffmpeg": ffmpeg_exe,
        "ffmpeg_exists": bool(ffmpeg_exe and Path(ffmpeg_exe).exists()),
        "nvidia_smi": bool(nvidia),
        "gpu": nvidia_info,
    }


def status(data_root: Path, settings: Mapping[str, str] | None = None) -> dict[str, Any]:
    settings = settings or {}
    configured = str(settings.get("MUSETALK_ENGINE_ROOT") or os.getenv("MUSETALK_ENGINE_ROOT", "")).strip()
    root = Path(configured) if configured else default_engine_root(data_root)
    result = inspect_engine(root, ffmpeg=resolve_ffmpeg(data_root, settings))
    result["custom"] = bool(configured)
    result["task"] = _state()
    result["download_configured"] = bool(str(settings.get("MUSETALK_ENGINE_URL") or os.getenv("MUSETALK_ENGINE_URL", "")).strip())
    return result


def _download(url: str, destination: Path, expected_sha256: str | None = None) -> None:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("引擎下载地址必须是 http(s) URL")
    request = urllib.request.Request(url, headers={"User-Agent": "afan-talking-head-agent/0.2"})
    with urllib.request.urlopen(request, timeout=60) as response, destination.open("wb") as output:
        total = int(response.headers.get("Content-Length") or 0)
        received = 0
        while chunk := response.read(1024 * 1024):
            output.write(chunk)
            received += len(chunk)
            progress = int(received * 70 / total) if total else 5
            _set_state(progress=min(70, max(5, progress)), message=f"正在下载引擎包：{received // (1024 * 1024)} MB")
    if expected_sha256 and _sha256(destination).lower() != expected_sha256.lower():
        raise ValueError("引擎包 SHA-256 校验失败，文件可能损坏或来源不匹配")


def _install_worker(url: str, root: Path, expected_sha256: str | None) -> None:
    token = uuid.uuid4().hex
    staging = root.parent / f".{ENGINE_ID}-{token}.staging"
    archive = root.parent / f".{ENGINE_ID}-{token}.download"
    try:
        root.parent.mkdir(parents=True, exist_ok=True)
        staging.mkdir(parents=True, exist_ok=True)
        _download(url, archive, expected_sha256)
        _set_state(progress=75, message="正在解压并检查模型文件…")
        _extract_archive(archive, staging)
        source = _find_engine_root(staging)
        if source is None:
            raise ValueError("引擎包缺少 models/musetalkV15/unet.pth 或 musetalk.json")
        if root.exists():
            backup = root.with_name(root.name + f".backup-{int(time.time())}")
            root.rename(backup)
        shutil.move(str(source), str(root))
        check = inspect_engine(root)
        if not check["installed"]:
            raise ValueError("引擎解压后关键文件仍不完整：" + ", ".join(check["missing"]))
        _set_state(status="succeeded", progress=100, message="MuseTalk 1.5 本地引擎已安装。", error="")
    except Exception as error:  # noqa: BLE001 - 转为用户可读的安装状态
        _set_state(status="failed", progress=0, message="本地引擎安装失败。", error=str(error))
    finally:
        shutil.rmtree(staging, ignore_errors=True)
        try:
            archive.unlink(missing_ok=True)
        except OSError:
            pass


def start_install(data_root: Path, *, url: str | None = None, root: str | None = None, sha256: str | None = None) -> dict[str, Any]:
    current = _state()
    if current.get("status") == "running":
        return current
    source = (url or os.getenv("MUSETALK_ENGINE_URL", "")).strip()
    if not source:
        raise ValueError("尚未配置引擎包下载地址；请填写项目发布的 MuseTalk 引擎包 URL。")
    target = Path(root).expanduser() if root else default_engine_root(data_root)
    _set_state(status="running", progress=1, message="准备下载本地引擎…", error="")
    threading.Thread(target=_install_worker, args=(source, target.resolve(), sha256), daemon=True).start()
    return _state()
