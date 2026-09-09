"""One-click MuseTalk model/source installer.

The normal UI deliberately does not accept arbitrary model URLs.  This module
uses a small, versioned manifest that points at the upstream MuseTalk source
and its documented Hugging Face model repositories (with the public mirror as
the first endpoint for mainland users).  Downloads are resumable and are
assembled in a staging directory before the engine is made visible.

This downloads the engine source and weights only.  It does not install a GPU
driver or silently replace a user's Python/CUDA environment.
"""

from __future__ import annotations

import json
import os
import shutil
import threading
import time
import urllib.error
import urllib.request
import uuid
import zipfile
from pathlib import Path
from typing import Any, Callable

from .local_engine import ENGINE_ID, inspect_engine


# Pin the source snapshot used by this adapter so a future upstream change
# cannot silently alter an installed engine.  Update deliberately when we
# review a newer MuseTalk release.
SOURCE_REVISION = "0a89dec45a0192b824e3cf4daf96c239440c5ed8"
SOURCE_URL = f"https://codeload.github.com/TMElyralab/MuseTalk/zip/{SOURCE_REVISION}"
MIRROR = "https://hf-mirror.com"
HF = "https://huggingface.co"

# The file list follows the upstream download_weights.bat.  Paths are fixed;
# users never have to copy URLs or decide where model files belong.
MANIFEST: tuple[dict[str, Any], ...] = (
    {"name": "MuseTalk source", "url": SOURCE_URL, "fallback": None, "path": "__source__.zip"},
    {"name": "MuseTalk 1.5 weights", "repo": "TMElyralab/MuseTalk", "remote": "musetalkV15/unet.pth", "path": "models/musetalkV15/unet.pth"},
    {"name": "MuseTalk 1.5 config", "repo": "TMElyralab/MuseTalk", "remote": "musetalkV15/musetalk.json", "path": "models/musetalkV15/musetalk.json"},
    {"name": "SD VAE config", "repo": "stabilityai/sd-vae-ft-mse", "remote": "config.json", "path": "models/sd-vae/config.json"},
    {"name": "SD VAE weights", "repo": "stabilityai/sd-vae-ft-mse", "remote": "diffusion_pytorch_model.bin", "path": "models/sd-vae/diffusion_pytorch_model.bin"},
    {"name": "Whisper config", "repo": "openai/whisper-tiny", "remote": "config.json", "path": "models/whisper/config.json"},
    {"name": "Whisper weights", "repo": "openai/whisper-tiny", "remote": "pytorch_model.bin", "path": "models/whisper/pytorch_model.bin"},
    {"name": "Whisper preprocessor", "repo": "openai/whisper-tiny", "remote": "preprocessor_config.json", "path": "models/whisper/preprocessor_config.json"},
    {"name": "DWPose weights", "repo": "yzd-v/DWPose", "remote": "dw-ll_ucoco_384.pth", "path": "models/dwpose/dw-ll_ucoco_384.pth"},
    {"name": "SyncNet weights", "repo": "ByteDance/LatentSync", "remote": "latentsync_syncnet.pt", "path": "models/syncnet/latentsync_syncnet.pt"},
    {"name": "Face parsing weights", "repo": "ManyOtherFunctions/face-parse-bisent", "remote": "79999_iter.pth", "path": "models/face-parse-bisent/79999_iter.pth"},
    {"name": "Face parsing backbone", "repo": "ManyOtherFunctions/face-parse-bisent", "remote": "resnet18-5c106cde.pth", "path": "models/face-parse-bisent/resnet18-5c106cde.pth"},
)

_LOCK = threading.Lock()
_STATE: dict[str, Any] = {
    "status": "idle",
    "progress": 0,
    "current": "",
    "message": "",
    "error": "",
    "started_at": None,
}


def _set(**updates: Any) -> None:
    with _LOCK:
        _STATE.update(updates)


def download_status() -> dict[str, Any]:
    with _LOCK:
        return dict(_STATE)


def _url(item: dict[str, Any], base: str) -> str:
    if item.get("url"):
        return str(item["url"])
    return f"{base}/{item['repo']}/resolve/main/{item['remote']}"


def _safe_target(root: Path, relative: str) -> Path:
    target = (root / relative).resolve()
    if target != root.resolve() and root.resolve() not in target.parents:
        raise ValueError(f"下载清单包含不安全路径：{relative}")
    return target


def _download(urls: list[str], destination: Path, report: Callable[[int, int | None], None]) -> None:
    """Download atomically to ``.part`` and resume a previous partial file."""
    part = destination.with_name(destination.name + ".part")
    destination.parent.mkdir(parents=True, exist_ok=True)
    last_error: Exception | None = None
    for url in urls:
        try:
            existing = part.stat().st_size if part.exists() else 0
            headers = {"User-Agent": "afan-talking-head-agent/0.2"}
            if existing:
                headers["Range"] = f"bytes={existing}-"
            request = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(request, timeout=90) as response:
                # Some mirrors ignore Range and return 200.  Restart safely.
                resumed = existing and response.status == 206
                if not resumed:
                    existing = 0
                mode = "ab" if resumed else "wb"
                length = response.headers.get("Content-Length")
                total = (existing + int(length)) if length and resumed else (int(length) if length else None)
                received = existing
                with part.open(mode) as output:
                    while chunk := response.read(1024 * 1024):
                        output.write(chunk)
                        received += len(chunk)
                        report(received, total)
            if part.stat().st_size <= 0:
                raise ValueError("服务器返回了空文件")
            part.replace(destination)
            return
        except (OSError, ValueError, urllib.error.URLError) as error:
            last_error = error
    raise RuntimeError(f"下载失败：{last_error}") from last_error


def _extract_source(archive: Path, staging: Path) -> Path:
    staging.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as source:
        for member in source.infolist():
            target = _safe_target(staging, member.filename)
            if member.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with source.open(member) as src, target.open("wb") as dst:
                    shutil.copyfileobj(src, dst)
    candidates = [staging, *[item for item in staging.iterdir() if item.is_dir()]]
    for candidate in candidates:
        if (candidate / "scripts" / "inference.py").is_file():
            return candidate
    raise ValueError("MuseTalk 源码包不完整，未找到推理脚本。")


def _worker(data_root: Path) -> None:
    engine_root = (data_root / "engines" / ENGINE_ID).resolve()
    parent = engine_root.parent
    token = uuid.uuid4().hex
    staging = parent / f".{ENGINE_ID}-{token}.staging"
    archive = staging / "__source__.zip"
    try:
        parent.mkdir(parents=True, exist_ok=True)
        staging.mkdir(parents=True, exist_ok=True)
        count = len(MANIFEST)
        source_root: Path | None = None
        for index, item in enumerate(MANIFEST):
            _set(current=item["name"], message=f"正在下载 {item['name']}…")

            def report(received: int, total: int | None, *, i=index) -> None:
                fraction = (received / total) if total else 0.02
                _set(progress=min(98, int(((i + fraction) / count) * 98)))

            urls = [_url(item, MIRROR)] if item.get("repo") else [_url(item, MIRROR)]
            if item.get("repo"):
                urls.append(_url(item, HF))
            target = archive if item["path"] == "__source__.zip" else _safe_target(staging, item["path"])
            _download(urls, target, report)
            if item["path"] == "__source__.zip":
                source_root = _extract_source(target, staging / "source")

        if source_root is None:
            raise ValueError("未找到 MuseTalk 源码。")
        # Move source and downloaded models into the final canonical root.
        assembled = parent / f".{ENGINE_ID}-{token}.assembled"
        if assembled.exists():
            shutil.rmtree(assembled, ignore_errors=True)
        shutil.copytree(source_root, assembled)
        for child in (staging / "models").iterdir():
            dest = assembled / "models" / child.name
            if dest.exists():
                shutil.rmtree(dest) if dest.is_dir() else dest.unlink()
            shutil.copytree(child, dest) if child.is_dir() else shutil.copy2(child, dest)
        check = inspect_engine(assembled)
        if not check["installed"]:
            raise ValueError("下载完成但关键文件不完整：" + ", ".join(check["missing"]))
        if engine_root.exists():
            backup = parent / f"{ENGINE_ID}.backup-{int(time.time())}"
            engine_root.rename(backup)
        assembled.rename(engine_root)
        _set(status="succeeded", progress=100, current="", message="MuseTalk 已下载并安装到本机。", error="")
    except Exception as error:  # noqa: BLE001
        _set(status="failed", progress=0, current="", message="MuseTalk 下载失败。", error=str(error))
    finally:
        shutil.rmtree(staging, ignore_errors=True)
        assembled = parent / f".{ENGINE_ID}-{token}.assembled"
        shutil.rmtree(assembled, ignore_errors=True)


def start_download(data_root: Path) -> dict[str, Any]:
    current = download_status()
    if current.get("status") == "running":
        return current
    _set(status="running", progress=1, current="", message="正在准备 MuseTalk…", error="", started_at=time.time())
    threading.Thread(target=_worker, args=(data_root.resolve(),), daemon=True).start()
    return download_status()


def manifest_info() -> dict[str, Any]:
    return {"item_count": len(MANIFEST), "engine_id": ENGINE_ID, "download_available": True}
