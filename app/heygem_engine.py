"""管理可选的本地 HeyGem Lite 引擎包（Docker 版）。

与 MuseTalk/Qwen3-TTS 的本地运行时不同，HeyGem Lite 官方以 Docker 镜像
分发：模型包内是一个 ``docker-compose-lite.yml`` 和一个
``HeyGem-Lite-image.tar``，服务固定监听本机 ``127.0.0.1:8383``（容器内
``/code/data`` 与宿主机共享目录交换素材）。

本模块只做三件事，与项目"不静默下载、不静默安装"的边界一致：

1. 只读检测：Docker CLI/守护进程、镜像、容器、8383 服务与模型包文件；
2. 一键接入（用户点击触发）：解压 .tar.zst → ``docker load`` →
   ``docker compose up -d`` → 等待服务就绪；
3. 把宿主机共享目录写入设置，供 ``HeyGemProvider`` 的素材暂存使用。

推理调用本身由 ``digital_human.HeyGemProvider`` 完成，这里不参与。
"""

from __future__ import annotations

import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Callable, Mapping


ENGINE_ID = "heygem-lite"
IMAGE_REF = "guiji2025/duix.avatar"
CONTAINER_NAME = "duix-avatar-gen-video"
DEFAULT_BASE_URL = "http://127.0.0.1:8383"
CONTAINER_MEDIA_DIR = "/code/data"
COMPOSE_FILE = "docker-compose-lite.yml"
IMAGE_TAR = "HeyGem-Lite-image.tar"
ARCHIVE_FILE = "HeyGem-Lite-模型包.tar.zst"
SERVICE_WAIT_SECONDS = 240
# Docker Desktop 官方稳定版直链（CDN，国内一般可直连）。下载与安装均由用户
# 在界面上主动点击触发；静默安装会代替用户接受 Docker 许可条款，因此按钮
# 文案必须注明这一点。
DOCKER_INSTALLER_URL = "https://desktop.docker.com/win/main/amd64/Docker%20Desktop%20Installer.exe"
DOCKER_INSTALLER_NAME = "Docker Desktop Installer.exe"
DOCKER_DESKTOP_EXE = r"C:\Program Files\Docker\Docker\Docker Desktop.exe"
MIN_WINDOWS_BUILD = 19041  # Windows 10 2004：WSL2 可用的最低版本
PREFLIGHT_TTL_SECONDS = 60

_STATE_LOCK = threading.Lock()
_STATE: dict[str, Any] = {
    "status": "idle",
    "progress": 0,
    "message": "尚未接入 HeyGem Lite。",
    "error": "",
}


def default_package_root(data_root: Path) -> Path:
    return data_root / "engines" / ENGINE_ID


def _set_state(**updates: Any) -> None:
    with _STATE_LOCK:
        _STATE.update(updates)


def _state() -> dict[str, Any]:
    with _STATE_LOCK:
        return dict(_STATE)


def _docker_cli() -> str:
    return shutil.which("docker") or ""


def _run_docker(args: list[str], *, timeout: float | None = 30) -> tuple[bool, str]:
    """运行一条 docker 命令；返回 (成功, 合并输出)。"""
    cli = _docker_cli()
    if not cli:
        return False, "未找到 docker 命令。"
    try:
        result = subprocess.run(
            [cli, *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
    output = (result.stdout or "") + (result.stderr or "")
    return result.returncode == 0, output.strip()


def _compose_host_media_dir(compose_path: Path) -> str:
    """从 compose 文件解析挂载到容器 /code/data 的宿主机目录。"""
    try:
        text = compose_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    match = re.search(r"-\s*([^:\s]+)\s*:\s*/code/data", text)
    return match.group(1).strip() if match else ""


def find_package_files(root: Path) -> dict[str, str]:
    """在目录（及其一级子目录）中定位模型包的三种形态。"""
    root = root.expanduser()
    found: dict[str, str] = {"compose": "", "image_tar": "", "archive": ""}
    if not root.is_dir():
        return found
    candidates = [root, *(p for p in sorted(root.iterdir()) if p.is_dir())]
    for candidate in candidates:
        try:
            compose = candidate / COMPOSE_FILE
            image = candidate / IMAGE_TAR
            archive = candidate / ARCHIVE_FILE
        except OSError:
            continue
        if not found["compose"] and compose.is_file():
            found["compose"] = str(compose)
        if not found["image_tar"] and image.is_file():
            found["image_tar"] = str(image)
        if not found["archive"] and archive.is_file():
            found["archive"] = str(archive)
    return found


def _service_ready(base_url: str) -> bool:
    """探测 8383 端口上是否已有 HTTP 服务在听（任意响应都算活着）。"""
    try:
        request = urllib.request.Request(base_url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(request, timeout=2.0):
            return True
    except urllib.error.HTTPError:
        # 404/405 等同样证明有 HTTP 服务在监听。
        return True
    except (OSError, ValueError):
        return False


def status(data_root: Path, settings: Mapping[str, str] | None = None) -> dict[str, Any]:
    """只读检测 HeyGem Lite 的接入状态，不启动任何进程。"""
    settings = settings or {}
    configured = str(settings.get("HEYGEM_PACKAGE_DIR") or "").strip()
    root = Path(configured).expanduser() if configured else default_package_root(data_root)
    base_url = str(settings.get("HEYGEM_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
    package = find_package_files(root)
    docker_ready, docker_output = (False, "")
    cli = _docker_cli()
    if cli:
        docker_ready, docker_output = _run_docker(["version", "--format", "{{.Server.Version}}"], timeout=12)
    image_ok, _ = _run_docker(["image", "inspect", IMAGE_REF], timeout=20)
    container_state = ""
    container_exists = False
    if cli:
        ok, output = _run_docker(
            ["ps", "-a", "--filter", f"name=^{CONTAINER_NAME}$", "--format", "{{.State}}"],
            timeout=20,
        )
        if ok and output:
            container_exists = True
            container_state = output.splitlines()[0].strip()
    gpu = ""
    nvidia = shutil.which("nvidia-smi")
    if nvidia:
        try:
            result = subprocess.run(
                [nvidia, "--query-gpu=name,memory.total", "--format=csv,noheader"],
                capture_output=True,
                text=True,
                timeout=8,
            )
            gpu = result.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            gpu = ""
    package_complete = bool(package["compose"] and package["image_tar"])
    service = _service_ready(base_url)
    running = container_state == "running" or service
    return {
        "id": ENGINE_ID,
        "root": str(root),
        "custom": bool(configured),
        "package": package,
        "package_complete": package_complete,
        "docker_cli": bool(cli),
        "docker_running": docker_ready,
        "docker_detail": "" if docker_ready else docker_output,
        "image": image_ok,
        "container": container_state or ("-" if container_exists else ""),
        "container_exists": container_exists,
        "service": service,
        "base_url": base_url,
        "media_root": str(settings.get("HEYGEM_MEDIA_ROOT") or CONTAINER_MEDIA_DIR),
        "host_media_root": str(settings.get("HEYGEM_HOST_MEDIA_ROOT") or ""),
        "gpu": gpu,
        "installed": running,
        "automatic_download": False,
        "env": preflight(data_root=data_root),
        "task": _state(),
    }


def _extract_archive(archive: Path, destination: Path) -> None:
    """用系统自带 tar.exe 解压 .tar.zst（Windows 10+ 原生支持 zstd）。"""
    tar_cli = shutil.which("tar")
    if not tar_cli:
        raise ValueError("系统缺少 tar.exe，请手动解压模型包后再点「接入」。")
    destination.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [tar_cli, "--zstd", "-xf", str(archive), "-C", str(destination)],
        capture_output=True,
        text=True,
        timeout=3600,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if result.returncode != 0:
        raise ValueError("解压模型包失败：" + ((result.stderr or result.stdout or "").strip() or "未知错误"))


def _wait_service(base_url: str, *, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _service_ready(base_url):
            return True
        time.sleep(2.0)
    return False


def _prepare_worker(
    root: Path,
    base_url: str,
    persist: Callable[[dict[str, str]], None] | None,
    explicit_path: Path | None,
) -> None:
    try:
        target = explicit_path or root
        files = find_package_files(target)
        step = 5
        # 1) 先做廉价的检查：包里至少要有可辨识的文件，Docker 必须就绪。
        #    没有 Docker 就失败，避免白解压几 GB 的模型包。
        if not (files["compose"] or files["image_tar"] or files["archive"]):
            raise ValueError(
                "没有在目录里找到 HeyGem Lite 模型包"
                f"（{COMPOSE_FILE} / {IMAGE_TAR} / {ARCHIVE_FILE} 任意一个）。当前目录：" + str(target)
            )
        if not _docker_cli():
            raise ValueError("未检测到 Docker。请先安装并启动 Docker Desktop（需启用 WSL 2 后端）。")
        _set_state(status="running", progress=step, message="正在检查 Docker 守护进程…", error="")
        docker_ready, docker_detail = _run_docker(["version", "--format", "{{.Server.Version}}"], timeout=15)
        if not docker_ready:
            raise ValueError("Docker 未在运行。请启动 Docker Desktop 后重试。" + (f"（{docker_detail}）" if docker_detail else ""))
        # 2) 只有 .tar.zst 时先解压出 compose + 镜像 tar。
        if not (files["compose"] and files["image_tar"]) and files["archive"]:
            _set_state(status="running", progress=step, message="正在解压 HeyGem Lite 模型包（约 5 GB，需要几分钟）…", error="")
            _extract_archive(Path(files["archive"]), Path(files["archive"]).parent)
            files = find_package_files(Path(files["archive"]).parent)
        if not files["compose"] or not files["image_tar"]:
            raise ValueError(
                "模型包不完整：需要 docker-compose-lite.yml 和 HeyGem-Lite-image.tar"
                "（或完整的 HeyGem-Lite-模型包.tar.zst）。当前目录：" + str(target)
            )
        compose_path = Path(files["compose"])
        host_media = _compose_host_media_dir(compose_path)
        # 3) 镜像缺失时导入（约 5 GB，耗时最长的一步）。
        #    发布包有个坑：镜像实际标签是 guiji2025/heygem.ai:latest，
        #    而 compose 要 guiji2025/duix.avatar；导入后必须补一个标签，
        #    否则 compose up 会去 Docker Hub 在线拉取并失败。
        image_ok, _ = _run_docker(["image", "inspect", IMAGE_REF], timeout=20)
        if not image_ok:
            _set_state(progress=25, message="正在导入 Docker 镜像（约 5 GB，可能需要 5-20 分钟，请勿关闭软件）…", error="")
            ok, output = _run_docker(["load", "-i", files["image_tar"]], timeout=None)
            if not ok:
                raise ValueError("docker load 失败：" + (output or "未知错误"))
        image_ok, _ = _run_docker(["image", "inspect", IMAGE_REF], timeout=20)
        if not image_ok:
            ok, source_image = _run_docker(
                ["images", "--format", "{{.Repository}}:{{.Tag}}"], timeout=30,
            )
            candidate = ""
            if ok:
                for line in source_image.splitlines():
                    if "heygem" in line.lower() or "duix" in line.lower():
                        candidate = line.strip()
                        break
            if not candidate:
                raise ValueError("镜像导入成功但找不到 heygem 镜像标签，请手动执行："
                                 f"docker tag <镜像名> {IMAGE_REF}")
            ok, output = _run_docker(["tag", candidate, IMAGE_REF], timeout=30)
            if not ok:
                raise ValueError(f"镜像标签修正失败（{candidate} → {IMAGE_REF}）：" + output)
        # 4) 启动容器（优先 compose；无 compose 插件时退回 docker start）。
        _set_state(progress=80, message="正在启动 HeyGem Lite 容器…", error="")
        ok, output = _run_docker(["compose", "-f", files["compose"], "up", "-d"], timeout=300)
        if not ok:
            started, start_output = _run_docker(["start", CONTAINER_NAME], timeout=120)
            if not started:
                raise ValueError("启动容器失败：" + (output or start_output or "未知错误"))
        # 5) 等待服务就绪并保存素材目录设置。
        _set_state(progress=90, message="服务已启动，等待模型就绪（最长约 4 分钟）…", error="")
        updates: dict[str, str] = {
            "HEYGEM_MEDIA_ROOT": CONTAINER_MEDIA_DIR,
        }
        if host_media:
            updates["HEYGEM_HOST_MEDIA_ROOT"] = host_media
        if persist is not None:
            try:
                persist(dict(updates))
            except Exception as exc:  # noqa: BLE001 - 设置保存失败不阻塞接入
                _set_state(error=f"设置保存失败：{exc}")
        if _wait_service(base_url, timeout=SERVICE_WAIT_SECONDS):
            _set_state(status="succeeded", progress=100, message="HeyGem Lite 已就绪，可在「模型分配」中选择 HeyGem。", error="")
        else:
            _set_state(
                status="succeeded",
                progress=100,
                message="容器已启动，但 8383 端口尚未响应（首次加载模型可能较慢）；稍后点「重新检测」。",
                error="",
            )
    except Exception as error:  # noqa: BLE001 - 转为用户可读的接入状态
        _set_state(status="failed", progress=0, message="HeyGem Lite 接入失败。", error=str(error))


def start_prepare(
    data_root: Path,
    settings: Mapping[str, str] | None = None,
    *,
    path: str | None = None,
    persist: Callable[[dict[str, str]], None] | None = None,
) -> dict[str, Any]:
    """后台执行"解压 → docker load → compose up"，用户通过 status 轮询进度。"""
    current = _state()
    if current.get("status") == "running":
        return current
    settings = settings or {}
    configured = str(settings.get("HEYGEM_PACKAGE_DIR") or "").strip()
    root = Path(configured).expanduser() if configured else default_package_root(data_root)
    base_url = str(settings.get("HEYGEM_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
    explicit = Path(path).expanduser().resolve() if path and Path(path).expanduser().is_dir() else None
    token = uuid.uuid4().hex
    _set_state(status="running", progress=1, message=f"正在准备 HeyGem Lite 接入（任务 {token[:8]}）…", error="")
    threading.Thread(target=_prepare_worker, args=(root.resolve(), base_url, persist, explicit), daemon=True).start()
    return _state()


# ── Docker 运行环境一键准备：预检 + 下载 + WSL2 + 静默安装 + 启动 ──
# 用户唯一需要交互的是 UAC 弹窗和可能的一次重启；其余步骤由任务自动完成。
_PREFLIGHT_LOCK = threading.Lock()
_PREFLIGHT_CACHE: dict[str, Any] = {"ts": 0.0, "value": None}


def _installer_path(data_root: Path) -> Path:
    return data_root / "tools" / "docker" / DOCKER_INSTALLER_NAME


def _powershell(script: str, *, timeout: float = 60) -> tuple[bool, str]:
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            capture_output=True,
            text=True,
            timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
    return result.returncode == 0, ((result.stdout or "") + (result.stderr or "")).strip()


def _run_elevated(exe: str, args: list[str], *, timeout: float | None = 1800) -> tuple[bool, str]:
    """弹 UAC 以管理员身份运行安装类命令；用户取消时返回可读提示。"""
    quoted = ",".join("'" + a.replace("'", "''") + "'" for a in args)
    script = (
        "$p = Start-Process -FilePath '" + exe.replace("'", "''") + "' "
        f"-ArgumentList {quoted} -Verb RunAs -PassThru -Wait; "
        "exit $p.ExitCode"
    )
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            capture_output=True,
            text=True,
            timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired:
        return False, "命令超时未完成。"
    except OSError as exc:
        return False, str(exc)
    if result.returncode != 0:
        text = ((result.stderr or "") + (result.stdout or "")).strip()
        lowered = text.lower()
        if "canceled by the user" in lowered or "用户取消" in text:
            return False, "已取消：你拒绝了管理员授权（UAC）弹窗。"
        return False, text or f"命令退出码 {result.returncode}"
    return True, (result.stdout or "").strip()


def preflight(*, data_root: Path | None = None, force: bool = False) -> dict[str, Any]:
    """检测一键安装 Docker 环境的前提条件，结果缓存 60 秒。"""
    with _PREFLIGHT_LOCK:
        now = time.monotonic()
        if (
            not force
            and _PREFLIGHT_CACHE["value"] is not None
            and now - _PREFLIGHT_CACHE["ts"] < PREFLIGHT_TTL_SECONDS
        ):
            return dict(_PREFLIGHT_CACHE["value"])

    build = getattr(sys, "getwindowsversion", None)
    windows_ok = bool(build and build().build >= MIN_WINDOWS_BUILD)
    hypervisor_ok, virtualization_ok = False, False
    ok, output = _powershell(
        "(Get-CimInstance Win32_ComputerSystem).HypervisorPresent; "
        "(Get-CimInstance Win32_Processor).VirtualizationFirmwareEnabled",
        timeout=30,
    )
    if ok:
        flags = [line.strip().lower() == "true" for line in output.splitlines() if line.strip()][:2]
        while len(flags) < 2:
            flags.append(False)
        hypervisor_ok, virtualization_ok = flags[0], flags[1]
    wsl_ok = False
    try:
        wsl_probe = subprocess.run(
            ["wsl", "--status"], capture_output=True, timeout=20,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        wsl_ok = wsl_probe.returncode == 0
    except (OSError, subprocess.SubprocessError):
        wsl_ok = False
    cli = _docker_cli()
    docker_ready = False
    if cli:
        docker_ready, _ = _run_docker(["version", "--format", "{{.Server.Version}}"], timeout=12)
    installer = _installer_path(Path(data_root)) if data_root else None
    disks: dict[str, float] = {}
    for label, path in (("data", data_root), ("system", Path(os.environ.get("SystemDrive", "C:") + "\\"))):
        if not path:
            continue
        try:
            disks[label] = round(shutil.disk_usage(str(path)).free / (1 << 30), 1)
        except OSError:
            disks[label] = -1
    value = {
        "windows_ok": windows_ok,
        "virtualization_ok": bool(hypervisor_ok or virtualization_ok),
        "wsl_ok": wsl_ok,
        "docker_cli": bool(cli),
        "docker_running": docker_ready,
        "installer_ready": bool(installer and installer.is_file()),
        "installer_path": str(installer or ""),
        "free_gb": disks,
        "ready": docker_ready,
        "blocked": (
            (not windows_ok and "windows")
            or (not (hypervisor_ok or virtualization_ok) and "virtualization")
            or ""
        ),
    }
    with _PREFLIGHT_LOCK:
        _PREFLIGHT_CACHE["ts"] = time.monotonic()
        _PREFLIGHT_CACHE["value"] = value
    return dict(value)


def _download_installer(destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(DOCKER_INSTALLER_URL, headers={"User-Agent": "afan-talking-head-agent/0.2"})
    last_report = 0.0
    with urllib.request.urlopen(request, timeout=120) as response, destination.open("wb") as output:
        total = int(response.headers.get("Content-Length") or 0)
        received = 0
        while chunk := response.read(1024 * 1024):
            output.write(chunk)
            received += len(chunk)
            now = time.monotonic()
            if now - last_report >= 2.0:
                last_report = now
                if total:
                    _set_state(progress=min(30, 5 + int(received * 25 / total)),
                               message=f"正在下载 Docker Desktop 安装包：{received // (1024 * 1024)}/{total // (1024 * 1024)} MB…")
                else:
                    _set_state(progress=15, message=f"正在下载 Docker Desktop 安装包：{received // (1024 * 1024)} MB…")


def _wait_docker_daemon(timeout: float = 300) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        ok, _ = _run_docker(["version", "--format", "{{.Server.Version}}"], timeout=12)
        if ok:
            return True
        time.sleep(3.0)
    return False


def _env_worker(data_root: Path) -> None:
    try:
        pre = preflight(data_root=data_root, force=True)
        if not pre["windows_ok"]:
            raise ValueError("需要 Windows 10 2004 或更高版本（含 Windows 11）才能使用 WSL2。")
        if not pre["virtualization_ok"]:
            raise ValueError(
                "这台电脑没有开启 CPU 虚拟化（VT-x/AMDV）。需要在 BIOS/UEFI 设置里开启后才能使用 WSL2；"
                "一般默认开启，如被关闭请参照主板说明书操作。"
            )
        installer = _installer_path(data_root)
        if pre["docker_cli"]:
            # Docker 已安装：跳过下载/启用/安装，只确保它在运行。
            _set_state(status="running", progress=85, message="检测到已安装 Docker，正在确保其运行…", error="")
        else:
            # 1) 下载安装包（已有则复用）。
            if not installer.is_file():
                _set_state(status="running", progress=5, message="正在下载 Docker Desktop 安装包（约 600 MB）…", error="")
                try:
                    _download_installer(installer)
                except (OSError, urllib.error.URLError) as exc:
                    raise ValueError(
                        "下载 Docker Desktop 安装包失败（"
                        + str(exc)
                        + "）。请检查网络，或从 https://www.docker.com/products/docker-desktop/ 手动下载后放到 "
                        + str(installer.parent)
                    ) from exc
            # 2) 启用 WSL2（需要管理员；可能要求重启）。
            if not pre["wsl_ok"]:
                _set_state(status="running", progress=32, message="正在启用 WSL2（请在弹窗中允许管理员权限）…", error="")
                ok, output = _run_elevated("wsl", ["--install", "--no-distribution"], timeout=900)
                if not ok:
                    raise ValueError("启用 WSL2 失败：" + output)
                if re.search(r"重新启动|重启|restart", output or "", re.IGNORECASE):
                    _set_state(status="reboot_required", progress=45,
                               message="WSL2 已启用，需要重启电脑。重启后重新打开软件，再次点击此按钮即可继续。",
                               error="")
                    return
            # 3) 静默安装 Docker Desktop（需要管理员；代替用户接受许可条款）。
            _set_state(status="running", progress=50, message="正在安装 Docker Desktop（请在弹窗中允许管理员权限，约需几分钟）…", error="")
            ok, output = _run_elevated(installer, ["install", "--quiet", "--accept-license"], timeout=1800)
            if not ok:
                if re.search(r"重新启动|重启|restart", output or "", re.IGNORECASE):
                    _set_state(status="reboot_required", progress=80,
                               message="安装基本完成，需要重启电脑。重启后重新打开软件，再次点击此按钮即可继续。",
                               error="")
                    return
                raise ValueError("Docker Desktop 安装失败：" + (output or "未知错误"))
        # 4) 启动 Docker Desktop 并等待守护进程。
        if not Path(DOCKER_DESKTOP_EXE).is_file():
            raise ValueError("安装后未找到 Docker Desktop，请确认安装是否成功：" + DOCKER_DESKTOP_EXE)
        _set_state(status="running", progress=85, message="正在启动 Docker Desktop（首次启动需要初始化）…", error="")
        subprocess.Popen(
            [DOCKER_DESKTOP_EXE],
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "DETACHED_PROCESS", 0),
        )
        if not _wait_docker_daemon(420):
            raise ValueError("Docker 已安装但守护进程未就绪。请手动打开 Docker Desktop 等其启动完成后重试。")
        _set_state(status="succeeded", progress=100,
                   message="Docker 环境就绪。请点击「导入镜像并启动服务」完成 HeyGem Lite 接入。",
                   error="")
    except Exception as error:  # noqa: BLE001 - 转为用户可读的环境准备状态
        _set_state(status="failed", progress=0, message="Docker 环境准备失败。", error=str(error))


def start_env_setup(data_root: Path) -> dict[str, Any]:
    """一键准备 Docker 运行环境；通过 status().task 轮询进度。"""
    current = _state()
    if current.get("status") == "running":
        return current
    _set_state(status="running", progress=1, message="正在检查本机环境…", error="")
    threading.Thread(target=_env_worker, args=(data_root.resolve(),), daemon=True).start()
    return _state()
