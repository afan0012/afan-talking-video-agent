"""确保 FFmpeg/ffprobe 可用：缺失时自动下载到数据目录 tools\ffmpeg\bin。

设计目标（开箱即用）：
- 不要求管理员权限、不改系统 PATH、可放任意数据盘；
- 下载源优先国内直连（npmmirror 二进制镜像），失败再回退 GitHub 发布包；
- 幂等：已可用的环境直接退出，重复运行无副作用。

用法：python scripts/ensure_ffmpeg.py [--force]
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from app.local_engine import resolve_ffmpeg, tools_ffmpeg_dir  # noqa: E402

# npmmirror 托管的 ffmpeg-static 官方构建（b6.0），国内直连速度快。
NPMMIRROR_FILES = {
    "ffmpeg.exe": "https://registry.npmmirror.com/-/binary/ffmpeg-static/b6.0/ffmpeg-win32-x64",
    "ffprobe.exe": "https://registry.npmmirror.com/-/binary/ffmpeg-static/b6.0/ffprobe-win32-x64",
}
# 回退：GitHub 发布包（zip，内含 bin/ffmpeg.exe 与 bin/ffprobe.exe）。
GITHUB_ZIP = "https://github.com/GyanD/codexffmpeg/releases/download/6.1.1/ffmpeg-6.1.1-full_build.zip"

CHUNK = 1024 * 1024


def _data_root() -> Path:
    """与主程序一致的源码运行数据目录；尊重 data_location.txt 迁移标记。"""
    root = REPO_ROOT
    override = root / "data_location.txt"
    if override.is_file():
        try:
            candidate = Path(override.read_text(encoding="utf-8").strip().strip('"'))
            if candidate.is_absolute() and candidate.is_dir():
                return candidate
        except OSError:
            pass
    return root


def _download(url: str, destination: Path, label: str) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": "afan-talking-video-agent/0.2"})
    with urllib.request.urlopen(request, timeout=120) as response, destination.open("wb") as output:
        total = int(response.headers.get("Content-Length") or 0)
        received = 0
        while True:
            chunk = response.read(CHUNK)
            if not chunk:
                break
            output.write(chunk)
            received += len(chunk)
            if total:
                percent = min(100, received * 100 // total)
                print(f"    下载 {label}：{received // (1024 * 1024)}MB / {total // (1024 * 1024)}MB（{percent}%）", end="\r")
    print()


def _download_from_github(target_dir: Path) -> bool:
    print("  国内镜像不可用，尝试 GitHub 发布包回退源…")
    try:
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "ffmpeg.zip"
            _download(GITHUB_ZIP, archive, "ffmpeg.zip")
            with zipfile.ZipFile(archive) as bundle:
                members = {Path(name).name: name for name in bundle.namelist()}
                for name in ("ffmpeg.exe", "ffprobe.exe"):
                    if name not in members:
                        print(f"  压缩包中缺少 {name}，回退失败。")
                        return False
                    with bundle.open(members[name]) as src, (target_dir / name).open("wb") as dst:
                        shutil.copyfileobj(src, dst)
        return True
    except (OSError, zipfile.BadZipFile) as error:
        print(f"  GitHub 回退下载失败：{error}")
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description="确保 FFmpeg 可用（缺失时自动下载）")
    parser.add_argument("--force", action="store_true", help="忽略现有 FFmpeg，强制重新下载到 tools 目录")
    args = parser.parse_args()

    target_dir = tools_ffmpeg_dir(_data_root())
    existing = resolve_ffmpeg(_data_root())
    if existing and not args.force:
        print(f"[OK] 已检测到 FFmpeg：{existing}")
        return 0

    if shutil.which("ffmpeg"):
        print("[OK] 系统 PATH 中已有 FFmpeg，无需下载。")
        return 0

    print("未找到 FFmpeg（视频剪辑与导出必需）。开始自动下载到：")
    print(f"  {target_dir}")
    target_dir.mkdir(parents=True, exist_ok=True)

    failed = False
    for name, url in NPMMIRROR_FILES.items():
        destination = target_dir / name
        try:
            _download(url, destination, name)
        except OSError as error:
            print(f"\n  {name} 下载失败：{error}")
            failed = True
            break
    if failed:
        target_dir.mkdir(parents=True, exist_ok=True)
        if not _download_from_github(target_dir):
            print("FFmpeg 自动下载失败。可手动下载 ffmpeg.exe / ffprobe.exe 放入：")
            print(f"  {target_dir}")
            return 1

    # 自检：能跑起来才算成功
    exe = target_dir / "ffmpeg.exe"
    if not exe.is_file():
        print("下载后未找到 ffmpeg.exe，请检查目录。")
        return 1
    try:
        os.system(f'"{exe}" -version >nul 2>&1')
    except OSError:
        pass
    print(f"[OK] FFmpeg 已就位：{exe}")
    print("     重新启动软件后，健康检查应显示 ffmpeg: true。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
