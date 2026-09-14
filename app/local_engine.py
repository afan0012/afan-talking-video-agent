"""管理可选的本地 MuseTalk 引擎包。

主程序本身不捆绑 GPU 模型和 PyTorch。这个模块只负责把一个经过发布者
准备的引擎压缩包下载/解压到用户指定目录，并检查关键文件是否齐全。
实际推理仍由 ``MuseTalkProvider`` 调用，因而下载器和推理适配器彼此独立。
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import sys
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


def bundled_bin_dir() -> Path | None:
    """安装包自带的 ffmpeg 目录（build_windows.py --ffmpeg 嵌入 _internal/bin）。

    源码运行没有这个目录，交由 tools/ffmpeg/bin、ensure_ffmpeg.py 或 PATH 兜底。
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent / "_internal" / "bin"
    return None


def resolve_ffmpeg(data_root: Path | None = None, settings: Mapping[str, str] | None = None) -> str:
    """按用户可维护的顺序解析 FFmpeg 可执行文件路径。

    优先级：设置项/环境变量 → 数据目录 tools\ffmpeg\bin（开箱即用的自动
    下载位置）→ 安装包自带 _internal/bin → PATH。全部落空返回空字符串，
    由调用方决定如何提示。
    """
    candidates: list[Path] = []
    configured = str((settings or {}).get("FFMPEG_PATH") or os.getenv("FFMPEG_PATH", "")).strip()
    if configured:
        candidate = Path(configured).expanduser()
        candidates.append(candidate / "ffmpeg.exe" if candidate.is_dir() else candidate)
    if data_root is not None:
        candidates.append(tools_ffmpeg_dir(data_root) / "ffmpeg.exe")
    bundled = bundled_bin_dir()
    if bundled is not None:
        candidates.append(bundled / "ffmpeg.exe")
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return shutil.which("ffmpeg") or ""


# ---------------------------------------------------------------------------
# 上游 MuseTalk inference.py 的 Windows 空格路径兼容补丁
# ---------------------------------------------------------------------------

_MUSE_COMPAT_MARKER = "ffmpeg_bin = args.ffmpeg_path"

_MUSE_COMPAT_PROBE_FPS = (
    "\n\n"
    "def probe_fps(video_path, ffprobe_bin):\n"
    "    probe = subprocess.run(\n"
    "        [ffprobe_bin, \"-v\", \"error\", \"-select_streams\", \"v:0\",\n"
    "         \"-show_entries\", \"stream=r_frame_rate\", \"-of\", \"default=nw=1:nk=1\", str(video_path)],\n"
    "        capture_output=True, text=True,\n"
    "    )\n"
    "    rate = probe.stdout.strip().splitlines()[0] if probe.stdout.strip() else \"\"\n"
    "    if \"/\" in rate:\n"
    "        num, den = rate.split(\"/\", 1)\n"
    "        if float(den):\n"
    "            return float(num) / float(den)\n"
    "    raise ValueError(f\"Unable to determine the FPS of video: {video_path}\")\n"
)

_MUSE_COMPAT_FFMPEG_BIN = (
    "\n    # afan-compat: paths may contain spaces; always invoke ffmpeg via argument lists.\n"
    "    ffmpeg_bin = args.ffmpeg_path if os.path.isfile(args.ffmpeg_path) else \"ffmpeg\"\n"
    "    ffprobe_bin = os.path.join(os.path.dirname(os.path.abspath(ffmpeg_bin)),\n"
    "                               \"ffprobe.exe\" if os.name == \"nt\" else \"ffprobe\")\n"
)

_MUSE_COMPAT_EXTRACT = (
    "extract = subprocess.run([ffmpeg_bin, \"-v\", \"fatal\", \"-i\", str(video_path),\n"
    "                                          \"-start_number\", \"0\", os.path.join(save_dir_full, \"%08d.png\")])\n"
    "                if extract.returncode != 0:\n"
    "                    raise RuntimeError(f\"ffmpeg frame extraction failed with exit code {extract.returncode}\")\n"
    "\\1input_img_list = sorted(glob.glob(os.path.join(save_dir_full, '*.[jpJP][pnPN]*[gG]')))\n"
    "                if not input_img_list:\n"
    "                    raise RuntimeError(\"No frames extracted from the source video\")\n"
    "                fps = get_video_fps(video_path)\n"
    "                if not fps or fps <= 0:\n"
    "                    fps = probe_fps(video_path, ffprobe_bin)"
)

_MUSE_COMPAT_SAVE = (
    "subprocess.run([ffmpeg_bin, \"-y\", \"-v\", \"warning\", \"-r\", str(fps), \"-f\", \"image2\",\n"
    "                            \"-i\", os.path.join(result_img_save_path, \"%08d.png\"),\n"
    "                            \"-vcodec\", \"libx264\", \"-vf\", \"format=yuv420p\", \"-crf\", \"18\", temp_vid_path],\n"
    "                           check=True)\n"
    "            subprocess.run([ffmpeg_bin, \"-y\", \"-v\", \"warning\", \"-i\", str(audio_path),\n"
    "                            \"-i\", temp_vid_path, output_vid_name], check=True)"
)


def apply_musetalk_windows_compat(inference_py: Path) -> str:
    """把上游 MuseTalk ``scripts/inference.py`` 打成 Windows 空格路径兼容版。

    上游用未加引号的 ``os.system`` 拼 ffmpeg 命令，任务目录含空格（例如
    ``%LOCALAPPDATA%\\afan Talking Video Agent\\runtime-jobs``）时抽帧会
    静默失败，空帧列表随后在 preprocessing 里触发除零，而任务级
    try/except 吞掉异常后进程仍以 0 退出，控制器只能报「没有找到输出
    MP4」。这里把三处 os.system 替换为 subprocess.run 列表调用，并让
    任务失败时以非零码退出，控制器从而能看到真实原因。

    幂等：已打过补丁返回 ``already``；识别不出上游代码（新版本/已修复）
    返回 ``skipped`` 且不改动文件；首次打补丁前写入 ``.afan-backup`` 备份。
    """
    try:
        raw = inference_py.read_bytes()
        text = raw.decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return "skipped"
    if _MUSE_COMPAT_MARKER in text:
        return "already"

    crlf = "\r\n" in text
    src = text.replace("\r\n", "\n")

    try:
        compile(src, str(inference_py), "exec")
        original_compiles = True
    except SyntaxError:
        original_compiles = False

    patched = src

    # 1) fast_check_ffmpeg 后追加 probe_fps（cv2 读不出 fps 时用 ffprobe 兜底）
    fast_check_tail = "    except:\n        return False\n"
    if patched.count(fast_check_tail) != 1:
        return "skipped"
    patched = patched.replace(fast_check_tail, fast_check_tail + _MUSE_COMPAT_PROBE_FPS)

    # 2) main() 里解析 ffmpeg/ffprobe 可执行路径
    warning_line = "            print(\"Warning: Unable to find ffmpeg, please ensure ffmpeg is properly installed\")\n"
    if patched.count(warning_line) != 1:
        return "skipped"
    patched = patched.replace(warning_line, warning_line + _MUSE_COMPAT_FFMPEG_BIN)

    # 3) 抽帧：os.system → subprocess.run，失败显式报错；空帧列表显式报错；
    #    fps 无效时用 ffprobe 兜底
    extract_pattern = re.compile(
        r"cmd = f\"ffmpeg -v fatal -i \{video_path\} -start_number 0 \{save_dir_full\}/%08d\.png\"\n"
        r"\s*os\.system\(cmd\)\n"
        r"(\s*)input_img_list = sorted\(glob\.glob\(os\.path\.join\(save_dir_full, '\*\.\[jpJP\]\[pnPN\]\*\[gG\]'\)\)\)\n"
        r"\s*fps = get_video_fps\(video_path\)"
    )
    if len(extract_pattern.findall(patched)) != 1:
        return "skipped"
    patched = extract_pattern.sub(_MUSE_COMPAT_EXTRACT, patched)

    # 4) 出片与合音频：os.system → subprocess.run(check=True)
    save_pattern = re.compile(
        r"cmd_img2video = f\"ffmpeg -y -v warning -r \{fps\} -f image2 -i \{result_img_save_path\}/%08d\.png"
        r" -vcodec libx264 -vf format=yuv420p -crf 18 \{temp_vid_path\}\"\n"
        r"\s*print\(\"Video generation command:\", cmd_img2video\)\s*\n"
        r"\s*os\.system\(cmd_img2video\)\s*\n"
        r"\s*cmd_combine_audio = f\"ffmpeg -y -v warning -i \{audio_path\} -i \{temp_vid_path\} \{output_vid_name\}\"\n"
        r"\s*print\(\"Audio combination command:\", cmd_combine_audio\)\s*\n"
        r"\s*os\.system\(cmd_combine_audio\)"
    )
    if len(save_pattern.findall(patched)) != 1:
        return "skipped"
    patched = save_pattern.sub(_MUSE_COMPAT_SAVE, patched)

    # 5) 任务失败置位 + 进程非零退出（控制器因此能看到真实错误）
    loop_head = "    # Process each task\n    for task_id in inference_config:\n"
    if patched.count(loop_head) != 1:
        return "skipped"
    patched = patched.replace(loop_head, "    # Process each task\n    task_failed = False\n    for task_id in inference_config:\n")

    except_tail = "        except Exception as e:\n            traceback.print_exc()\n            print(\"Error occurred during processing:\", e)\n"
    if patched.count(except_tail) != 1:
        return "skipped"
    patched = patched.replace(
        except_tail,
        except_tail + "            task_failed = True\n    if task_failed:\n        sys.exit(1)\n",
    )

    if original_compiles:
        try:
            compile(patched, str(inference_py), "exec")
        except SyntaxError:
            return "skipped"

    backup = inference_py.with_suffix(inference_py.suffix + ".afan-backup")
    if not backup.exists():
        backup.write_bytes(raw)
    out = patched.replace("\n", "\r\n") if crlf else patched
    inference_py.write_bytes(out.encode("utf-8"))
    return "patched"


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
    request = urllib.request.Request(url, headers={"User-Agent": "afan-talking-video-agent/0.2"})
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
