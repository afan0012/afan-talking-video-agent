from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import mimetypes
import os
import re
import queue
import secrets
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable

import dashscope
import http.server
import httpx
import tempfile
from dashscope import Generation, VideoSynthesis
from dashscope.audio.asr import Recognition, RecognitionCallback
from dashscope.audio.http_tts.http_speech_synthesizer import HttpSpeechSynthesizer
from dashscope.audio.qwen_tts_realtime.qwen_tts_realtime import (
    AudioFormat,
    QwenTtsRealtime,
    QwenTtsRealtimeCallback,
)
from dashscope.utils.oss_utils import check_and_upload_local
from fastapi import BackgroundTasks, Body, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

# ── 控制台子进程防崩（Windows）───────────────────────────────
# 双击启动的打包版是无控制台的 GUI 进程，标准句柄无效；此时派生
# ffmpeg/ffprobe 等控制台工具会偶发 STATUS_DLL_INIT_FAILED(0xC0000142)。
# CREATE_NO_WINDOW 让每个子进程获得一套全新的隐藏控制台句柄，根治此问题。
if os.name == "nt":
    _CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

    def _patch_spawn(kwargs: dict[str, Any]) -> dict[str, Any]:
        kwargs["creationflags"] = kwargs.get("creationflags", 0) | _CREATE_NO_WINDOW
        return kwargs

    _orig_subprocess_run = subprocess.run

    def _subprocess_run(*args: Any, **kwargs: Any) -> Any:
        return _orig_subprocess_run(*args, **_patch_spawn(kwargs))

    subprocess.run = _subprocess_run  # type: ignore[assignment]

    _orig_subprocess_popen = subprocess.Popen

    def _subprocess_popen(*args: Any, **kwargs: Any) -> Any:
        return _orig_subprocess_popen(*args, **_patch_spawn(kwargs))

    subprocess.Popen = _subprocess_popen  # type: ignore[assignment]
from app.digital_human import provider_for, provider_infos
from app.domain import BrollClip, Job, now
from app.funasr_local import FunASRAdapter
from app.job_store import JobStore as _JobStore
from app.local_engine import (
    ENGINE_ID,
    default_engine_root,
    resolve_ffmpeg,
    start_install,
    status as local_engine_status,
)
from app.local_engine_registry import analyze_model_bundle, funasr_engine_status, ollama_engine_status
from app.heygem_engine import preflight as heygem_preflight, status as heygem_lite_status, start_env_setup as start_heygem_env_setup, start_prepare as start_heygem_prepare
from app.local_runtime import (
    qwen_engine_status,
    status as local_runtime_status,
    start_funasr as start_local_funasr,
    start_musetalk as start_local_musetalk,
    start_qwen3_tts as start_local_qwen3_tts,
)
from app.musetalk_downloader import download_status as musetalk_download_status, start_download as start_musetalk_download
from app.qwen_tts_local import LocalQwenTTS
from app.storage import read_json, write_json_atomic
from app.workflow_contract import WORKFLOW as _WORKFLOW_CONTRACT
from app.service_connections import (
    SERVICE_CAPABILITIES,
    SUPPORTED_SERVICE_ADAPTERS,
    clean_service_models as _clean_service_models_impl,
    clean_service_url as _clean_service_url_impl,
    custom_provider as _custom_provider_impl,
    discover_service_models as _discover_service_models_impl,
    load_custom_providers as _load_custom_providers_impl,
    load_local_ollama as _load_local_ollama_impl,
    load_model_routes as _load_model_routes_impl,
    load_service_connections as _load_service_connections_impl,
    normalize_local_ollama_url as _normalize_local_ollama_url_impl,
    ollama_provider as _ollama_provider_impl,
    save_custom_providers as _save_custom_providers_impl,
    save_local_ollama as _save_local_ollama_impl,
    save_model_routes as _save_model_routes_impl,
    save_service_connections as _save_service_connections_impl,
    service_connection_for_model as _service_connection_for_model_impl,
    service_model_options as _service_model_options_impl,
    service_model_value as _service_model_value_impl,
    service_models_url as _service_models_url_impl,
)
from app.model_registry import (
    ASR_MODELS,
    CLONE_MODEL_ALIASES,
    DIRECT_TTS_MODELS,
    DIRECT_TTS_VOICES,
    LIPSYNC_MODELS,
    MODEL_ROUTE_OPTIONS,
    REMOVED_CLONE_MODELS,
    REWRITE_MODELS,
    VOICE_CLONE_MODELS,
    cosyvoice_instruction,
    is_generic_text_model,
    normalize_clone_model,
)


SOURCE_ROOT = Path(__file__).resolve().parents[1]


def _default_data_root() -> Path:
    """Installed default data root (%LOCALAPPDATA%\\afan Talking Video Agent); development uses the project directory."""
    if getattr(sys, "frozen", False):
        local_app_data = Path(os.getenv("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        return local_app_data / "afan Talking Video Agent"
    return SOURCE_ROOT


def _dir_usage(root: Path) -> int:
    """数据目录已用空间（字节），失败时返回 0。"""
    total = 0
    try:
        for path in root.rglob("*"):
            if path.is_file():
                try:
                    total += path.stat().st_size
                except OSError:
                    pass
    except OSError:
        return 0
    return total


# PyInstaller unpacks bundled resources to ``_MEIPASS``.  User-created files
# must *not* live there: the directory is replaced on upgrade and may be
# read-only under Program Files.
RESOURCE_ROOT = Path(getattr(sys, "_MEIPASS", SOURCE_ROOT))
if getattr(sys, "frozen", False):
    local_app_data = Path(os.getenv("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    default_root = local_app_data / "afan Talking Video Agent"
else:
    default_root = SOURCE_ROOT

# 数据目录可由用户自定义：
# 1. 环境变量 AFAN_DATA_DIR（安装器/高级用户用）；
# 2. 默认位置下的 data_location.txt 文件（网页设置里选择后写入，
#    重启后生效——路径解析发生在所有路由之前，无法热切换）。
USER_DATA_ROOT = default_root
_override_file = default_root / "data_location.txt"
try:
    if _override_file.is_file():
        _custom = Path(_override_file.read_text(encoding="utf-8").strip().strip('"'))
        if _custom.is_absolute() and _custom != default_root:
            USER_DATA_ROOT = _custom
except OSError:
    pass

# 发行版（PyInstaller 打包后 sys.frozen 为真）移除境外服务等仅限本地自用的选项。
PUBLIC_BUILD = bool(getattr(sys, "frozen", False))

ROOT = USER_DATA_ROOT  # Backwards-compatible name for local configuration paths.
DATA_DIR = USER_DATA_ROOT / "data"
JOBS_DIR = DATA_DIR / "jobs"
DB_PATH = DATA_DIR / "jobs.json"
LIBRARY_DIR = DATA_DIR / "library"
LIBRARY_DB_PATH = DATA_DIR / "library.json"
STATIC_DIR = RESOURCE_ROOT / "app" / "static"
CONFIG_PATH = USER_DATA_ROOT / ".env"
for directory in (USER_DATA_ROOT, DATA_DIR, JOBS_DIR, LIBRARY_DIR):
    directory.mkdir(parents=True, exist_ok=True)

# Backwards-compatible constructor for tests and extensions that historically
# imported ``main.JobStore()``.  The implementation remains in ``app.job_store``
# while the default follows this module's current data-root configuration.
class JobStore(_JobStore):
    def __init__(self, db_path: Path | None = None) -> None:
        super().__init__(db_path or DB_PATH)

# The API layer receives a ready-to-use store; persistence and legacy job
# migration live in ``app.job_store`` rather than in this module.
store = JobStore(DB_PATH)


_LOCAL_SETTINGS: dict[str, str] = {}


def load_dotenv(path: Path) -> None:
    """Load this application's local settings file; never inspect OS env vars."""
    _LOCAL_SETTINGS.clear()
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        name = name.strip()
        value = value.strip().strip('"').strip("'")
        if name:
            _LOCAL_SETTINGS[name] = value


load_dotenv(CONFIG_PATH)

MAX_VIDEO_SECONDS = 120
VIDEO_LOCK = threading.Lock()
MIMO_API_URL = "https://api.xiaomimimo.com/v1/chat/completions"
DEFAULT_REWRITE_INSTRUCTION = "改写为自然、通顺、可直接朗读的原创口播表达。"


def safe_log(message: str) -> None:
    """Best-effort diagnostic output that cannot break a background job.

    Windows apps launched without an attached console can raise ``OSError(22)``
    on ``print(..., flush=True)``.  Logging is optional; a media job is not.
    """
    try:
        print(message, flush=True)
    except OSError:
        pass


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


LIBRARY_KINDS = {"voice": "音色", "person": "人物形象", "script": "文案", "video": "视频"}
LIBRARY_AUDIO_SUFFIXES = {".aac", ".flac", ".m4a", ".mp3", ".mpeg", ".oga", ".ogg", ".opus", ".wav", ".webm"}
LIBRARY_VIDEO_SUFFIXES = {".mp4", ".mov", ".m4v", ".webm", ".avi", ".mkv"}


def _read_library() -> list[dict[str, Any]]:
    data = read_json(LIBRARY_DB_PATH, [])
    return [item for item in data if isinstance(item, dict) and item.get("id") and item.get("kind") in LIBRARY_KINDS] if isinstance(data, list) else []


def _write_library(items: list[dict[str, Any]]) -> None:
    write_json_atomic(LIBRARY_DB_PATH, items)


def _library_public(item: dict[str, Any]) -> dict[str, Any]:
    result = dict(item)
    if result.get("filename"):
        result["url"] = f"/api/library/{result['id']}/file"
    return result


def _library_item(asset_id: str) -> dict[str, Any]:
    item = next((entry for entry in _read_library() if entry.get("id") == asset_id), None)
    if not item:
        raise HTTPException(404, "素材库中没有找到这个素材。")
    return item


def local_setting(name: str) -> str | None:
    """Return a value explicitly saved through this application's Settings page."""
    return _LOCAL_SETTINGS.get(name)


def _runtime_script(name: str) -> Path:
    """Locate a bundled adapter script in both source and PyInstaller builds."""
    candidate = RESOURCE_ROOT / "scripts" / name
    return candidate if candidate.is_file() else SOURCE_ROOT / "scripts" / name


def _is_loopback_url(url: str) -> bool:
    """判断是否为本机/回环地址：这类请求不应交给系统代理转发。"""
    try:
        return (urllib.parse.urlparse(url).hostname or "").lower() in {"localhost", "127.0.0.1", "::1"}
    except ValueError:
        return False


def _remote_alive(url: str) -> bool:
    """探测已配置的远程引擎地址是否真的有服务在听。"""
    try:
        response = httpx.get(url.rstrip("/") + "/health", timeout=2.0, trust_env=not _is_loopback_url(url))
        return 200 <= response.status_code < 300
    except Exception:
        return False


def ensure_local_musetalk_runtime() -> str:
    """Start the installed loopback adapter and expose it to this process only."""
    existing = (local_setting("MUSETALK_BASE_URL") or "").strip()
    if existing:
        # Explicit developer configuration remains supported — but only while
        # it is actually alive; a stale URL must not block the local lifecycle.
        if _remote_alive(existing):
            return existing.rstrip("/")
    configured_root = (local_setting("MUSETALK_ENGINE_ROOT") or "").strip()
    root = Path(configured_root) if configured_root else default_engine_root(USER_DATA_ROOT)
    result = start_local_musetalk(USER_DATA_ROOT, _runtime_script("musetalk_remote_server.py"), engine_root=root, settings=_LOCAL_SETTINGS)
    if result.get("status") != "running" or not result.get("url"):
        raise RuntimeError(str(result.get("error") or "MuseTalk 本地引擎无法启动。"))
    _LOCAL_SETTINGS["MUSETALK_BASE_URL"] = str(result["url"])
    return str(result["url"])


def ensure_local_qwen3_tts_runtime() -> str:
    """Start Qwen3-TTS after its local engine has been installed."""
    existing = (local_setting("QWEN_TTS_BASE_URL") or "").strip()
    if existing:
        # 同 MuseTalk：过期/连不通的覆盖地址不应阻塞本机生命周期。
        if _remote_alive(existing):
            return existing.rstrip("/")
    result = start_local_qwen3_tts(USER_DATA_ROOT, _runtime_script("qwen_tts_server.py"), settings=_LOCAL_SETTINGS)
    if result.get("status") != "running" or not result.get("url"):
        raise RuntimeError(str(result.get("error") or "Qwen3-TTS 本地引擎无法启动。"))
    _LOCAL_SETTINGS["QWEN_TTS_BASE_URL"] = str(result["url"])
    return str(result["url"])


def local_musetalk_engine_status() -> dict[str, Any]:
    result = local_engine_status(USER_DATA_ROOT, _LOCAL_SETTINGS)
    result["runtime"] = local_runtime_status("musetalk")
    # The ordinary UI uses the fixed, reviewed manifest; it never asks users
    # to paste an arbitrary model URL.
    result["automatic_install_available"] = True
    result["download"] = musetalk_download_status()
    return result


def require_api_key() -> str:
    key = local_setting("DASHSCOPE_API_KEY")
    if not key:
        raise RuntimeError("未填写阿里云百炼 API Key。请在“设置”中填写后重试。")
    dashscope.api_key = key
    dashscope.base_http_api_url = "https://dashscope.aliyuncs.com/api/v1"
    return key


def require_mimo_api_key() -> str:
    key = local_setting("MIMO_API_KEY")
    if not key:
        raise RuntimeError("未填写 MiMo API Key。请在“设置”中填写后重试。")
    return key


def mimo_post(payload: dict[str, Any], timeout: float) -> httpx.Response:
    """Call MiMo with retries for transient TLS/proxy connection failures."""
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            return httpx.post(
                MIMO_API_URL,
                headers={"Authorization": f"Bearer {require_mimo_api_key()}", "Content-Type": "application/json"},
                json=payload,
                timeout=timeout,
            )
        except (httpx.HTTPError, OSError) as error:
            last_error = error
            if attempt < 2:
                time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"MiMo 网络连接失败，已自动重试 3 次：{last_error}") from last_error


def openai_compat_chat(
    *, base_url: str, api_key: str, model: str, messages: list[dict[str, Any]],
    timeout: float = 120, temperature: float = 0.65, max_tokens: int = 1600,
    extra_body: dict[str, Any] | None = None,
) -> httpx.Response:
    """Call any OpenAI-compatible chat completions endpoint with retries.

    兼容 DeepSeek / MiMo / 通义 / 各类中转站 / 自建网关。base_url 支持
    ``https://host/v1`` 或 ``https://host/v1/chat/completions`` 两种填法。
    ``extra_body`` 里的字段（如 ``thinking``）会合并进请求体，不支持的
    端点通常会忽略它们。
    """
    url = (base_url or "").strip().rstrip("/")
    if not url:
        raise RuntimeError("未配置接口地址，请检查自定义供应商设置。")
    if not url.endswith("/chat/completions"):
        url = url + "/chat/completions" if url.endswith(("/v1", "/v1/chat")) else url + "/v1/chat/completions"
    body: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if extra_body:
        body.update(extra_body)
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            return httpx.post(
                url,
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json=body,
                timeout=timeout,
                trust_env=not _is_loopback_url(url),
            )
        except (httpx.HTTPError, OSError) as error:
            last_error = error
            if attempt < 2:
                time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"通用接口网络连接失败，已自动重试 3 次：{last_error}") from last_error


def _openai_audio_url(base_url: str, endpoint: str) -> str:
    url = base_url.rstrip("/")
    if url.endswith(endpoint):
        return url
    if url.endswith("/v1"):
        return url + endpoint
    return url + "/v1" + endpoint


def transcribe_with_openai_compat(audio: Path, provider: dict[str, Any], model: str) -> str:
    """Call the documented OpenAI-style transcription wire format."""
    url = _openai_audio_url(provider["base_url"], "/audio/transcriptions")
    headers = {"Authorization": f"Bearer {provider.get('api_key', '')}"} if provider.get("api_key") else {}
    try:
        with audio.open("rb") as source:
            response = httpx.post(
                url, headers=headers, data={"model": model, "response_format": "json"},
                files={"file": (audio.name, source, "audio/wav")}, timeout=httpx.Timeout(connect=30, read=180, write=180, pool=30),
            )
    except (httpx.HTTPError, OSError) as error:
        raise RuntimeError(f"{provider['name']} 的语音识别连接失败：{error}") from error
    if not response.is_success:
        raise RuntimeError(f"{provider['name']} 语音识别返回 HTTP {response.status_code}：{response.text[:300]}")
    try:
        text = str(response.json().get("text") or "").strip()
    except (ValueError, AttributeError) as error:
        raise RuntimeError(f"{provider['name']} 的语音识别响应格式不正确。") from error
    if not text:
        raise RuntimeError(f"{provider['name']} 未返回识别文本。")
    return text


def synthesize_with_openai_compat(text: str, target: Path, provider: dict[str, Any], model: str) -> None:
    """Call the documented OpenAI-style speech endpoint and save its audio."""
    url = _openai_audio_url(provider["base_url"], "/audio/speech")
    headers = {"Content-Type": "application/json"}
    if provider.get("api_key"):
        headers["Authorization"] = f"Bearer {provider['api_key']}"
    payload = {"model": model, "input": text, "voice": "alloy", "response_format": "wav"}
    try:
        response = httpx.post(url, headers=headers, json=payload, timeout=httpx.Timeout(connect=30, read=180, write=180, pool=30))
    except (httpx.HTTPError, OSError) as error:
        raise RuntimeError(f"{provider['name']} 的配音连接失败：{error}") from error
    if not response.is_success:
        raise RuntimeError(f"{provider['name']} 配音返回 HTTP {response.status_code}：{response.text[:300]}")
    if not response.content:
        raise RuntimeError(f"{provider['name']} 未返回配音音频。")
    target.write_bytes(response.content)


def _extract_chat_content(response: httpx.Response, provider: str) -> str:
    """从 OpenAI 兼容响应中取出正文；失败给出含状态码的明确报错。"""
    if not response.is_success:
        raise RuntimeError(f"{provider} 返回错误：HTTP {response.status_code} {response.text[:300]}")
    result = response.json()
    try:
        message = result["choices"][0]["message"]
        finish_reason = result["choices"][0].get("finish_reason")
        content = message["content"]
    except (KeyError, IndexError, TypeError) as error:
        raise RuntimeError(f"{provider} 未返回可用结果：{str(result)[:400]}") from error
    # 思考型模型（如 deepseek-v4-flash-0731）会把 token 预算耗在
    # reasoning_content 上，正文可能被 finish_reason=length 静默截断，
    # 甚至 reasoning 用尽预算导致正文为空。截断内容不完整，必须显式失败，
    # 由上层重试逻辑兜底，而不是把半截文案交给用户。
    if finish_reason == "length":
        raise RuntimeError(
            f"{provider} 的输出因达到 max_tokens 上限被截断（思考过程占用了 token 预算）。"
            "请重试；若反复出现，请在提示词中要求更精简的输出。"
        )
    text = str(content).strip()
    if not text:
        raise RuntimeError(f"{provider} 返回内容为空。")
    return text


CUSTOM_PROVIDERS_PATH = DATA_DIR / "custom_providers.json"
LOCAL_OLLAMA_PATH = DATA_DIR / "local_ollama.json"
LOCAL_FUNASR_PATH = DATA_DIR / "local_funasr.json"
SERVICE_CONNECTIONS_PATH = DATA_DIR / "service_connections.json"

MODEL_ROUTES_PATH = DATA_DIR / "model_routes.json"


def _clean_service_url(value: str) -> str:
    return _clean_service_url_impl(value)


def _clean_service_models(value: Any) -> list[str]:
    return _clean_service_models_impl(value)


def _discover_service_models(base_url: str, api_key: str) -> list[str]:
    return _discover_service_models_impl(base_url, api_key)


def _service_models_url(base_url: str) -> str:
    return _service_models_url_impl(base_url)


def _load_service_connections() -> list[dict[str, Any]]:
    return _load_service_connections_impl(SERVICE_CONNECTIONS_PATH)


def _save_service_connections(items: list[dict[str, Any]]) -> None:
    _save_service_connections_impl(SERVICE_CONNECTIONS_PATH, items)


def _service_model_value(provider_id: str, capability: str, model: str) -> str:
    return _service_model_value_impl(provider_id, capability, model)


def _service_connection_for_model(model: str, capability: str) -> tuple[dict[str, Any], dict[str, Any], str] | None:
    return _service_connection_for_model_impl(model, capability, SERVICE_CONNECTIONS_PATH)


def _service_model_options(step: str) -> list[dict[str, str]]:
    return _service_model_options_impl(step, SERVICE_CONNECTIONS_PATH)


def _load_custom_providers() -> list[dict[str, Any]]:
    return _load_custom_providers_impl(CUSTOM_PROVIDERS_PATH)


def _save_custom_providers(items: list[dict[str, Any]]) -> None:
    _save_custom_providers_impl(CUSTOM_PROVIDERS_PATH, items)


def _normalize_local_ollama_url(value: str) -> str:
    return _normalize_local_ollama_url_impl(value)


def _load_local_ollama() -> dict[str, str] | None:
    return _load_local_ollama_impl(LOCAL_OLLAMA_PATH)


def _save_local_ollama(config: dict[str, str]) -> None:
    _save_local_ollama_impl(LOCAL_OLLAMA_PATH, config)


def _ollama_provider(model: str) -> dict[str, str] | None:
    return _ollama_provider_impl(model, LOCAL_OLLAMA_PATH)


def _custom_provider(model: str) -> dict[str, Any] | None:
    return _custom_provider_impl(model, CUSTOM_PROVIDERS_PATH)


def _has_settings(*names: str) -> bool:
    return all(bool((local_setting(name) or "").strip()) for name in names)


def funasr_available() -> bool:
    """Whether local FunASR can run: a live adapter service or a package runtime."""
    engine = local_funasr_engine_status()
    return bool(engine.get("runtime_installed"))


def local_funasr_engine_status() -> dict[str, Any]:
    """Return the no-download readiness state shown by Local AI Engines."""
    return funasr_engine_status(USER_DATA_ROOT, _LOCAL_SETTINGS)


def funasr_ready(model_id: str | None = None) -> bool:
    """Whether an installed local runtime has the requested local weights.

    Merely importing FunASR is not enough: selecting a route must never cause
    an unannounced first-run model download.
    """
    engine = local_funasr_engine_status()
    if not engine["runtime_installed"]:
        return False
    if not model_id:
        return any(item["installed"] for item in engine["models"])
    return any(item["id"] == model_id and item["installed"] for item in engine["models"])


def local_ollama_engine_status() -> dict[str, Any]:
    """Discover only the user's normal loopback Ollama process."""
    return ollama_engine_status(_load_local_ollama() or {})


def _funasr_adapter() -> FunASRAdapter:
    engine = local_funasr_engine_status()
    return FunASRAdapter(LOCAL_FUNASR_PATH, _LOCAL_SETTINGS, engine_root=Path(engine["root"]))


def ensure_local_funasr_runtime() -> str:
    """Start the loopback FunASR adapter inside the engine package's runtime."""
    existing = (local_setting("FUNASR_BASE_URL") or "").strip()
    if existing and _remote_alive(existing):
        return existing.rstrip("/")
    engine = local_funasr_engine_status()
    root = Path(engine["root"])
    result = start_local_funasr(USER_DATA_ROOT, _runtime_script("funasr_server.py"), engine_root=root, settings=_LOCAL_SETTINGS)
    if result.get("status") != "running" or not result.get("url"):
        raise RuntimeError(str(result.get("error") or "FunASR 本地服务无法启动。"))
    _LOCAL_SETTINGS["FUNASR_BASE_URL"] = str(result["url"])
    return str(result["url"])


def transcribe_with_funasr(audio: Path, model: str) -> str:
    ensure_local_funasr_runtime()
    return _funasr_adapter().transcribe(audio, model)


def transcribe_timeline_with_funasr(audio: Path, model: str) -> tuple[str, list[dict[str, Any]]]:
    ensure_local_funasr_runtime()
    return _funasr_adapter().transcribe_timeline(audio, model)


def _is_builtin_model_available(step: str, model: str) -> bool:
    """Return true only when this built-in route has both code and credentials.

    The settings page is an assignment surface, not a catalogue.  Keeping this
    rule beside route validation prevents an unavailable choice from slipping
    back in through an old saved route or a direct API request.
    """
    dashscope_ready = _has_settings("DASHSCOPE_API_KEY", "DASHSCOPE_WORKSPACE_ID")
    mimo_ready = _has_settings("MIMO_API_KEY")
    if model == "auto":
        if step in {"script", "rewrite", "edit_plan"}:
            return mimo_ready or dashscope_ready
        if step == "asr":
            return mimo_ready or dashscope_ready or funasr_ready()
        if step == "subtitle_asr":
            return dashscope_ready or funasr_ready()
        return dashscope_ready
    if model.startswith("local:funasr-"):
        return funasr_ready(model)
    if model.startswith("mimo-"):
        return mimo_ready
    if model in {"qwen3.7-flash", "paraformer-realtime-v2", "qwen-audio-3.0-asr-flash-filetrans", "qwen-builtin-tts", "videoretalk", "qwen-voice", "cosyvoice-v3.5-plus", "qwen3-tts-vc"}:
        return dashscope_ready
    if model in {"qwen3-tts-local-base", "qwen3-tts-local-customvoice"}:
        # A normal installation does not expose a URL.  The loopback adapter
        # starts automatically when an installed local Qwen engine is used.
        return _has_settings("QWEN_TTS_BASE_URL") or bool(qwen_engine_status(USER_DATA_ROOT, _LOCAL_SETTINGS).get("installed"))
    if step == "lipsync" and model in {"musetalk-1.5", "heygem"}:
        # Local runtimes are intentionally selectable before installation; the
        # adapter reports the precise missing configuration when invoked.
        return True
    return False


def _is_model_option(step: str, model: str) -> bool:
    if step not in MODEL_ROUTE_OPTIONS:
        return False
    if model in MODEL_ROUTE_OPTIONS[step]:
        return _is_builtin_model_available(step, model)
    if step in {"script", "rewrite", "edit_plan"} and (_custom_provider(model) is not None or _ollama_provider(model) is not None):
        return True
    capability = {"script": "chat", "rewrite": "chat", "edit_plan": "chat", "asr": "asr", "direct_tts": "tts"}.get(step)
    return bool(capability and _service_connection_for_model(model, capability))


def _load_model_routes() -> dict[str, str]:
    routes = {
        "script": "auto",
        "rewrite": "auto",
        "asr": "auto",
        "subtitle_asr": "auto",
        "voice_clone": "mimo-v2.5-tts-voiceclone",
        "direct_tts": "mimo-v2.5-tts",
        "lipsync": "videoretalk",
        "edit_plan": "auto",
    }
    return _load_model_routes_impl(MODEL_ROUTES_PATH, routes, _is_model_option)


def _save_model_routes(routes: dict[str, str]) -> None:
    _save_model_routes_impl(MODEL_ROUTES_PATH, routes)


def selected_model(step: str, requested: str | None = None) -> str:
    model = (requested or "").strip() or _load_model_routes()[step]
    if not _is_model_option(step, model):
        raise HTTPException(400, "所选模型不可用，请在设置中重新选择。")
    return model


def model_label(model: str) -> str:
    """Safe label for progress updates; never exposes an API key."""
    service = None
    for capability in ("chat", "asr", "tts"):
        service = _service_connection_for_model(model, capability)
        if service:
            provider, _, selected = service
            return f"{provider['name']} · {selected}"
    return (REWRITE_MODELS.get(model) or ASR_MODELS.get(model) or VOICE_CLONE_MODELS.get(model)
            or DIRECT_TTS_MODELS.get(model) or LIPSYNC_MODELS.get(model) or model)


def selected_text_model(step: str, requested: str | None = None) -> str:
    """Compatibility wrapper for the two text-generation entry points."""
    return selected_model(step, requested)


def bailian_request(method: str, url: str, *, headers: dict[str, str], payload: dict[str, Any] | None = None) -> httpx.Response:
    """Retry only transport failures; API error responses remain visible to the caller."""
    last_error: Exception | None = None
    timeout = httpx.Timeout(connect=30, read=90, write=90, pool=30)
    for attempt in range(3):
        try:
            with httpx.Client(timeout=timeout, follow_redirects=True) as client:
                return client.request(method, url, headers=headers, json=payload)
        except (httpx.HTTPError, OSError) as error:
            last_error = error
            if attempt < 2:
                time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"百炼网络连接失败，已自动重试 3 次：{last_error}") from last_error


def require_workspace_id() -> str:
    workspace_id = local_setting("DASHSCOPE_WORKSPACE_ID")
    if not workspace_id:
        raise RuntimeError(
            "缺少 DASHSCOPE_WORKSPACE_ID。请在百炼北京地域的业务空间详情页复制业务空间 ID，"
            "并在“设置”中填写。"
        )
    return workspace_id


def ffmpeg_path() -> str:
    bundled = RESOURCE_ROOT / "bin" / "ffmpeg.exe"
    configured = (local_setting("FFMPEG_PATH") or os.getenv("FFMPEG_PATH") or "").strip()
    path = (
        configured
        or (str(bundled) if bundled.is_file() else None)
        or (resolve_ffmpeg(USER_DATA_ROOT) or None)
        or shutil.which("ffmpeg")
    )
    if not path:
        raise RuntimeError(
            "未找到 FFmpeg。请双击「一键启动.bat」自动下载到 tools\\ffmpeg，"
            "或在设置中填写 FFMPEG_PATH。"
        )
    return path


# 中文字体（Windows 黑体）。drawtext 依赖 fontconfig 查字体，Gyan.dev 静态构建
# 版缺 fonts.conf 会导致崩溃，必须用 fontfile 绝对路径绕过；subtitles/libass 用
# fontsdir + Fontname 指定。若字体缺失可用 msyh.ttc（微软雅黑）等替代。
def _detect_font() -> tuple[str, str, str]:
    candidates = [
        (r"C:\Windows\Fonts\simhei.ttf", r"C:\Windows\Fonts", "SimHei"),
        (r"C:\Windows\Fonts\msyh.ttc", r"C:\Windows\Fonts", "Microsoft YaHei"),
        ("/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc", "/usr/share/fonts", "WenQuanYi Zen Hei"),
        ("/System/Library/Fonts/PingFang.ttc", "/System/Library/Fonts", "PingFang SC"),
    ]
    for file_path, fonts_dir, font_name in candidates:
        if Path(file_path).is_file():
            escaped = file_path.replace("\\", "/").replace(":", "\\:")
            escaped_dir = fonts_dir.replace("\\", "/").replace(":", "\\:")
            return escaped, escaped_dir, font_name
    return "C\\:/Windows/Fonts/simhei.ttf", "C\\:/Windows/Fonts", "SimHei"


_FONT_FILE, _FONTS_DIR, _FONT_NAME = _detect_font()


def hex_to_ass(color: str) -> str:
    """Convert a RRGGBB hex to opaque ASS &H00BBGGRR& (BGR reversed)."""
    color = (color or "").strip().lstrip("#")
    if len(color) != 6:
        return color
    r, g, b = color[0:2], color[2:4], color[4:6]
    return f"&H00{b.upper()}{g.upper()}{r.upper()}&"


def duration_seconds(video: Path) -> float:
    ffprobe = Path(ffmpeg_path()).with_name("ffprobe.exe")
    if not ffprobe.exists():
        return 0
    process = subprocess.run(
        [str(ffprobe), "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(video)],
        capture_output=True,
        text=True,
        check=True,
    )
    return float(process.stdout.strip() or 0)


def safe_unlink(path: Path) -> None:
    """清理临时文件；删除失败绝不致命。

    某些运行环境（沙箱/安全软件）会拦截删除并抛 OSError（如
    safe-delete FAIL_CLOSED）。临时探针文件删不掉只是留下垃圾，
    下次会被覆盖，不应让整个渲染/配音流程失败。
    """
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def effective_clip_duration(job: "Job", work: Path) -> float:
    """成片目标时长（秒）：显式裁剪终点优先；否则用改口型视频真实时长减去片头裁剪。

    旧项目可能没记录 person_duration/duration（值为 0），直接按字段算会把
    clip_duration 算成 0.1 秒，字幕时间轴只覆盖一闪而过的 0.1 秒。这里先探测
    output 文件真实时长，探测失败再退回 job 记录值，最后兜底 1 秒。
    """
    if job.trim_end is not None:
        return max(0.1, job.trim_end - job.trim_start)
    real = 0.0
    if job.output_name:
        source = work / job.output_name
        if source.exists():
            try:
                real = duration_seconds(source)
            except subprocess.CalledProcessError:
                real = 0.0
    base = real or job.person_duration or job.duration or 1
    return max(0.1, base - job.trim_start)


def extract_audio(video: Path, target: Path) -> None:
    subprocess.run(
        [ffmpeg_path(), "-y", "-i", str(video), "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(target)],
        capture_output=True,
        check=True,
    )


def pcm_to_wav(source: Path, target: Path) -> None:
    subprocess.run(
        [ffmpeg_path(), "-y", "-f", "s16le", "-ar", "24000", "-ac", "1", "-i", str(source), str(target)],
        capture_output=True,
        check=True,
    )


def trim_audio_to_duration(source: Path, target: Path, target_duration: float) -> None:
    """Trim generated speech locally so VideoRetalk receives matching media lengths."""
    fade_duration = min(0.15, max(0.0, target_duration / 10))
    fade_start = max(0.0, target_duration - fade_duration)
    command = [ffmpeg_path(), "-y", "-i", str(source), "-t", f"{target_duration:.3f}"]
    if fade_duration:
        command.extend(["-af", f"afade=t=out:st={fade_start:.3f}:d={fade_duration:.3f}"])
    command.append(str(target))
    subprocess.run(command, capture_output=True, check=True)


def extend_video_to_duration(source: Path, target: Path, target_duration: float, strategy: str) -> None:
    """Create a silent video that lasts exactly as long as the generated voice."""
    if strategy == "loop_video":
        command = [
            ffmpeg_path(), "-y", "-stream_loop", "-1", "-i", str(source), "-t", str(target_duration),
            "-map", "0:v:0", "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(target),
        ]
    elif strategy == "freeze_tail":
        source_duration = duration_seconds(source)
        extra_duration = max(0.1, target_duration - source_duration)
        video_filter = (
            f"[0:v]tpad=stop_mode=clone:stop_duration={extra_duration:.3f},"
            f"trim=duration={target_duration:.3f},setpts=PTS-STARTPTS[v]"
        )
        command = [
            ffmpeg_path(), "-y", "-i", str(source), "-filter_complex", video_filter,
            "-map", "[v]", "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(target),
        ]
    else:
        raise ValueError(f"Unknown video extension strategy: {strategy}")
    subprocess.run(command, capture_output=True, check=True)


def normalize_videoretalk_video(source: Path, target: Path) -> None:
    """Normalize phone footage for VideoRetalk (minimum side 640, H.264, 30fps)."""
    command = [
        ffmpeg_path(), "-y", "-i", str(source),
        "-vf", r"scale=if(lt(iw\,640)\,640\,-2):if(lt(ih\,640)\,640\,-2),fps=30",
        "-c:v", "libx264", "-preset", "fast", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-ar", "48000", "-movflags", "+faststart", str(target),
    ]
    try:
        subprocess.run(command, capture_output=True, check=True)
    except subprocess.CalledProcessError as error:
        detail = (error.stderr or b"").decode("utf-8", errors="replace")[-500:]
        raise RuntimeError(f"人物视频预处理失败：{detail}") from error


def prepare_clone_sample(sample: Path, target_dir: Path) -> Path:
    """CosyVoice 样音规范：24kHz 单声道 WAV，最长 30 秒（样本越长音色越稳）。"""
    target = target_dir / f"clone-{uuid.uuid4().hex[:8]}.wav"
    subprocess.run(
        [ffmpeg_path(), "-y", "-i", str(sample), "-t", "30", "-ac", "1", "-ar", "24000", str(target)],
        capture_output=True,
        check=True,
    )
    if not target.exists() or target.stat().st_size == 0:
        raise RuntimeError("样音预处理失败，请更换 10–60 秒清晰、单人的自然口播样音；官方建议 10–20 秒。")
    return target


class AudioCollector(QwenTtsRealtimeCallback):
    def __init__(self) -> None:
        self.audio = bytearray()
        self.done = threading.Event()
        self.error: str | None = None

    def on_open(self) -> None:
        pass

    def on_close(self, close_status_code: int, close_msg: str) -> None:
        self.done.set()

    def on_event(self, event: dict[str, Any]) -> None:
        if event.get("type") == "response.audio.delta":
            import base64

            delta = event.get("delta") or event.get("audio", {}).get("data")
            if delta:
                self.audio.extend(base64.b64decode(delta))
        elif event.get("type") == "error":
            self.error = str(event)
            self.done.set()
        elif event.get("type") == "response.done":
            self.done.set()


class BailianPipeline:
    ASR_MODEL = "qwen-audio-3.0-asr-flash-filetrans"
    REWRITE_MODEL = "qwen3.7-flash"
    SYSTEM_TTS_MODEL = "qwen-audio-3.0-tts-plus"
    SYSTEM_TTS_VOICE = "longanlingxin"
    VIDEO_MODEL = "videoretalk"

    def __init__(self) -> None:
        self.key = require_api_key()
        self.workspace_id = require_workspace_id()
        # The current Beijing ASR API is workspace-scoped. The generic DashScope
        # endpoint can accept a task but may fail it server-side for this model.
        dashscope.base_http_api_url = f"https://{self.workspace_id}.cn-beijing.maas.aliyuncs.com/api/v1"

    def headers(self, *, asynchronous: bool = False) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self.key}",
            "Content-Type": "application/json",
            # The private input OSS certificate is issued for this workspace.
            # Keep the request in the same workspace so the service can resolve it.
            "X-DashScope-WorkSpace": self.workspace_id,
        }
        if asynchronous:
            headers["X-DashScope-Async"] = "enable"
        return headers

    def stage(self, file: Path, model: str) -> str:
        """Let the DashScope SDK upload a local asset to its temporary OSS input store."""
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                uploaded, url, _ = check_and_upload_local(model, str(file), self.key, None)
                break
            except Exception as error:
                last_error = error
                if attempt < 2:
                    time.sleep(2 * (attempt + 1))
        else:
            raise RuntimeError(f"百炼媒体临时上传失败，已自动重试 3 次：{last_error}") from last_error
        # DashScope returns an oss:// URI for its own temporary input bucket.
        # It is a valid model input even though it is not browser-accessible.
        if not url or not str(url).startswith(("http://", "https://", "oss://")):
            raise RuntimeError("百炼未能为本地媒体生成临时访问地址。")
        return str(url)

    @staticmethod
    def _walk_text(value: Any) -> list[str]:
        if isinstance(value, str):
            return [value]
        if isinstance(value, dict):
            found: list[str] = []
            for key, item in value.items():
                if key in {"text", "sentence", "transcript", "content"}:
                    found.extend(BailianPipeline._walk_text(item))
                elif isinstance(item, (dict, list)):
                    found.extend(BailianPipeline._walk_text(item))
            return found
        if isinstance(value, list):
            return [part for item in value for part in BailianPipeline._walk_text(item)]
        return []

    def _asr_url(self, suffix: str) -> str:
        return f"https://{self.workspace_id}.cn-beijing.maas.aliyuncs.com/api/v1/{suffix.lstrip('/')}"

    def _asr_headers(self, *, asynchronous: bool = False) -> dict[str, str]:
        """Headers from the official workspace-domain HTTP example."""
        headers = {
            "Authorization": f"Bearer {self.key}",
            "Content-Type": "application/json",
        }
        if asynchronous:
            headers["X-DashScope-Async"] = "enable"
        return headers

    @staticmethod
    def _json_response(response: httpx.Response, prefix: str) -> dict[str, Any]:
        try:
            body = response.json()
        except ValueError as error:
            raise RuntimeError(f"{prefix}：百炼返回了无法解析的响应（HTTP {response.status_code}）。") from error
        if not isinstance(body, dict):
            raise RuntimeError(f"{prefix}：百炼返回了异常响应（HTTP {response.status_code}）。")
        return body

    def _submit_asr(self, audio: Path) -> str:
        """Submit through the official HTTP API, which supports SDK-uploaded oss:// inputs.

        DashScope's Python SDK accepts a local path helper, but its file-transcription
        SDK path does not support the resulting temporary ``oss://`` URL.  The HTTP
        endpoint explicitly supports it when OSS resource resolution is enabled.
        """
        headers = self._asr_headers(asynchronous=True)
        headers["X-DashScope-OssResourceResolve"] = "enable"
        response = bailian_request(
            "POST",
            self._asr_url("services/audio/asr/transcription"),
            headers=headers,
            payload={
                "model": self.ASR_MODEL,
                "input": {"file_urls": [self.stage(audio, self.ASR_MODEL)]},
                # Always send a parameters object.  It is required by the newer
                # workspace domains in some regions and is valid for Beijing too.
                "parameters": {"channel_id": [0], "language_hints": ["zh"]},
            },
        )
        body = self._json_response(response, "ASR 提交失败")
        if not response.is_success:
            raise RuntimeError(self._task_error("ASR 提交失败", body, body))
        output = body.get("output")
        task_id = output.get("task_id") if isinstance(output, dict) else None
        if not isinstance(task_id, str) or not task_id:
            raise RuntimeError(self._task_error("ASR 提交失败", body, body))
        return task_id

    def _fetch_asr(self, task_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
        response = bailian_request(
            "GET",
            self._asr_url(f"tasks/{task_id}"),
            headers=self._asr_headers(),
        )
        body = self._json_response(response, "ASR 状态查询失败")
        if not response.is_success:
            raise RuntimeError(self._task_error("ASR 状态查询失败", body, body))
        output = body.get("output")
        if not isinstance(output, dict):
            raise RuntimeError(self._task_error("ASR 状态查询失败", body, body))
        return body, output

    @staticmethod
    def _failed_asr_subtask(output: dict[str, Any]) -> dict[str, Any] | None:
        results = output.get("results")
        if not isinstance(results, list):
            return None
        return next(
            (item for item in results if isinstance(item, dict) and item.get("subtask_status") in {"FAILED", "CANCELED"}),
            None,
        )

    def _await_asr(self, audio: Path, *, timeout_seconds: int) -> tuple[dict[str, Any], dict[str, Any]]:
        task_id = self._submit_asr(audio)
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            body, output = self._fetch_asr(task_id)
            failed_subtask = self._failed_asr_subtask(output)
            if failed_subtask:
                raise RuntimeError(self._task_error("ASR 任务失败", body, {"output": output, "failed_subtask": failed_subtask}))
            status = output.get("task_status")
            if status == "SUCCEEDED":
                return body, output
            if status in {"FAILED", "CANCELED"}:
                raise RuntimeError(self._task_error("ASR 任务失败", body, body))
            time.sleep(2)
        raise TimeoutError("ASR 超时。")

    def transcribe(self, audio: Path) -> str:
        _, output = self._await_asr(audio, timeout_seconds=15 * 60)
        payload: Any = output
        # File-transcription results are usually a temporary JSON document.
        for url in self._find_urls(output):
            try:
                with httpx.Client(timeout=60, follow_redirects=True) as client:
                    response = client.get(url)
                    if response.is_success and "json" in response.headers.get("content-type", ""):
                        payload = [payload, response.json()]
            except httpx.HTTPError:
                continue
        texts = self._walk_text(payload)
        text = "\n".join(dict.fromkeys(part.strip() for part in texts if len(part.strip()) > 1))
        if text:
            return text
        raise RuntimeError("ASR 已完成但未返回可用文案。")

    @staticmethod
    def _task_error(prefix: str, result: Any, output: Any) -> str:
        """Expose DashScope's useful task diagnostics without leaking credentials."""
        data = output if isinstance(output, dict) else {}
        def result_value(key: str) -> Any:
            return result.get(key) if isinstance(result, dict) else getattr(result, key, None)

        # Different async endpoints place their diagnostic in different
        # nested objects (for example ``error`` or ``task_result``).  Pull
        # only known safe, human-readable fields rather than dumping the full
        # response, which might contain temporary media URLs.
        nested: list[dict[str, Any]] = []

        def collect(value: Any) -> None:
            if isinstance(value, dict):
                nested.append(value)
                for child in value.values():
                    collect(child)
            elif isinstance(value, list):
                for child in value:
                    collect(child)

        collect(data)

        def first_value(*keys: str) -> Any:
            for item in nested:
                for key in keys:
                    value = item.get(key)
                    if value not in (None, "") and not isinstance(value, (dict, list)):
                        return value
            return None

        details = {
            "code": result_value("code") or first_value("code", "error_code"),
            "message": result_value("message") or first_value("message", "error_message", "error_msg", "detail"),
            "request_id": result_value("request_id") or first_value("request_id", "requestId"),
            "task_status": first_value("task_status", "status"),
        }
        compact = ", ".join(f"{key}={value}" for key, value in details.items() if value not in (None, ""))
        return f"{prefix}{'：' + compact if compact else '：百炼未返回错误详情；请确认已开通该 ASR 模型。'}"

    @staticmethod
    def _find_urls(value: Any) -> list[str]:
        if isinstance(value, str):
            return [value] if value.startswith(("http://", "https://")) else []
        if isinstance(value, dict):
            return [url for item in value.values() for url in BailianPipeline._find_urls(item)]
        if isinstance(value, list):
            return [url for item in value for url in BailianPipeline._find_urls(item)]
        return []

    @staticmethod
    def _walk_sentences(value: Any) -> list[dict[str, Any]]:
        """Collect per-sentence {start, end, text} records (seconds) from a payload.

        DashScope file-transcription results nest sentence records with
        ``begin_time``/``end_time`` (milliseconds) under transcripts[].sentences[].
        Walking the whole payload avoids depending on the exact schema version.
        """
        found: list[dict[str, Any]] = []
        if isinstance(value, dict):
            begin, end, text = value.get("begin_time"), value.get("end_time"), value.get("text")
            if (
                isinstance(begin, (int, float))
                and isinstance(end, (int, float))
                and isinstance(text, str)
                and text.strip()
                and end > begin
            ):
                found.append({"start": round(begin / 1000, 2), "end": round(end / 1000, 2), "text": text.strip()})
            for item in value.values():
                if item is not value:
                    found.extend(BailianPipeline._walk_sentences(item))
        elif isinstance(value, list):
            for item in value:
                found.extend(BailianPipeline._walk_sentences(item))
        return found

    def transcribe_timeline(self, audio: Path) -> list[dict[str, Any]]:
        """Transcribe an audio file and keep per-sentence timestamps for subtitles."""
        _, output = self._await_asr(audio, timeout_seconds=10 * 60)
        payload: Any = output
        for url in self._find_urls(output):
            try:
                with httpx.Client(timeout=60, follow_redirects=True) as client:
                    response = client.get(url)
                    if response.is_success and "json" in response.headers.get("content-type", ""):
                        payload = [payload, response.json()]
            except httpx.HTTPError:
                continue
        sentences = self._walk_sentences(payload)
        if sentences:
            # Ensure chronological order regardless of nesting order.
            sentences.sort(key=lambda item: item["start"])
            return sentences
        raise RuntimeError("ASR 已完成但未返回带时间戳的分句。")

    def rewrite(self, transcript: str, instruction: str, target_duration: float | None = None) -> str:
        duration_rule = (
            f"目标口播时长约 {target_duration:.1f} 秒，尽量将成稿控制在该时长内。"
            if target_duration else "人物视频尚未上传，不要假设目标时长。"
        )
        prompt = (
            "你是短视频口播文案编辑。质量与原创边界同等重要：只可使用可验证的通用事实，"
            "不得保留、模仿或延续原作者的人设、个人经历、独创观点或表达。"
            "请围绕用户目标重新组织一篇独立口播稿，不得复用原稿的金句、比喻、段落顺序、"
            "叙事结构、案例细节或其他具有识别性的表达；不确定是否属于独创内容时，应改为通用表述或删除。"
            "输出仅包含可直接朗读的中文口播稿，不要标题、解释或 Markdown。"
            f"{duration_rule}\n"
            f"改写要求：{instruction}\n\n原稿：\n{transcript}"
        )
        # qwen3.7-flash 是工作区专属部署，只在 workspace 的 OpenAI 兼容端点上提供；
        # 公共域名与 SDK 默认文本路径都会被百炼以 InvalidParameter url error 拒绝。
        response = openai_compat_chat(
            base_url=f"https://{self.workspace_id}.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
            api_key=self.key,
            model=self.REWRITE_MODEL,
            messages=[{"role": "user", "content": prompt}],
            timeout=180,
            extra_body={"enable_thinking": False},
        )
        if response.status_code != 200:
            raise RuntimeError(f"文案改写失败：HTTP {response.status_code} {response.text[:300]}")
        return _extract_chat_content(response, "百炼")

    def generate_script(self, prompt: str, *, temperature: float = 0.65, max_tokens: int = 1600) -> str:
        """Generate a spoken script through the workspace OpenAI-compatible endpoint."""
        response = openai_compat_chat(
            base_url=f"https://{self.workspace_id}.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
            api_key=self.key,
            model=self.REWRITE_MODEL,
            messages=[{"role": "user", "content": prompt}],
            timeout=180,
            temperature=temperature,
            max_tokens=max_tokens,
            extra_body={"enable_thinking": False},
        )
        if response.status_code != 200:
            raise RuntimeError(f"AI 文案创作失败：HTTP {response.status_code} {response.text[:300]}")
        content = _extract_chat_content(response, "百炼")
        if not content.strip():
            raise RuntimeError("百炼未返回可用文案。")
        return content.strip()

    CLONE_MODEL = "cosyvoice-v3.5-plus"

    def enroll_voice(self, audio: Path, job_id: str, *, allow_tunnel: bool = False) -> str:
        """CosyVoice 音色注册：与合成同一模型；样音经 DashScope 临时 OSS 暂存后注册。

        历史实现曾把样音挂到 trycloudflare 公网隧道再让阿里云来抓取，但
        trycloudflare 在境内网络长期不可达（本地自检都会被代理挡成 502），
        导致音色注册稳定失败。现改用 DashScope SDK 自带的临时 OSS 输入桶：
        样音只上传到阿里云自己的私有暂存区，配 X-DashScope-OssResourceResolve
        头让注册服务直接解析 oss:// 地址，全程不出阿里云网络，无需公网隧道，
        也不再需要用户的传输同意勾选（allow_tunnel 参数保留兼容旧调用）。
        """
        prefix = re.sub(r"[^a-z0-9]", "", job_id.lower())[:8] or "voice"
        temp_dir = Path(tempfile.mkdtemp(prefix="clone-sample-"))
        try:
            prepared = prepare_clone_sample(audio, temp_dir)
            # DashScope 会把本地文件上传到其临时 OSS 输入桶并返回 oss:// URI。
            oss_url = self.stage(prepared, f"voice-enroll-{prefix}")
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

        try:
            # Use the documented HTTP API rather than hiding the response in
            # an SDK exception.  This preserves code/message/request_id in the
            # UI when the provider rejects a sample or an account capability.
            headers = self._asr_headers()
            # stage() 返回的是 oss:// 私有地址；这里直接调注册端点，必须
            # 显式打开 OSS 解析，否则服务端读不到样音。
            headers["X-DashScope-OssResourceResolve"] = "enable"
            response = bailian_request(
                "POST",
                self._asr_url("services/audio/tts/customization"),
                headers=headers,
                payload={
                    "model": "voice-enrollment",
                    "input": {
                        "action": "create_voice",
                        "target_model": self.CLONE_MODEL,
                        "prefix": prefix,
                        "url": oss_url,
                        "language_hints": ["zh"],
                        "max_prompt_audio_length": 30.0,
                    },
                },
            )
            body = self._json_response(response, "阿里云 CosyVoice 音色注册失败")
            if not response.is_success:
                raise RuntimeError(self._task_error("阿里云 CosyVoice 音色注册失败", body, body))
            output = body.get("output")
            voice_id = output.get("voice_id") if isinstance(output, dict) else None
            if not isinstance(voice_id, str) or not voice_id:
                raise RuntimeError(self._task_error("阿里云 CosyVoice 未返回音色 ID", body, body))
        except RuntimeError:
            raise
        except Exception as error:
            raise RuntimeError(f"阿里云 CosyVoice 音色注册失败：{str(error)[:300]}") from error
        return voice_id

    def synthesize(
        self,
        text: str,
        voice_id: str,
        target: Path,
        *,
        speed: str = "standard",
        emotion: str = "natural",
        rate: float = 1.0,
        volume: int = 50,
        pitch: float = 1.0,
        seed: int = 0,
        language: str = "auto",
        instruction: str = "",
    ) -> None:
        """CosyVoice 合成：与音色注册使用同一模型，支持官方精细控制参数。

        - instruction：任意自然语言表达指令（官方限 100 字符），控制情感/角色/语气
        - rate/volume/pitch：官方数值参数（语速 0.5–2.0 / 音量 0–100 / 音高 0.5–2.0）
        - seed：随机种子，非 0 时相同输入可复现相同合成结果
        - language：发音语言提示（zh/en/ja/ko 等），auto 时不传由模型自行判断
        """
        extra: dict[str, Any] = {
            "rate": rate,
            "volume": volume,
            "pitch": pitch,
            "instruction": cosyvoice_instruction(speed, emotion, instruction),
        }
        if seed:
            extra["seed"] = seed
        if language and language != "auto":
            extra["language_hints"] = [language]
        try:
            result = HttpSpeechSynthesizer.call(
                model=self.CLONE_MODEL,
                text=text,
                voice=voice_id,
                audio_format="wav",
                sample_rate=24000,
                stream=False,
                api_key=self.key,
                **extra,
            )
        except Exception as error:
            raise RuntimeError(f"阿里云 CosyVoice 合成失败：{str(error)[:300]}") from error
        audio = result.get_audio_data() if hasattr(result, "get_audio_data") else None
        if not audio and getattr(result, "audio_url", None):
            audio = httpx.get(result.audio_url, timeout=120).content
        if not audio:
            raise RuntimeError("阿里云 CosyVoice 未返回音频数据，请稍后重试或改用 MiMo 声音复刻。")
        if not audio.startswith(b"RIFF"):
            raise RuntimeError("阿里云 CosyVoice 返回的音频格式异常。")
        target.write_bytes(audio)

    def synthesize_builtin_tts(self, text: str, target: Path, *, speed: str = "standard", voice: str = SYSTEM_TTS_VOICE) -> None:
        """Generate a system-voice preview through the native Qwen Audio TTS API."""
        rate = {"slow": 0.85, "standard": 1.0, "fast": 1.15}.get(speed, 1.0)
        try:
            result = HttpSpeechSynthesizer.call(
                model=self.SYSTEM_TTS_MODEL,
                text=text,
                voice=voice,
                audio_format="wav",
                sample_rate=24000,
                stream=False,
                rate=rate,
                api_key=self.key,
            )
        except Exception as error:
            raise RuntimeError(f"阿里云 Qwen Audio TTS 合成失败：{str(error)[:300]}") from error
        audio = result.get_audio_data() if hasattr(result, "get_audio_data") else None
        if not audio and getattr(result, "audio_url", None):
            audio = httpx.get(result.audio_url, timeout=120).content
        if not audio:
            raise RuntimeError("阿里云 Qwen Audio TTS 未返回音频数据，请确认已开通该模型。")
        if not audio.startswith(b"RIFF"):
            raise RuntimeError("阿里云 Qwen Audio TTS 返回的音频格式异常。")
        target.write_bytes(audio)

    QWEN_VC_ENROLL_MODEL = "qwen-voice-enrollment"
    QWEN_VC_MODEL = "qwen3-tts-vc-realtime-2026-01-15"

    def enroll_voice_qwen_vc(self, audio: Path, job_id: str) -> tuple[str, str | None]:
        """Qwen3-TTS 复刻音色注册：样音转 24kHz 单声道 WAV 后 base64 直传。

        与 CosyVoice 注册的区别（均已实测验证）：action 必须是 "create"；
        preferred_name 只能字母数字；音频走 base64 data URL 而非临时 OSS；
        同步返回，音色名字段是 output.voice（不是 voice_id）。
        返回 (音色名, 降级原因或 None)：样音质量欠佳时百炼返回 fallback_mode=true。
        """
        import tempfile

        preferred = re.sub(r"[^a-zA-Z0-9]", "", job_id)[:16] or "qwenvc"
        temp_dir = Path(tempfile.mkdtemp(prefix="qwen-vc-enroll-"))
        try:
            prepared = temp_dir / "clone-sample.wav"
            subprocess.run(
                [ffmpeg_path(), "-y", "-i", str(audio), "-t", "30", "-ac", "1", "-ar", "24000", str(prepared)],
                capture_output=True,
                check=True,
            )
            if not prepared.exists() or prepared.stat().st_size == 0:
                raise RuntimeError("样音预处理失败，请更换 10–60 秒清晰、单人的自然口播样音；官方建议 10–20 秒。")
            data_url = "data:audio/wav;base64," + base64.b64encode(prepared.read_bytes()).decode()
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)
        response = bailian_request(
            "POST",
            f"https://{self.workspace_id}.cn-beijing.maas.aliyuncs.com/api/v1/services/audio/tts/customization",
            headers={"Authorization": f"Bearer {self.key}", "Content-Type": "application/json"},
            payload={
                "model": self.QWEN_VC_ENROLL_MODEL,
                "input": {
                    "action": "create",
                    "target_model": self.QWEN_VC_MODEL,
                    "preferred_name": preferred,
                    "audio": {"data": data_url},
                    "language": "zh",
                },
            },
        )
        try:
            body = response.json()
        except ValueError:
            body = {}
        if not response.is_success:
            raise RuntimeError(self._task_error("百炼 Qwen3-TTS 音色注册失败", response, body))
        output = body.get("output") or {}
        voice = output.get("voice") if isinstance(output, dict) else None
        if not isinstance(voice, str) or not voice:
            raise RuntimeError(self._task_error("百炼 Qwen3-TTS 未返回音色名", response, body))
        fallback_reason = None
        if output.get("fallback_mode"):
            fallback_reason = str(output.get("fallback_reason") or "").strip() or "样音质量欠佳"
        return voice, fallback_reason

    def synthesize_qwen_vc(self, text: str, voice: str, target: Path, language: str = "auto") -> None:
        """Qwen3-TTS 复刻合成：实时 WebSocket，裸参数（不带表达指令/情感参数，音色相似度最高）。

        connect → update_session → append_text → finish 的顺序不能换；
        返回 24kHz 16bit 单声道 PCM 裸流，这里用 wave 模块包成 WAV。
        """
        import wave

        collector = AudioCollector()
        dashscope.api_key = self.key
        client = QwenTtsRealtime(model=self.QWEN_VC_MODEL, callback=collector, workspace=self.workspace_id)
        language_names = {
            "auto": "Auto", "zh": "Chinese", "en": "English", "de": "German",
            "it": "Italian", "pt": "Portuguese", "es": "Spanish", "ja": "Japanese",
            "ko": "Korean", "fr": "French", "ru": "Russian",
        }
        client.connect()
        try:
            client.update_session(
                voice=voice,
                response_format=AudioFormat.PCM_24000HZ_MONO_16BIT,
                language_type=language_names.get(language, "Auto"),
            )
            client.append_text(text)
            client.finish()
            if not collector.done.wait(timeout=10 * 60):
                raise TimeoutError("百炼 Qwen3-TTS 合成超时。")
            if collector.error:
                raise RuntimeError(f"百炼 Qwen3-TTS 合成失败：{collector.error[:300]}")
            if not collector.audio:
                raise RuntimeError("百炼 Qwen3-TTS 未返回音频数据。")
            with wave.open(str(target), "wb") as wav_file:
                wav_file.setnchannels(1)
                wav_file.setsampwidth(2)
                wav_file.setframerate(24000)
                wav_file.writeframes(bytes(collector.audio))
        finally:
            client.close()

    def lipsync(
        self,
        video: Path,
        audio: Path,
        cancelled: Callable[[], bool] | None = None,
        progress: Callable[[int], None] | None = None,
    ) -> str:
        # VideoRetalk is a newer dedicated video-synthesis API, not generic media[].
        request = {
            "model": self.VIDEO_MODEL,
            "input": {"video_url": self.stage(video, self.VIDEO_MODEL), "audio_url": self.stage(audio, self.VIDEO_MODEL)},
        }
        # check_and_upload_local() returns DashScope's private oss:// input URL.
        # The SDK adds this header automatically for such inputs; this endpoint
        # is called directly here, so we must explicitly enable OSS resolution.
        headers = self.headers(asynchronous=True)
        headers["X-DashScope-OssResourceResolve"] = "enable"
        response = bailian_request(
            "POST",
            "https://dashscope.aliyuncs.com/api/v1/services/aigc/image2video/video-synthesis",
            headers=headers,
            payload=request,
        )
        if not response.is_success:
            raise RuntimeError(f"VideoRetalk 提交失败：{response.text}")
        task_id = response.json().get("output", {}).get("task_id")
        if not task_id:
            raise RuntimeError("VideoRetalk 未返回 task_id。")
        deadline = time.monotonic() + 30 * 60
        started = time.monotonic()
        while time.monotonic() < deadline:
            if cancelled and cancelled():
                raise RuntimeError("改口型任务已取消。")
            response = bailian_request(
                "GET",
                f"https://dashscope.aliyuncs.com/api/v1/tasks/{task_id}",
                headers=self.headers(),
            )
            if not response.is_success:
                raise RuntimeError(f"VideoRetalk 查询失败：{response.text}")
            result = response.json()
            output = result.get("output", {})
            status = output.get("task_status")
            if status == "SUCCEEDED":
                url = output.get("video_url") or output.get("url")
                if url:
                    return url
                raise RuntimeError("VideoRetalk 已完成但未返回视频 URL。")
            if status in {"FAILED", "CANCELED"}:
                code = output.get("code") or result.get("code") or status
                message = output.get("message") or result.get("message") or "云端未提供详细原因"
                raise RuntimeError(f"VideoRetalk 失败（{code}）：{message}")
            if progress:
                # Long videos can take several minutes. Move gradually from
                # the submission checkpoint to 99%, never presenting success
                # until the cloud task actually returns SUCCEEDED.
                progress(min(99, 48 + int((time.monotonic() - started) / 180 * 51)))
            time.sleep(3)
        raise TimeoutError("VideoRetalk 超时。")


def transcribe_with_mimo(audio: Path, model: str) -> str:
    if model != "mimo-v2.5-asr":
        raise RuntimeError(f"不支持的 MiMo ASR 模型：{model}")
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": [{
            "type": "input_audio",
            "input_audio": {
                "data": base64.b64encode(audio.read_bytes()).decode("ascii"),
                "format": "wav",
            },
        }]}],
        "temperature": 0,
    }
    # 服务端偶发 5xx（尤其音频较大时）时自动重试，与连接层重试策略保持一致。
    last_detail = ""
    for attempt in range(3):
        response = mimo_post(payload, timeout=240)
        if response.is_success:
            break
        last_detail = f"HTTP {response.status_code} {response.text[:300]}"
        # 4xx 是请求本身的问题（鉴权/参数/格式），重试无意义，直接失败。
        if response.status_code < 500:
            raise RuntimeError(f"MiMo 转写失败：{last_detail}")
        safe_log(f"[asr] MiMo 返回 {response.status_code}，第 {attempt + 1}/3 次尝试失败")
        if attempt < 2:
            time.sleep(3 * (attempt + 1))
    else:
        raise RuntimeError(f"MiMo 转写失败（已自动重试 3 次）：{last_detail}")
    result = response.json()
    try:
        content = result["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as error:
        raise RuntimeError(f"MiMo 未返回转写内容：{str(result)[:500]}") from error
    if isinstance(content, list):
        content = "\n".join(str(part.get("text", "")) if isinstance(part, dict) else str(part) for part in content)
    text = str(content).strip()
    if not text:
        raise RuntimeError("MiMo 未返回可用文案。")
    return text


def _resolve_asr_model(requested: str | None) -> str:
    """Resolve ``auto`` deterministically without silently downloading local models."""
    model = (requested or "auto").strip() or "auto"
    if model != "auto":
        return model
    if _has_settings("MIMO_API_KEY"):
        return "mimo-v2.5-asr"
    if _has_settings("DASHSCOPE_API_KEY", "DASHSCOPE_WORKSPACE_ID"):
        return "qwen-audio-3.0-asr-flash-filetrans"
    if funasr_ready("local:funasr-paraformer"):
        return "local:funasr-paraformer"
    return "qwen-audio-3.0-asr-flash-filetrans"


def transcribe_for_model(
    audio: Path,
    model: str,
    *,
    pipeline: Any | None = None,
    duration: float | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    """Run one ASR route and return text plus native or proportional timing."""
    effective = _resolve_asr_model(model)
    service_asr = _service_connection_for_model(effective, "asr")
    if service_asr:
        provider, _, selected = service_asr
        transcript = transcribe_with_openai_compat(audio, provider, selected)
        return transcript, timed_lines(transcript, duration) if duration else []
    if effective.startswith("local:funasr-"):
        return transcribe_timeline_with_funasr(audio, effective)
    if effective == "mimo-v2.5-asr":
        transcript = transcribe_with_mimo(audio, effective)
        return transcript, timed_lines(transcript, duration) if duration else []
    active_pipeline = pipeline or BailianPipeline()
    if duration:
        try:
            timeline = active_pipeline.transcribe_timeline(audio)
            if timeline:
                return "\n".join(str(item.get("text", "")).strip() for item in timeline if item.get("text")), timeline
        except Exception:
            # Text transcription below remains the authoritative fallback.
            pass
    transcript = active_pipeline.transcribe(audio)
    return transcript, timed_lines(transcript, duration) if duration else []


def _build_rewrite_prompt(transcript: str, instruction: str, target_duration: float | None) -> str:
    """构建「提取并改写」统一提示词，所有改写供应商共用同一套要求。"""
    duration_rule = (
        f"人物视频约 {target_duration:.1f} 秒。这只是配音适配参考；若与保留完整信息冲突，优先保留完整信息，后续会自动裁切超出的配音。"
        if target_duration else "人物视频时长未知，不要擅自压缩内容。"
    )
    return (
        "你是短视频口播文案编辑。任务是创作高质量、独立的新口播稿，不是逐句改写、摘要、提纲、标题或导流话术。"
        "质量要求：内容准确、逻辑清晰、表达自然、有实际信息价值，并有完整的开场、主体和结尾。"
        "原创与侵权边界同等重要：只可使用可验证的通用事实、行业术语、产品名称、数字和操作步骤；"
        "不得复用或近似改写参考内容的金句、比喻、独创措辞、段落顺序、叙事结构、案例细节、人物设定、个人经历或有识别性的表达。"
        "请根据用户目标重新选择结构、论述顺序、表达方式和通用例子；若某项内容依赖原作者的独创案例、观点或表述，应改成通用表述或省略，"
        "不要为了覆盖原稿或凑篇幅而保留。不得编造事实、人物经历、数据或承诺。"
        "若参考内容包含歌词、诗歌、书籍段落、影视台词等第三方受保护内容，必须完全用自己的原创表达替代，"
        "不得引用或模仿其表达形式；你的输出将被直接用于公开发布，不得存在对参考内容的任何侵权风险。"
        "新稿不得少于 160 字，但无需与参考内容保持相近篇幅。"
        "输出仅包含可直接朗读的中文口播稿，不要标题、解释、版权声明或 Markdown。"
        f"{duration_rule}\n用户的风格与目标要求：{instruction}\n\n参考内容：\n{transcript}"
    )


def rewrite_with_mimo(transcript: str, instruction: str, model: str, target_duration: float | None = None) -> str:
    if model not in {"mimo-v2.5", "mimo-v2.5-pro"}:
        raise RuntimeError(f"不支持的 MiMo 文案模型：{model}")
    source_chars = len(re.sub(r"\s+", "", transcript))
    # 质量下限不再与参考稿篇幅绑定，避免为了“像原稿一样长”而产生近似改写。
    minimum_chars = 160
    prompt = _build_rewrite_prompt(transcript, instruction, target_duration)
    response = mimo_post({"model": model, "messages": [{"role": "user", "content": prompt}], "temperature": 0.3}, timeout=120)
    if not response.is_success:
        raise RuntimeError(f"MiMo 文案改写失败：HTTP {response.status_code} {response.text[:300]}")
    result = response.json()
    try:
        content = result["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as error:
        raise RuntimeError(f"MiMo 未返回改写结果：{str(result)[:500]}") from error
    text = str(content).strip()
    if not text:
        raise RuntimeError("MiMo 未返回可用的新文案。")
    if source_chars >= 300 and len(re.sub(r"\s+", "", text)) < minimum_chars:
        retry_prompt = (
            f"你刚才输出的文案信息不足。请重新生成一篇至少 {minimum_chars} 字、结构完整且可直接朗读的独立口播稿。"
            "补足必要的通用背景、解释和结尾，但不得复用或近似改写参考内容的独创措辞、金句、案例叙事、段落顺序、人物设定或个人经历。"
            "不要解释，不要输出标题。\n\n"
            + prompt
        )
        retry = mimo_post({"model": model, "messages": [{"role": "user", "content": retry_prompt}], "temperature": 0.3}, timeout=120)
        if retry.is_success:
            retried_content = retry.json().get("choices", [{}])[0].get("message", {}).get("content", "")
            retried_text = str(retried_content).strip()
            if len(re.sub(r"\s+", "", retried_text)) >= minimum_chars:
                text = retried_text
        if len(re.sub(r"\s+", "", text)) < minimum_chars:
            raise RuntimeError("文案改写结果过短，已自动重试仍未保留足够信息；请调整改写要求后重试。")
    return text


def rewrite_with_compat(transcript: str, instruction: str, target_duration: float | None, config: tuple[str, str, str]) -> str:
    """通过用户添加的 OpenAI 兼容供应商改写。"""
    base_url, api_key, model = config
    source_chars = len(re.sub(r"\s+", "", transcript))
    minimum_chars = 160
    prompt = _build_rewrite_prompt(transcript, instruction, target_duration)

    def _chat(user_prompt: str) -> httpx.Response:
        # 思考型模型的 reasoning 会消耗 max_tokens 预算，导致正文被截断
        # 甚至为空（实测 deepseek-v4-flash-0731 约 1/3 概率触发）。DeepSeek
        # 官方支持关闭思考；不支持该字段的端点会忽略它，无副作用。
        extra: dict[str, Any] = {}
        if model.startswith(("deepseek-", "qwen3")):
            extra["thinking"] = {"type": "disabled"}
        return openai_compat_chat(
            base_url=base_url, api_key=api_key, model=model,
            messages=[{"role": "user", "content": user_prompt}],
            timeout=120, temperature=0.3, max_tokens=4000, extra_body=extra,
        )

    text = _extract_chat_content(_chat(prompt), f"通用接口（{model}）")
    if source_chars >= 300 and len(re.sub(r"\s+", "", text)) < minimum_chars:
        retry_prompt = (
            f"你刚才输出的文案信息不足。请重新生成一篇至少 {minimum_chars} 字、结构完整且可直接朗读的独立口播稿。"
            "补足必要的通用背景、解释和结尾，但不得复用或近似改写参考内容的独创措辞、金句、案例叙事、段落顺序、人物设定或个人经历。"
            "不要解释，不要输出标题。\n\n"
            + prompt
        )
        retry = _chat(retry_prompt)
        if retry.is_success:
            try:
                retried_text = _extract_chat_content(retry, f"通用接口（{model}）")
            except RuntimeError:
                retried_text = ""
            if len(re.sub(r"\s+", "", retried_text)) >= minimum_chars:
                text = retried_text
        if len(re.sub(r"\s+", "", text)) < minimum_chars:
            raise RuntimeError("文案改写结果过短，已自动重试仍未保留足够信息；请调整改写要求后重试。")
    return text


def rewrite_auto(transcript: str, instruction: str, target_duration: float | None = None) -> str:
    """改写自动路由：MiMo → 百炼；自定义供应商须由用户明确选择。"""
    if local_setting("MIMO_API_KEY"):
        return rewrite_with_mimo(transcript, instruction, "mimo-v2.5", target_duration)
    return BailianPipeline().rewrite(transcript, instruction, target_duration)


def synthesize_with_mimo(text: str, target: Path, *, voice: str = "冰糖") -> None:
    payload = {
        "model": "mimo-v2.5-tts",
        "messages": [
            {"role": "user", "content": "请用自然、清晰、亲切的普通话朗读，语速自然。"},
            {"role": "assistant", "content": text},
        ],
        "audio": {"format": "wav", "voice": voice},
    }
    response = mimo_post(payload, timeout=240)
    if not response.is_success:
        raise RuntimeError(f"MiMo 音频生成失败：HTTP {response.status_code} {response.text[:300]}")
    try:
        audio_data = response.json()["choices"][0]["message"]["audio"]["data"]
        audio = base64.b64decode(audio_data)
    except (KeyError, IndexError, TypeError, ValueError) as error:
        raise RuntimeError("MiMo 未返回可播放的音频数据。") from error
    if not audio.startswith(b"RIFF"):
        raise RuntimeError("MiMo 返回的音频格式异常。")
    target.write_bytes(audio)


def synthesize_voiceclone_with_mimo(text: str, sample: Path, target: Path, emotion: str, style: str = "") -> None:
    mime = "audio/mpeg" if sample.suffix.lower() == ".mp3" else "audio/wav"
    voice_data = base64.b64encode(sample.read_bytes()).decode("ascii")
    # MiMo 用 user 消息做自然语言表达控制；自定义指令优先，留空时按情绪档位映射。
    style = style.strip() or {"natural": "自然、清晰、亲切。", "warm": "热情、有感染力。", "steady": "沉稳、可信、不过度夸张。"}.get(emotion, "自然、清晰、亲切。")
    payload = {
        "model": "mimo-v2.5-tts-voiceclone",
        "messages": [{"role": "user", "content": style}, {"role": "assistant", "content": text}],
        "audio": {"format": "wav", "voice": f"data:{mime};base64,{voice_data}"},
    }
    response = mimo_post(payload, timeout=240)
    if not response.is_success:
        raise RuntimeError(f"MiMo 声音复刻失败：HTTP {response.status_code} {response.text[:300]}")
    try:
        audio = base64.b64decode(response.json()["choices"][0]["message"]["audio"]["data"])
    except (KeyError, IndexError, TypeError, ValueError) as error:
        raise RuntimeError("MiMo 未返回可播放的克隆音频。") from error
    if not audio.startswith(b"RIFF"):
        raise RuntimeError("MiMo 返回的克隆音频格式异常。")
    target.write_bytes(audio)


def download(
    url: str,
    target: Path,
    max_bytes: int | None = None,
    on_progress: Callable[[int, int], None] | None = None,
    on_retry: Callable[[int, int, Exception], None] | None = None,
) -> None:
    """Download a remote file, resuming when a CDN closes a response early.

    If *on_progress* is given it is called with ``(downloaded, total_or_0)``
    at most once per second so callers can update UI progress.
    """
    partial = target.with_suffix(f"{target.suffix}.part")
    partial.unlink(missing_ok=True)
    last_error: Exception | None = None
    dl_timeout = httpx.Timeout(connect=30, read=60, write=60, pool=30)
    max_attempts = 6

    for attempt in range(max_attempts):
        offset = partial.stat().st_size if partial.exists() else 0
        # Identity avoids content encodings that make byte ranges ambiguous on
        # some CDNs. Each retry keeps the .part file and requests only what is
        # missing.
        headers = {"Accept-Encoding": "identity"}
        if offset:
            headers["Range"] = f"bytes={offset}-"
        try:
            with httpx.stream("GET", url, headers=headers, timeout=dl_timeout, follow_redirects=True) as response:
                response.raise_for_status()
                if offset and response.status_code == 200:
                    partial.unlink(missing_ok=True)
                    offset = 0
                elif offset and response.status_code != 206:
                    raise RuntimeError(f"服务器不支持断点续传（HTTP {response.status_code}）。")
                elif offset:
                    content_range = response.headers.get("content-range", "")
                    range_match = re.match(r"bytes\s+(\d+)-\d+/(?:\d+|\*)", content_range, re.IGNORECASE)
                    if not range_match or int(range_match.group(1)) != offset:
                        raise RuntimeError("服务器返回的断点位置不正确，无法安全续传。")

                content_length = int(response.headers.get("content-length") or 0)
                content_range = response.headers.get("content-range", "")
                total_text = content_range.rsplit("/", 1)[-1] if "/" in content_range else ""
                expected_total = int(total_text) if total_text.isdigit() else offset + content_length
                if max_bytes and expected_total > max_bytes:
                    raise RuntimeError("视频文件超过 500 MB 限制。")

                last_report = time.monotonic()
                downloaded = offset
                with partial.open("ab" if offset else "wb") as output:
                    for chunk in response.iter_bytes():
                        output.write(chunk)
                        downloaded += len(chunk)
                        if on_progress and time.monotonic() - last_report >= 1.0:
                            on_progress(downloaded, expected_total or 0)
                            last_report = time.monotonic()
                        if max_bytes and output.tell() > max_bytes:
                            raise RuntimeError("视频文件超过 500 MB 限制。")
                if on_progress:
                    on_progress(downloaded, expected_total or downloaded)

            if expected_total and partial.stat().st_size != expected_total:
                raise RuntimeError("文件大小与服务器声明不一致。")
            partial.replace(target)
            return
        except (httpx.HTTPError, OSError, RuntimeError) as error:
            last_error = error
            if max_bytes and "500 MB" in str(error):
                partial.unlink(missing_ok=True)
                raise
            if attempt == max_attempts - 1:
                partial.unlink(missing_ok=True)
                raise RuntimeError(f"获取参考视频时网络中断；已自动重试 {max_attempts} 次：{error}") from error
            if on_retry:
                on_retry(attempt + 2, max_attempts, error)
            # Immediate retries tend to hit the same overloaded CDN edge.
            time.sleep(min(8, 2 ** attempt))

    raise RuntimeError(f"获取参考视频失败：{last_error}")


def timed_lines(text: str, duration: float) -> list[dict[str, Any]]:
    """Fallback timed transcript when the ASR provider returns only a full transcript."""
    chunks = [part.strip() for part in re.split(r"(?<=[。！？!?])\s*|\n+", text) if part.strip()]
    if not chunks and text.strip():
        chunks = [text.strip()]
    total_weight = sum(max(1, len(part)) for part in chunks) or 1
    cursor = 0.0
    lines: list[dict[str, Any]] = []
    for index, part in enumerate(chunks):
        width = duration * max(1, len(part)) / total_weight
        end = duration if index == len(chunks) - 1 else round(cursor + width, 2)
        lines.append({"start": round(cursor, 2), "end": end, "text": part})
        cursor = end
    return lines


def sentences_to_subtitles(sentences: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Split ASR sentences into short subtitle lines, keeping sentence timings.

    ASR sentence-level timestamps are accurate; a long sentence is cut into
    subtitle-sized chunks whose sub-spans are proportional to chunk length.
    """
    lines: list[dict[str, Any]] = []
    for sentence in sentences:
        segments = [part for part in split_subtitles(sentence.get("text", "")) if part]
        if not segments:
            continue
        span = max(0.2, float(sentence.get("end", 0)) - float(sentence.get("start", 0)))
        total = sum(len(part) for part in segments) or 1
        cursor = float(sentence.get("start", 0))
        for index, segment in enumerate(segments):
            end = (
                float(sentence.get("end", 0))
                if index == len(segments) - 1
                else round(cursor + span * len(segment) / total, 2)
            )
            if end > cursor:
                lines.append({"start": round(cursor, 2), "end": round(end, 2), "text": segment})
            cursor = end
    return lines


def clip_timeline(
    lines: list[dict[str, Any]], trim_start: float, clip_duration: float
) -> list[dict[str, Any]]:
    """Shift a timeline by the edit trim offset and clip it to the final clip window.

    成片用 ``-ss trim_start`` 裁掉片头后，字幕时间轴必须整体前移；
    被裁剪点拦腰截断的句子，文本也要按比例跳过已说过的字——否则音频
    已经说到句子中间（例如裁掉开头 2 秒后第一句从"有个叫齐博士"说起），
    字幕却还从头显示整句（"最近有个现象……"）。超过一条字幕长度的
    长句同样按标点拆分并在句内按字数比例内插时间，避免整句字幕墙。
    """
    clip_end = trim_start + clip_duration
    clipped: list[dict[str, Any]] = []

    def slice_text(text: str, keep_ratio: float, align_start: bool) -> str:
        """按比例截取句中文本，并把切口对齐到最近的标点，避免残句。"""
        cut = max(0, min(len(text), int(round(len(text) * keep_ratio))))
        if align_start:
            for idx in range(cut, min(cut + 6, len(text))):
                if text[idx] in "，。！？、；":
                    cut = idx + 1
                    break
        else:
            for idx in range(cut - 1, max(cut - 7, 0), -1):
                if text[idx] in "。！？，、；":
                    cut = idx + 1
                    break
        return text[cut:] if align_start else text[:cut]

    for line in lines:
        start = float(line.get("start", 0))
        end = float(line.get("end", 0))
        text = str(line.get("text", "")).strip()
        if not text or end <= trim_start + 0.05 or start >= clip_end - 0.05:
            continue
        # 裁剪点落在句中：跳过已说过的字（按句内时长比例）
        if start < trim_start < end:
            ratio = (trim_start - start) / max(0.2, end - start)
            text = slice_text(text, ratio, align_start=True)
            start = trim_start
        # 成片结尾落在句中：只保留说到成片结束的字
        if start < clip_end < end:
            ratio = (clip_end - start) / max(0.2, end - start)
            text = slice_text(text, ratio, align_start=False)
            end = clip_end
        text = text.strip()
        if not text:
            continue
        # 超长句拆成多条短字幕，子行时长按字数比例内插
        pieces = split_subtitles(text)
        if len(pieces) > 1:
            span = max(0.2, end - start)
            total = sum(len(p) for p in pieces) or 1
            cursor = start
            for index, piece in enumerate(pieces):
                piece_end = end if index == len(pieces) - 1 else cursor + span * len(piece) / total
                piece_start = max(0.0, cursor - trim_start)
                piece_end_clamped = min(clip_duration, piece_end - trim_start)
                if piece_end_clamped - piece_start >= 0.15:
                    clipped.append({"start": round(piece_start, 2), "end": round(piece_end_clamped, 2), "text": piece.strip()})
                cursor = piece_end
        else:
            out_start = max(0.0, start - trim_start)
            out_end = min(clip_duration, end - trim_start)
            if out_end - out_start >= 0.15:
                clipped.append({"start": round(out_start, 2), "end": round(out_end, 2), "text": text})
    return clipped


def to_srt(lines: list[dict[str, Any]]) -> str:
    def stamp(seconds: float) -> str:
        milliseconds = max(0, int(seconds * 1000))
        hours, milliseconds = divmod(milliseconds, 3_600_000)
        minutes, milliseconds = divmod(milliseconds, 60_000)
        seconds_value, milliseconds = divmod(milliseconds, 1_000)
        return f"{hours:02}:{minutes:02}:{seconds_value:02},{milliseconds:03}"

    return "\n\n".join(
        f"{index}\n{stamp(line['start'])} --> {stamp(line['end'])}\n{line['text']}"
        for index, line in enumerate(lines, start=1)
    )


def split_subtitles(text: str, max_len: int = 12) -> list[str]:
    """Split a script into subtitle-sized chunks (<=max_len chars each).

    Prefers cutting at sentence terminators, then commas; falls back to hard
    cuts so a single subtitle never spans more than ``max_len`` characters —
    keeping the on-screen text short, sentence-by-sentence.

    竖屏口播单行 12 字以内观感最接近抖音爆款字幕：16 字在 1080 宽下
    一行塞满显得又宽又满，故默认从 16 降到 12。
    """
    segments: list[str] = []
    sentences = [s.strip() for s in re.split(r"(?<=[。！？!?；;])", text) if s.strip()]
    for sentence in sentences:
        if len(sentence) <= max_len:
            segments.append(sentence)
            continue
        parts = [p for p in re.split(r"(?<=[，、,:：])", sentence) if p]
        buffer = ""
        for part in parts:
            if len(buffer) + len(part) <= max_len:
                buffer += part
            else:
                if buffer:
                    segments.append(buffer)
                buffer = part
        if buffer:
            segments.append(buffer)
    return segments


def to_ass(
    lines: list[dict[str, Any]],
    *,
    font_size: int = 18,
    primary: str = "&H00FFFFFF&",
    outline: str = "&H90000000&",
    margin_v: int = 36,
    keywords: str = "",
    keyword_color: str = "&H00FFFF00&",
    title: str = "",
    title_style: str = "inline-cyan",
    title_color_name: str = "white",
    title_font_ratio: int = 18,
    title_position: str = "top",
    duration: float = 3600,
) -> str:
    """Build one ASS script for both title and subtitles.

    ``keywords`` is a comma-separated list.  Any keyword substring found in a
    subtitle line is wrapped in ASS override tags ({\\c...}...{\\c}) so it
    renders in ``keyword_color`` while the rest of the line keeps its style.
    ASS colors use &HBBGGRR& (BGR reversed).  Sizes are resolved relative to
    1080p and scaled by PlayResY for the actual video height.

    爆款字幕样式：加粗 + 粗描边（黑体大字冲击力）+ 每条字幕 120ms 淡入
    入场 + 关键词放大 1.15 倍，观感对齐抖音热门口播视频。
    """
    def ass_stamp(seconds: float) -> str:
        centiseconds = max(0, int(seconds * 100))
        hours, centiseconds = divmod(centiseconds, 3_600_00)
        minutes, centiseconds = divmod(centiseconds, 60_00)
        seconds_value, centiseconds = divmod(centiseconds, 100)
        return f"{hours}:{minutes:02}:{seconds_value:02}.{centiseconds:02}"

    keywords_list = [k.strip() for k in (keywords or "").split(",") if k.strip()]
    keyword_tags = None
    if keywords_list:
        # Escape ASS override tag characters in keywords before regex use.
        pattern = "|".join(re.escape(k) for k in keywords_list)
        keyword_tags = re.compile(pattern)

    # 旧项目可能同时保存句级和词级时间轴。两者时间重叠时，短词会叠在
    # 完整句子上（例如“ 三天 ”叠在“我花三天时间”上），视觉上像两套字幕。
    # 对有重叠且文本被另一条完整包含的短条，只保留较长的句级字幕。
    candidate_lines = [line for line in lines if str(line.get("text") or "").strip()]
    filtered_lines: list[dict[str, Any]] = []
    for index, line in enumerate(candidate_lines):
        text = str(line.get("text") or "").strip()
        start = float(line.get("start", 0) or 0)
        end = float(line.get("end", 0) or 0)
        covered = False
        for other_index, other in enumerate(candidate_lines):
            if index == other_index:
                continue
            other_text = str(other.get("text") or "").strip()
            other_start = float(other.get("start", 0) or 0)
            other_end = float(other.get("end", 0) or 0)
            overlap = min(end, other_end) - max(start, other_start)
            if len(other_text) > len(text) and text in other_text and overlap > 0.05:
                covered = True
                break
        if not covered:
            filtered_lines.append(line)

    def ass_escape(text: str) -> str:
        return text.replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}")

    def colorize(text: str, color: str) -> str:
        escaped = ass_escape(text).replace("\\\\N", r"\N")
        if not keyword_tags:
            return escaped
        return keyword_tags.sub(
            lambda match: f"{{\\c{color}\\fscx115\\fscy115}}{match.group(0)}{{\\c\\fscx100\\fscy100}}",
            escaped,
        )

    events: list[str] = []
    if title.strip():
        title_colors = {"white": "FFFFFF", "yellow": "FFFF00", "black": "111111", "red": "FF6B6B", "cyan": "4ECDC4"}
        title_primary = hex_to_ass(title_colors.get((title_color_name or "").lower(), (title_color_name or "FFFFFF")))
        title_keyword_color = hex_to_ass("4ECDC4")
        if title_style == "classic-yellow":
            title_primary = hex_to_ass("9D1717")
            title_keyword_color = hex_to_ass("FFFF00")
        elif title_style == "highlight-red":
            title_keyword_color = hex_to_ass("FFFF00")
        elif title_style == "soft-white":
            title_keyword_color = hex_to_ass("FF7C83")
        title_lines = [title.strip()[index:index + 10] for index in range(0, len(title.strip()), 10)][:3]
        if title_style in {"inline-cyan", "classic-yellow", "highlight-red"} and len(title_lines) > 1:
            first = colorize(title_lines[0], title_primary)
            second_color = title_keyword_color if title_style != "classic-yellow" else hex_to_ass("FFFFFF")
            second = f"{{\\c{second_color}}}{ass_escape(title_lines[1])}{{\\c}}"
            title_text = first + r"\N" + second
        else:
            title_text = colorize(r"\N".join(title_lines), title_primary)
        ratio = max(8, min(72, int(title_font_ratio or 18)))
        if title_position == "center":
            title_tag = r"{\an5\pos(960,540)}"
        elif title_position == "bottom":
            title_tag = r"{\an2\pos(960,970)}"
        else:
            title_tag = r"{\an8\pos(960,70)}"
        events.append(f"Dialogue: 1,0:00:00.00,{ass_stamp(max(0.1, duration))},Title,,0,0,0,,{title_tag}{title_text}")

    seen_events: set[tuple[float, float, str]] = set()
    for line in filtered_lines:
        raw_text = str(line.get("text") or "").strip()
        if not raw_text:
            continue
        # 某些旧项目的时间轴同时包含句级和重复的词级记录；完全相同的
        # 时间区间/文本若全部烧录，会在画面上叠出两套字幕。保留一条即可。
        event_key = (round(float(line.get("start", 0)), 2), round(float(line.get("end", 0)), 2), raw_text)
        if event_key in seen_events:
            continue
        seen_events.add(event_key)
        text = raw_text
        if keyword_tags:
            def _wrap(match: re.Match[str]) -> str:
                # 关键词：变色 + 放大 1.15 倍，视觉焦点更突出。
                return (
                    f"{{\\c{keyword_color}\\fscx115\\fscy115}}"
                    f"{match.group(0)}{{\\c\\fscx100\\fscy100}}"
                )
            text = keyword_tags.sub(_wrap, text)
        # 每条字幕入场淡入 120ms（\fad(120,0)），比硬切更顺滑。
        events.append(
            f"Dialogue: 0,{ass_stamp(line['start'])},{ass_stamp(line['end'])},Default,,0,0,0,,{{\\fad(120,0)}}{text}"
        )

    header = (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        "PlayResX: 1920\n"
        "PlayResY: 1080\n"
        "\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        # Bold=-1（加粗）+ Outline=4（粗描边）+ Shadow=1：大字冲击力，白字黑边是口播字幕的黄金组合。
        f"Style: Default,{_FONT_NAME},{font_size},{primary},{primary},{outline},&H80000000&,-1,0,0,0,100,100,0,0,1,4,1,2,20,20,{margin_v},134\n"
        f"Style: Title,{_FONT_NAME},{max(24, min(96, round(1080 / max(8, int(title_font_ratio or 18)))) )},&H00FFFFFF&,&H00FFFFFF&,&H90000000&,&H80000000&,-1,0,0,0,100,100,0,0,1,4,2,8,20,20,70,134\n"
        "\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    return header + "\n".join(events) + "\n"


def assess_lipsync_risk(video: Path) -> list[str]:
    """Conservative local preflight: warnings, never a claim that a clip is safe."""
    risks: list[str] = []
    try:
        import cv2

        capture = cv2.VideoCapture(str(video))
        frames = max(1, int(capture.get(cv2.CAP_PROP_FRAME_COUNT)))
        samples = [int(frames * part) for part in (0.15, 0.5, 0.85)]
        cascade = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
        seen: list[int] = []
        for frame_no in samples:
            capture.set(cv2.CAP_PROP_POS_FRAMES, frame_no)
            ok, frame = capture.read()
            if not ok:
                continue
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            faces = cascade.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=5)
            seen.append(len(faces))
            if len(faces) == 0:
                risks.append("未稳定检测到正脸：侧脸、遮挡或快速运动可能导致口型效果下降。")
            elif len(faces) > 1:
                risks.append("检测到多人：当前版本只会以主要人物为目标，其他人可能出现异常口型。")
            elif max(faces[0][2], faces[0][3]) < min(frame.shape[:2]) * 0.18:
                risks.append("人物脸部偏小：建议使用人物占画面更大的近景视频。")
        capture.release()
    except Exception:
        risks.append("无法完成本地人脸预检；请确认视频为单人正脸近景。")
    return list(dict.fromkeys(risks))


def run_job(job_id: str) -> None:
    job = store.get(job_id)
    work = JOBS_DIR / job_id
    try:
        pipeline = BailianPipeline()
        source = next(work.glob("source.*"))
        store.update(job, status="running", stage="处理中 8%", progress=8)
        audio = work / "source.wav"
        extract_audio(source, audio)
        store.update(job, stage="处理中 22%", progress=22)
        effective_asr = _resolve_asr_model(job.asr_model)
        transcript, _timeline = transcribe_for_model(audio, effective_asr, pipeline=pipeline)
        store.update(job, transcript=transcript, stage="处理中 42%", progress=42)
        rewritten = pipeline.rewrite(transcript, job.instruction)
        store.update(job, rewritten_text=rewritten, stage="处理中 56%", progress=56)
        voice_id = job.voice_id
        if job.voice_clone_model == "qwen3-tts-vc":
            if job.create_voice or not voice_id:
                voice_id, _fallback = pipeline.enroll_voice_qwen_vc(audio, job.id)
                store.update(job, voice_id=voice_id)
            if not voice_id:
                raise RuntimeError("未选择创建音色，也没有提供已有音色名。")
            store.update(job, stage="处理中 68%", progress=68)
            speech = work / "rewritten.wav"
            pipeline.synthesize_qwen_vc(rewritten, voice_id, speech)
        else:
            if job.create_voice:
                voice_id = pipeline.enroll_voice(audio, job.id)
                store.update(job, voice_id=voice_id)
            if not voice_id:
                raise RuntimeError("未选择创建音色，也没有提供已有 voice_id。")
            store.update(job, stage="处理中 68%", progress=68)
            speech_pcm = work / "rewritten.pcm"
            pipeline.synthesize(rewritten, voice_id, speech_pcm)
            speech = work / "rewritten.wav"
            pcm_to_wav(speech_pcm, speech)
        store.update(job, stage="处理中 82%", progress=82)
        # Cloud task supports up to 120 seconds. The UI validates before submitting.
        with VIDEO_LOCK:
            result_url = pipeline.lipsync(source, speech)
        output = work / "result.mp4"
        download(result_url, output)
        store.update(job, status="succeeded", stage="成片已保存", progress=100, output_name=output.name)
    except Exception as error:  # keep task failures visible in UI
        store.update(job, status="failed", stage="处理失败", error=str(error))


def run_extraction(job_id: str) -> None:
    job = store.get(job_id)
    work = JOBS_DIR / job.id
    try:
        source = work / "source.mp4"
        store.update(job, status="running", stage="处理中 20%", progress=20, current_step=1, error=None)
        seconds = duration_seconds(source)
        if seconds < 2:
            raise RuntimeError("视频过短，无法稳定处理口播内容。")
        store.update(job, stage="处理中 24%", progress=24, duration=round(seconds, 2), reference_duration=round(seconds, 2))
        audio = work / "source.wav"
        extract_audio(source, audio)
        # 参考视频只在取音频阶段需要；音频就绪后立即删除原片，工作目录不保留参考视频。
        safe_unlink(source)
        store.update(job, stage="处理中 45%", progress=45)
        effective_asr = _resolve_asr_model(job.asr_model)
        transcript, timeline = transcribe_for_model(audio, effective_asr, duration=seconds)
        if not timeline:
            timeline = timed_lines(transcript, seconds)
        subtitle = work / "source.srt"
        subtitle.write_text(to_srt(timeline), encoding="utf-8")
        store.update(
            job,
            transcript=transcript,
            timeline=timeline,
            subtitle_name=subtitle.name,
            audio_name=audio.name,
        )
        # 整理与改写在同一条路径上完成；未填写额外要求时也使用默认要求自动改写。
        run_rewrite(job_id, job.instruction or DEFAULT_REWRITE_INSTRUCTION, job.rewrite_model, start_progress=65)
        # 参考内容派生文件同样不留：source.wav 转写后无人使用；source.srt
        # 后续可随时从 job.transcript 重新生成（见 /transcript 接口）。
        safe_unlink(work / "source.wav")
        safe_unlink(work / "source.srt")
    except Exception as error:
        # 失败也同样不留参考痕迹：源片、原声、逐字稿与续传残留一并清理。
        safe_unlink(work / "source.mp4")
        safe_unlink(work / "source.mp4.part")
        safe_unlink(work / "source.wav")
        safe_unlink(work / "source.srt")
        store.update(job, status="failed", stage="处理失败", error=str(error))


def run_rewrite(job_id: str, instruction: str, rewrite_model: str, *, start_progress: int = 35) -> None:
    job = store.get(job_id)
    try:
        if not job.transcript:
            raise RuntimeError("请先完成第一步。")
        instruction = instruction.strip() or DEFAULT_REWRITE_INSTRUCTION
        custom_prov = _custom_provider(rewrite_model) or _ollama_provider(rewrite_model)
        service_chat = _service_connection_for_model(rewrite_model, "chat")
        # 与提取链路同口径：状态只显示百分比，不出现供应商与步骤描述。
        store.update(job, status="running", stage=f"处理中 {start_progress}%", progress=start_progress, current_step=2, error=None, instruction=instruction, rewrite_model=rewrite_model)
        if rewrite_model == "auto":
            rewritten = rewrite_auto(job.transcript, instruction, job.person_duration or None)
        elif custom_prov:
            rewritten = rewrite_with_compat(job.transcript, instruction, job.person_duration or None,
                                            config=(custom_prov["base_url"], custom_prov.get("api_key", ""), custom_prov.get("model") or "gpt-4o-mini"))
        elif service_chat:
            provider, _, selected = service_chat
            rewritten = rewrite_with_compat(job.transcript, instruction, job.person_duration or None,
                                            config=(provider["base_url"], provider.get("api_key", ""), selected))
        elif rewrite_model.startswith("mimo-v2.5"):
            rewritten = rewrite_with_mimo(job.transcript, instruction, rewrite_model, job.person_duration or None)
        else:
            rewritten = BailianPipeline().rewrite(job.transcript, instruction, job.person_duration or None)
        target_duration = job.person_duration
        estimated_seconds = round(len(rewritten) / 4.2, 1)
        if not target_duration:
            note = "新文案已生成；上传人物视频后将按人物视频时长校准。"
        elif estimated_seconds <= target_duration:
            note = "预计时长在人物视频范围内。"
        else:
            note = "预计配音超出人物视频时长；建议缩短文案或调快语速。"
        store.update(job, status="ready", stage=note, progress=100, rewritten_text=rewritten, script_confirmed=False, preview_confirmed=False)
    except Exception as error:
        store.update(job, status="failed", stage="改写失败", error=str(error))


def generate_script_text(
    prompt: str, model: str = "auto", timeout: float = 120,
    *, temperature: float = 0.65, max_tokens: int = 1600,
) -> str:
    """AI 写稿：可按设置指定模型，或在 auto 时按已配置的官方供应商选择。

    ``custom:<id>`` 和 ``local:ollama`` 都走 OpenAI 兼容接口；不会读取或展示他人的 Key。
    剪辑方案等结构化输出会收紧 ``temperature``/``max_tokens``，调度链路与写稿完全一致。
    """
    custom = _custom_provider(model) or _ollama_provider(model)
    service_chat = _service_connection_for_model(model, "chat")
    if custom:
        response = openai_compat_chat(
            base_url=custom["base_url"], api_key=custom.get("api_key", ""), model=custom.get("model") or "gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            timeout=timeout, temperature=temperature, max_tokens=max_tokens,
        )
        return _extract_chat_content(response, f"{custom.get('name') or '本地 Ollama'}（{custom.get('model') or 'gpt-4o-mini'}）")
    if service_chat:
        provider, _, selected = service_chat
        response = openai_compat_chat(
            base_url=provider["base_url"], api_key=provider.get("api_key", ""), model=selected,
            messages=[{"role": "user", "content": prompt}], timeout=timeout, temperature=temperature, max_tokens=max_tokens,
        )
        return _extract_chat_content(response, model_label(model))
    if model in {"auto", "mimo-v2.5"} and local_setting("MIMO_API_KEY"):
        response = mimo_post(
            {"model": "mimo-v2.5", "messages": [{"role": "user", "content": prompt}],
             "temperature": temperature, "max_tokens": max_tokens},
            timeout=timeout,
        )
        return _extract_chat_content(response, "MiMo")
    if model == "mimo-v2.5":
        raise RuntimeError("未配置 MiMo API Key，请到设置中填写你自己的 Key。")
    if model == "qwen3.7-flash":
        return BailianPipeline().generate_script(prompt, temperature=temperature, max_tokens=max_tokens)
    if model == "auto":
        return BailianPipeline().generate_script(prompt, temperature=temperature, max_tokens=max_tokens)
    raise RuntimeError("AI 文案创作暂不支持该模型。")


def run_ai_script(job_id: str, prompt: str, model: str) -> None:
    """Create a spoken script from a user's brief using the fast writer."""
    job = store.get(job_id)
    try:
        provider = _custom_provider(model) or _ollama_provider(model)
        store.update(job, status="running", stage="处理中 35%", progress=35, current_step=1, error=None)
        request = (
            "你是短视频口播文案策划。根据用户的需求创作一篇可直接朗读的中文口播稿。"
            "请给出有吸引力的开场、清晰的正文与自然的结尾；不要虚构事实、数据、经历、效果承诺或引用来源。"
            "输出只能包含口播正文，不要标题、说明、Markdown 或创作过程。\n\n"
            f"用户需求：\n{prompt.strip()}"
        )
        text = generate_script_text(request, model=model)
        work = JOBS_DIR / job.id
        lines = timed_lines(text, max(0.0, len(text) / 4.2))
        subtitle = work / "source.srt"
        subtitle.write_text(to_srt(lines), encoding="utf-8")
        store.update(
            job, status="ready", stage="AI 文案已生成，请确认或修改", progress=100,
            transcript=text, rewritten_text=text, timeline=lines, subtitle_name=subtitle.name,
            script_confirmed=False, current_step=2,
        )
    except Exception as error:
        store.update(job, status="failed", stage="AI 文案生成失败", error=str(error))


def run_person_video(job_id: str) -> None:
    job = store.get(job_id)
    work = JOBS_DIR / job.id
    try:
        source = work / "person.mp4"
        if not source.exists():
            raise RuntimeError("人物视频文件不存在。")
        store.update(job, status="running", stage="处理中 18%", progress=18, current_step=3, error=None, person_status="处理中 18%")
        seconds = duration_seconds(source)
        if seconds < 2 or seconds > MAX_VIDEO_SECONDS:
            raise RuntimeError(f"人物视频需在 2–{MAX_VIDEO_SECONDS} 秒之间。")
        audio = work / "person.wav"
        extract_audio(source, audio)
        risks = assess_lipsync_risk(source)
        store.update(
            job, status="ready", stage="人物视频已就绪", progress=100,
            person_video_name=source.name, person_audio_name=audio.name, person_duration=round(seconds, 2),
            person_risks=risks, person_status="人物视频已就绪", voice_id=None,
            preview_audio_name=None, preview_duration=0, preview_confirmed=False, output_name=None,
        )
    except Exception as error:
        store.update(job, status="failed", stage="人物视频处理失败", error=str(error), person_status="人物视频处理失败")


def run_voice_preview(
    job_id: str,
    mode: str,
    existing_voice_id: str | None,
    speed: str,
    emotion: str,
    voice_clone_model: str,
    direct_tts_model: str,
    fish_model: str,
    fish_style: str,
    fish_speed: float,
    fish_volume: float,
    fish_temperature: float,
    fish_top_p: float,
    fish_quality_guard: bool,
    reference_text: str,
    custom_voice: Path | None = None,
    voice_rate: float = 1.0,
    voice_volume: int = 50,
    voice_pitch: float = 1.0,
    voice_seed: int = 0,
    voice_lang: str = "auto",
    voice_instruction: str = "",
    direct_tts_voice: str = "",
    minimax_emotion: str = "",
    minimax_tts_model: str = "speech-2.8-hd",
    minimax_speed: float = 1.0,
    minimax_volume: float = 1.0,
    minimax_pitch: int = 0,
    minimax_language_boost: str = "auto",
    minimax_text_normalization: bool = False,
    minimax_latex_read: bool = False,
    minimax_pronunciation: list[str] | None = None,
    minimax_sound_effect: str = "",
    mimo_style: str = "",
) -> None:
    job = store.get(job_id)
    work = JOBS_DIR / job.id
    voice_clone_model = normalize_clone_model(voice_clone_model)
    try:
        text = job.rewritten_text.strip()
        if not text:
            raise RuntimeError("请先完成第二步文案改写。")
        if not job.script_confirmed:
            raise RuntimeError("请先确认新文案。")
        # Generate the complete preview first.  A longer source script is valid
        # The Lite workflow keeps timing explicit: users provide a sufficiently
        # long person video, and only a shorter voice track may trim its tail.
        # 只有「使用人物视频原声」才必须依赖人物视频；其他模式各用自己的前置条件，
        # 避免用户用直接配音/已有音色时还被"请上传人物视频"挡住。
        if mode == "original" and not job.person_audio_name:
            raise RuntimeError("请先上传人物视频并完成原声提取。")
        if mode == "upload" and not custom_voice:
            raise RuntimeError("请上传新的声音样音。")
        if mode == "saved" and not (existing_voice_id or "").strip():
            raise RuntimeError("请选择已保存的音色 ID。")
        if voice_clone_model == "mimo-v2.5-tts-voiceclone" and mode == "saved":
            raise RuntimeError("MiMo 声音复刻需要人物原声或上传样音；已保存的 voice_id 请改用阿里云 CosyVoice 或 Fish Audio。")
        if voice_clone_model == "qwen3-tts-local-base" and mode == "saved":
            raise RuntimeError("本地 Qwen3-TTS 声音复刻当前需要上传样音；生成后的 WAV 可直接复用。")
        direct_tts_service = _service_connection_for_model(direct_tts_model, "tts")
        if mode == "direct" and direct_tts_model not in DIRECT_TTS_MODELS and not direct_tts_service:
            raise RuntimeError("未知的直接配音模型。")
        if mode == "direct" and direct_tts_model in DIRECT_TTS_VOICES:
            voices = DIRECT_TTS_VOICES[direct_tts_model]
            direct_tts_voice = direct_tts_voice.strip() or next(iter(voices))
            if direct_tts_voice not in voices:
                raise RuntimeError("所选标准音色不可用，请重新选择。")
        stage = f"{model_label(direct_tts_model)} 正在生成试听" if mode == "direct" else "准备音色与试听"
        store.update(
            job,
            status="running",
            stage=stage,
            progress=35,
            current_step=3,
            error=None,
            voice_mode=mode,
            voice_speed=speed,
            voice_emotion=emotion,
            voice_rate=voice_rate,
            voice_volume=voice_volume,
            voice_pitch=voice_pitch,
            voice_seed=voice_seed,
            voice_lang=voice_lang,
            voice_instruction=voice_instruction,
            voice_clone_model=voice_clone_model,
            direct_tts_model=direct_tts_model,
            fish_model=fish_model,
            fish_style=fish_style,
            fish_speed=fish_speed,
            fish_volume=fish_volume,
            fish_temperature=fish_temperature,
            fish_top_p=fish_top_p,
            fish_quality_guard=fish_quality_guard,
            reference_text=reference_text,
            voice_quality_note=None,
        )
        preview = work / "preview.wav"
        if voice_clone_model in REMOVED_CLONE_MODELS:
            raise RuntimeError("该复刻通道已在轻量版移除，请在“设置 → 模型分配”中改用阿里云或小米的复刻模型。")
        if voice_clone_model == "mimo-v2.5-tts-voiceclone" and mode != "direct":
            sample = custom_voice if mode == "upload" and custom_voice else work / job.person_audio_name
            store.update(job, stage="处理中 62%", progress=62)
            synthesize_voiceclone_with_mimo(text, sample, preview, emotion, style=mimo_style)
        elif mode == "direct" and direct_tts_service:
            provider, _, selected = direct_tts_service
            store.update(job, stage="处理中 62%", progress=62)
            synthesize_with_openai_compat(text, preview, provider, selected)
        elif mode == "direct" and direct_tts_model == "mimo-v2.5-tts":
            store.update(job, stage="处理中 62%", progress=62)
            synthesize_with_mimo(text, preview, voice=direct_tts_voice)
        elif mode == "direct" and direct_tts_model == "qwen-builtin-tts":
            store.update(job, stage="处理中 62%", progress=62)
            BailianPipeline().synthesize_builtin_tts(text, preview, speed=speed, voice=direct_tts_voice)
        elif mode == "direct" and direct_tts_model == "qwen3-tts-local-customvoice":
            store.update(job, stage="Qwen3-TTS 本地内置音色生成中", progress=62)
            ensure_local_qwen3_tts_runtime()
            LocalQwenTTS(_LOCAL_SETTINGS).synthesize_custom_voice(
                text,
                preview,
                voice=direct_tts_voice or "Vivian",
                language=voice_lang,
                instruction=voice_instruction,
            )
        elif voice_clone_model == "qwen3-tts-vc" and mode != "direct":
            pipeline = BailianPipeline()
            reference_hash: str | None = None
            if mode == "saved":
                voice_id = existing_voice_id
            else:
                sample = custom_voice if mode == "upload" and custom_voice else work / job.person_audio_name
                reference_hash = file_sha256(sample)
                # 同一段样音不重复注册：hash 一致直接复用已保存的音色名。
                voice_id = job.voice_id if job.voice_id and job.voice_reference_hash == reference_hash else None
            if not voice_id:
                store.update(job, stage="处理中 52%", progress=52)
                voice_id, fallback_reason = pipeline.enroll_voice_qwen_vc(sample, job.id)
                if fallback_reason:
                    store.update(job, voice_quality_note="样音质量欠佳，已降级处理，建议更换更清晰的样音。")
            store.update(
                job,
                stage="处理中 72%",
                progress=72,
                voice_id=voice_id,
                voice_reference_hash=reference_hash if mode != "saved" else job.voice_reference_hash,
            )
            # 裸参数合成：试听就是成片配音，preview.wav 直接进入后续改口型流程。
            pipeline.synthesize_qwen_vc(text, voice_id, preview, language=voice_lang)
        elif voice_clone_model == "cosyvoice-v3.5-plus" and mode != "direct":
            pipeline = BailianPipeline()
            reference_hash: str | None = None
            if mode == "saved":
                voice_id = existing_voice_id
            else:
                sample = custom_voice if mode == "upload" and custom_voice else work / job.person_audio_name
                reference_hash = file_sha256(sample)
                voice_id = job.voice_id if job.voice_id and job.voice_reference_hash == reference_hash else None
            if not voice_id:
                store.update(job, stage="处理中 52%", progress=52)
                voice_id = pipeline.enroll_voice(sample, job.id)
            store.update(
                job,
                stage="处理中 72%",
                progress=72,
                voice_id=voice_id,
                voice_reference_hash=reference_hash or job.voice_reference_hash,
            )
            pipeline.synthesize(
                text,
                voice_id,
                preview,
                speed=speed,
                emotion=emotion,
                rate=voice_rate,
                volume=voice_volume,
                pitch=voice_pitch,
                seed=voice_seed,
                language=voice_lang,
                instruction=voice_instruction,
            )
        elif voice_clone_model == "qwen3-tts-local-base" and mode != "direct":
            sample = custom_voice if mode == "upload" and custom_voice else work / job.person_audio_name
            if not sample or not sample.is_file():
                raise RuntimeError("本地 Qwen3-TTS 声音复刻找不到参考样音。")
            store.update(job, stage="Qwen3-TTS 本地声音复刻中", progress=62)
            ensure_local_qwen3_tts_runtime()
            LocalQwenTTS(_LOCAL_SETTINGS).synthesize_clone(
                text,
                sample,
                preview,
                reference_text=reference_text,
                language=voice_lang,
            )
        else:
            raise RuntimeError("未知的声音复刻模型，请在“设置 → 模型分配”中重新选择。")
        preview_duration = round(duration_seconds(preview), 2)
        # ── 字幕对齐：对刚生成的配音跑 ASR，拿真实逐句时间轴 ──
        # 成片音频就是这段配音，字幕用它的时间轴才能和说话对上。
        # 对齐失败不阻塞配音流程，且**不再写入字数估算的假时间轴**——
        # 那会在渲染时挡住静音检测等更准的兜底（历史 bug：估算的长句
        # 让整个成片只显示一面 85 字的字幕墙）。
        voice_timeline: list[dict[str, Any]] = []
        try:
            store.update(job, stage="处理中 92%", progress=92)
            voice_timeline = sentences_to_subtitles(subtitle_asr_timeline(preview))
        except Exception:
            # Subtitle alignment is an optional enhancement.  Do not write to
            # stdout here: a Windows GUI/background launch may have no valid
            # console handle, turning this recoverable ASR error into Errno 22
            # and incorrectly marking a successfully generated preview failed.
            voice_timeline = []
        delta = round(preview_duration - job.person_duration, 2)
        if not job.person_duration:
            store.update(job, status="ready", stage="试听音频已就绪", progress=100, preview_audio_name=preview.name, preview_duration=preview_duration, duration_delta=0, duration_status="系统配音已生成；上传人物视频后将检查时长并生成改口型视频。", preview_confirmed=False, voice_timeline=voice_timeline)
            return
        tolerance = max(0.8, job.person_duration * 0.05)
        if abs(delta) <= tolerance:
            duration_status = "时长匹配，可直接生成改口型视频。"
        elif delta < 0:
            duration_status = f"新配音比人物视频短 {abs(delta):.1f} 秒；可保留片尾或裁掉尾部。"
        elif preview_duration <= MAX_VIDEO_SECONDS:
            duration_status = f"新配音长 {delta:.1f} 秒；生成时可裁切配音到视频长度，或冻结片尾、循环人物视频补足。"
        else:
            duration_status = f"新配音长 {delta:.1f} 秒，超过人物视频可用时长；请先校准。"
        store.update(job, status="ready", stage="试听音频已就绪，请确认试听", progress=100, preview_audio_name=preview.name, preview_duration=preview_duration, duration_delta=delta, duration_status=duration_status, preview_confirmed=False, voice_timeline=voice_timeline)
    except Exception as error:
        store.update(job, status="failed", stage="声音生成失败", error=str(error))


def run_video_generation(job_id: str) -> None:
    job = store.get(job_id)
    work = JOBS_DIR / job.id
    try:
        if not job.preview_audio_name:
            raise RuntimeError("请先生成并确认第三步试听音频。")
        if not job.preview_confirmed:
            raise RuntimeError("请先确认声音试听。")
        if not job.person_video_name:
            raise RuntimeError("请先上传人物视频。")
        if job.preview_duration > MAX_VIDEO_SECONDS:
            raise RuntimeError(f"当前配音超过本版本支持的 {MAX_VIDEO_SECONDS} 秒上限，请重新生成较短配音后再试。")
        tolerance = max(0.8, job.person_duration * 0.05)
        if job.duration_delta > tolerance:
            raise RuntimeError("新配音比人物视频长，请重新生成较短配音或准备更长的人物视频。")
        store.update(job, status="running", stage="处理中 18%", progress=18, current_step=4, error=None)
        source = work / job.person_video_name
        audio = work / job.preview_audio_name
        if job.duration_strategy == "trim_tail" and job.duration_delta < -0.8:
            trimmed = work / "person-trimmed.mp4"
            subprocess.run([ffmpeg_path(), "-y", "-i", str(source), "-t", str(job.preview_duration), "-c", "copy", str(trimmed)], capture_output=True, check=True)
            source = trimmed
        # VideoRetalk requires a normalized H.264/30fps input.  Local
        # MuseTalk/HeyGem installations own their preprocessing rules, so the
        # original user file is passed through unchanged.
        if job.lipsync_model == "videoretalk":
            normalized = work / "videoretalk-input.mp4"
            store.update(job, stage="正在适配人物视频格式", progress=12)
            normalize_videoretalk_video(source, normalized)
            source = normalized
        store.update(job, stage="处理中 48%", progress=48)
        with VIDEO_LOCK:
            cancelled = lambda: store.get(job_id).cancel_requested
            progress = lambda value: store.update(job, stage=f"{model_label(job.lipsync_model)} 生成中 {value}%（可能需要一些时间，请耐心等待）", progress=value)
            if job.lipsync_model == "videoretalk":
                result_url = BailianPipeline().lipsync(source, audio, cancelled=cancelled, progress=progress)
                output = work / "result.mp4"
                store.update(job, stage="处理中 85%", progress=85)
                download(result_url, output)
            else:
                output = work / "result.mp4"
                if job.lipsync_model == "musetalk-1.5":
                    store.update(job, stage="正在启动本机 MuseTalk 引擎", progress=35)
                    ensure_local_musetalk_runtime()
                provider = provider_for(job.lipsync_model, _LOCAL_SETTINGS, ffmpeg=ffmpeg_path())
                provider.generate(source, audio, output, cancelled=cancelled, progress=progress)
        if store.get(job_id).cancel_requested:
            store.update(job, status="ready", stage="改口型任务已取消，已保留人物视频、文案和试听。", progress=0)
            return
        store.update(job, status="ready", stage="口型视频已生成，可进入剪辑", progress=100, output_name=output.name, current_step=5)
    except Exception as error:
        if job.cancel_requested:
            store.update(job, status="ready", stage="改口型任务已取消，已保留人物视频、文案和试听。", progress=0, error=None)
        else:
            store.update(job, status="failed", stage="视频生成失败", error=str(error))


def _parse_style_json(raw: str) -> dict[str, Any]:
    text = raw.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    match = re.search(r"```(?:json)?\s*\n(.*?)\n```", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1).strip())
        except json.JSONDecodeError:
            pass
    first, last = text.find("{"), text.rfind("}")
    if first != -1 and last > first:
        try:
            return json.loads(text[first:last + 1])
        except json.JSONDecodeError:
            pass
    raise RuntimeError("AI 返回的样式方案无法解析为 JSON。")


def run_auto_edit(job_id: str, locked: str = "") -> None:
    """AI 一键成片：DeepSeek 通读文案敲定 04/05 的剪辑决策，然后直接渲染导出。

    locked 为逗号分隔的字段名列表——用户手动调整过的设置不覆盖，AI 只补齐剩下的。
    AI 决策失败时使用保守默认方案继续出片——全自动流程不允许因 AI 抖动而中断。
    """
    job = store.get(job_id)
    locked_fields = {name.strip() for name in locked.split(",") if name.strip()}
    try:
        if not job.output_name:
            raise RuntimeError("请先完成第四步视频生成。")
        work = JOBS_DIR / job.id
        text = (job.rewritten_text or job.transcript or "").strip()[:600]
        if not text:
            raise RuntimeError("请先完成文案创作。")
        clip_duration = effective_clip_duration(job, work)
        source = work / job.output_name
        # 与烧字幕口径一致：先拿（或现算）真实时间轴，字幕文本只取成片窗口里的实际话。
        voice_timeline = _source_voice_timeline(job, work, source)
        spoken = (
            "".join(line.get("text", "") for line in clip_timeline(voice_timeline, job.trim_start, clip_duration))
            if voice_timeline else ""
        )
        subtitle_text = (spoken or text)[: max(20, int(clip_duration * 4.2))]
        # 多段 B-roll：AI 决策作用于全部已上传片段（每段一个时间窗）。
        # 兼容旧单值字段：broll_clips 为空但 broll_name 存在时先迁移成一条 clip。
        if not job.broll_clips and job.broll_name:
            legacy_path = work / job.broll_name
            if legacy_path.exists():
                job.broll_clips = [BrollClip(
                    name=job.broll_name,
                    start=float(job.broll_start or 0),
                    duration=float(job.broll_duration or 4),
                    enabled=True,
                )]
        broll_names = [c.name for c in job.broll_clips]
        has_broll = bool(broll_names) and all((work / n).exists() for n in broll_names)
        if broll_names and not has_broll:
            # 记录里有但磁盘文件丢失（例如历史上传后被清理）时，保持记录与磁盘一致，
            # 避免界面上显示“已添加”却导出时静默跳过。
            store.update(job, broll_clips=[], broll_name=None, broll_enabled=False)
        has_music = bool(job.music_name)
        store.update(job, status="running", stage="处理中 25%", progress=25, current_step=5, error=None)

        clip_count = len(job.broll_clips) if has_broll else 0
        prompt = (
            "你是资深短视频剪辑师。根据口播文案输出一份“一键成片”剪辑决策。\n\n"
            f"文案：\n{text}\n\n"
            f"成片约 {clip_duration:.1f} 秒，竖屏口播视频，字幕只显示这段：\n"
            f"「{subtitle_text}」\n\n"
            f"可支配素材："
            + (
                f"已上传 {clip_count} 段 B-roll 素材（需为每段决定插入时间与时长，broll_windows 数组必须正好 {clip_count} 个元素）"
                if has_broll else "无 B-roll 素材（broll_windows 填空数组）"
            )
            + f"；{'已上传背景音乐' if has_music else '无背景音乐（music_volume 填 0.14）'}。\n\n"
            "输出严格 JSON（不要 Markdown，不要解释）：\n"
            "{\n"
            '  "title": "不超过12字的吸睛标题",\n'
            '  "title_color": "white 或 yellow 或 #FF6B6B 或 #4ECDC4",\n'
            '  "title_font_size": "h/14 或 h/18 或 h/24",\n'
            '  "title_position": "top（默认，避免遮挡人物面部）",\n'
            '  "subtitle_keywords": ["从字幕文本中选2-3个实际出现的词"],\n'
            '  "subtitle_keyword_color": "FFFF00 或 FF6B6B 或 4ECDC4",\n'
            '  "sticker": "不超过6字的氛围小贴纸，不需要则填空字符串",\n'
            '  "cover_text": "封面大字，不超过10字",\n'
            '  "music_volume": 0.1 到 0.2 之间的小数,\n'
            '  "broll_windows": [{"start": 数字（秒，0到成片时长之间）, "duration": 数字（秒，1到5之间）}]\n'
            "}\n"
            "要求：颜色与文案主题匹配（干货类黄色、情感类暖红、科技类青色）；"
            "subtitle_keywords 必须从上面字幕文本中选择实际出现的词语。\n"
            + (
                "B-roll 插入点分散在不同时间段（如成片前/中/后各插一段），"
                "覆盖文案的转折点或关键论据处；各段时间窗不要相互重叠。\n"
                if has_broll else ""
            )
            + "\n标题与封面文案创作公式（必须套用，禁止平铺直叙地复述文案）：\n"
            "1. 数字法：具体数字制造可信感，如「3个方法」「月省2000块」\n"
            "2. 悬念法：留一半不说完，如「90%的人不知道」「最后一步最关键」\n"
            "3. 反差法：制造认知冲突，如「越便宜越坑」「别再交智商税」\n"
            "4. 利益法：直给用户能得到什么，如「看完少走3年弯路」「新手也能日入500」\n"
            "5. 禁忌法：劝阻式表达，如「千万别这样做」「这3种人千万别碰」\n"
            "title 用公式1-5之一；cover_text 是封面大字，要比 title 更短更狠，"
            "优先用反差或数字，控制在4-8字（如「劝你别学」「月省2千」）。"
        )
        decision: dict[str, Any] = {}
        try:
            plan_text = generate_script_text(
                prompt, model=selected_model("edit_plan"), timeout=60, temperature=0, max_tokens=500,
            )
            decision = _parse_style_json(plan_text)
        except Exception:
            decision = {}

        # ── 两段式标题优化：第一段决策完成后，若拿到 title/cover_text，再让 AI
        # 以较高温度生成 3 个候选并挑选最优，避免单次低温调用的平庸输出。──
        if decision.get("title") or decision.get("cover_text"):
            try:
                refine_prompt = (
                    "你是短视频爆款标题专家。下面是一份口播文案和一版候选标题/封面文案。\n\n"
                    f"文案：\n{text}\n\n"
                    f"候选标题：「{decision.get('title', '')}」 候选封面大字：「{decision.get('cover_text', '')}」\n\n"
                    "请按爆款公式（数字/悬念/反差/利益/禁忌）重新生成 3 组候选，每组 JSON 字段："
                    '{"title": "不超过12字", "cover_text": "封面大字4-8字"}。\n'
                    "要求：title 悬念感强、有点击欲；cover_text 比 title 更短更狠、视觉冲击力强。"
                    "输出严格 JSON 数组（不要 Markdown，不要解释）："
                    '[{"title": "...", "cover_text": "..."}, ...] ，共 3 个元素。'
                )
                refine_text = generate_script_text(
                    refine_prompt, model=selected_model("edit_plan"), timeout=60, temperature=0.8, max_tokens=400,
                )
                raw = refine_text.strip()
                start_idx, end_idx = raw.find("["), raw.rfind("]")
                candidates: list[dict[str, Any]] = []
                if start_idx != -1 and end_idx > start_idx:
                    parsed = json.loads(raw[start_idx:end_idx + 1])
                    if isinstance(parsed, list):
                        candidates = [c for c in parsed if isinstance(c, dict)]
                # 让同一模型从候选里挑最优（含原方案作第 4 选项）；挑选失败就保持第一段结果。
                if candidates:
                    original = {"title": str(decision.get("title", "")), "cover_text": str(decision.get("cover_text", ""))}
                    pick_prompt = (
                        "你是短视频运营总监。从以下候选标题方案中选出最适合抖音/视频号传播的一组。\n\n"
                        f"文案开头：{text[:120]}\n\n"
                        "候选（JSON 数组）：\n"
                        f"{json.dumps([original] + candidates, ensure_ascii=False)}\n\n"
                        "输出严格 JSON（不要解释）：{\"pick\": 序号从0开始}，0 表示原方案。"
                    )
                    pick_text = generate_script_text(
                        pick_prompt, model=selected_model("edit_plan"), timeout=30, temperature=0, max_tokens=30,
                    )
                    pick_match = re.search(r"\d+", pick_text)
                    if pick_match:
                        pick = int(pick_match.group())
                        pool = [original] + candidates
                        if 0 <= pick < len(pool):
                            chosen = pool[pick]
                            if str(chosen.get("title", "")).strip():
                                decision["title"] = str(chosen["title"]).strip()
                            if str(chosen.get("cover_text", "")).strip():
                                decision["cover_text"] = str(chosen["cover_text"]).strip()
            except Exception as error:
                safe_log(f"[auto_edit] 标题两段式优化失败，保留第一段结果：{type(error).__name__}: {error}")

        # ── 应用决策（AI 缺字段时用保守默认值，保证一定能出片）──
        # 兜底标题：取文案第一个完整短句（截到标点）而非硬截 14 字，
        # 避免「有个叫齐博士的」这种残句标题。
        first_sentence = re.split(r"[。！？!?；;]", text, maxsplit=1)[0]
        first_sentence = re.sub(r"[，、,:\s]+$", "", first_sentence).strip()
        fallback_title = (first_sentence[:14] if first_sentence else "") or "精彩口播"
        colors = {"white", "yellow", "#FF6B6B", "#4ECDC4", "black"}
        updates: dict[str, Any] = {
            "title": str(decision.get("title") or fallback_title)[:20],
            "title_color": decision.get("title_color") if decision.get("title_color") in colors else "white",
            "title_font_size": decision.get("title_font_size") if decision.get("title_font_size") in {"h/14", "h/18", "h/24"} else "h/18",
            "title_position": decision.get("title_position") if decision.get("title_position") in {"top", "center", "bottom"} else "top",
            # 字幕主体固定白色 + 偏大字号（与 AI 样式面板口径一致），AI 只决定高亮色。
            "subtitle_enabled": True,
            "subtitle_color": "FFFFFF",
            "subtitle_font_size": 42,
            "subtitle_keywords": "",
            "subtitle_keyword_color": decision.get("subtitle_keyword_color") if decision.get("subtitle_keyword_color") in {"FFFF00", "FF6B6B", "4ECDC4"} else "FFFF00",
            "sticker": str(decision.get("sticker") or "").strip()[:12],
            "cover_text": str(decision.get("cover_text") or fallback_title[:10])[:14],
            "music_volume": 0.14,
            # 没上传 B-roll 素材就不开启开关，避免界面上“已启用”的假象。
            "broll_enabled": bool(has_broll),
        }
        if isinstance(decision.get("subtitle_keywords"), list):
            spoken_pool = subtitle_text
            keywords = [
                str(k).strip() for k in decision["subtitle_keywords"]
                if str(k).strip() and str(k).strip() in spoken_pool
            ]
            updates["subtitle_keywords"] = ",".join(dict.fromkeys(keywords[:5]))
        if has_music:
            try:
                updates["music_volume"] = max(0.02, min(0.5, float(decision.get("music_volume", 0.14))))
            except (TypeError, ValueError):
                updates["music_volume"] = 0.14
        if has_broll:
            # AI 为每段 B-roll 输出时间窗（broll_windows 数组，与片段一一对应）；
            # 数组缺失/元素不足时退回均匀分布，保证每段都有合理插入点。
            windows = decision.get("broll_windows")
            if not isinstance(windows, list) or len(windows) < len(job.broll_clips):
                windows = []
            new_clips: list[BrollClip] = []
            for i, clip in enumerate(job.broll_clips):
                win = windows[i] if i < len(windows) and isinstance(windows[i], dict) else {}
                try:
                    start = max(0.0, min(float(win.get("start", 0)), max(0.0, clip_duration - 0.5)))
                except (TypeError, ValueError):
                    # 兜底：按段序均匀分布（第 1 段在 1/4 处，之后依次后移）。
                    start = min(clip_duration * (0.2 + 0.25 * i), max(0.0, clip_duration - 1.0))
                try:
                    duration = max(0.5, min(float(win.get("duration", 3)), min(8.0, clip_duration - start)))
                except (TypeError, ValueError):
                    duration = min(3.0, max(0.5, clip_duration - start))
                new_clips.append(BrollClip(
                    name=clip.name, start=round(start, 2), duration=round(duration, 2), enabled=True,
                    title=clip.title, mode=clip.mode, pip_position=clip.pip_position,
                    pip_scale=clip.pip_scale, pip_margin=clip.pip_margin,
                ))
            if "broll_clips" not in locked_fields:
                updates["broll_clips"] = new_clips
                updates["broll_start"] = new_clips[0].start
                updates["broll_duration"] = new_clips[0].duration
        # 用户手动调整过的字段一律保留，只让 AI 补齐剩下的。
        updates = {key: value for key, value in updates.items() if key not in locked_fields}
        store.update(job, **updates)
        render_edit(job_id)
    except Exception as error:
        store.update(job, status="failed", stage="一键成片失败", error=str(error))


IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"}


def is_image_path(path: Path) -> bool:
    """按文件后缀判断是否为静态图片（用于 B-roll 输入方式选择）。"""
    return path.suffix.lower() in IMAGE_SUFFIXES


def broll_input_args(path: Path) -> list[str]:
    """B-roll 输入参数：图片用 -loop 1 把单帧循环成视频流，视频用 -stream_loop -1。"""
    return ["-loop", "1"] if is_image_path(path) else ["-stream_loop", "-1"]


def video_dimensions(path: Path) -> tuple[int, int]:
    """读取视频首个画面尺寸；失败时返回 (0, 0)。"""
    try:
        ffprobe = Path(ffmpeg_path()).with_name("ffprobe.exe")
        result = subprocess.run(
            [str(ffprobe), "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height", "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, check=True,
        )
        width, height = (result.stdout.strip() or "").split(",")[:2]
        return int(width), int(height)
    except (OSError, ValueError, subprocess.CalledProcessError):
        return 0, 0


def build_layout_filter(
    *,
    background_type: str,
    foreground_scale: float,
    foreground_position: str,
    foreground_margin: int,
    background_color: str,
    blur_strength: int,
    main_w: int,
    main_h: int,
    background_input: int | None = None,
) -> str:
    """构建主体视频画中画布局，输出 ``[layout]`` 视频流。

    输入 0 是主体口播视频；background_input（如果有）是本地 B-roll 或图片。
    标题、字幕等包装 filter 由调用方继续放在布局之上。
    """
    if not main_w or not main_h:
        raise RuntimeError("无法读取主体口播视频尺寸，无法生成画中画布局。")
    scale = max(0.15, min(0.95, float(foreground_scale or 0.32)))
    margin = max(0, min(300, int(foreground_margin or 24)))
    foreground_width = max(2, int(main_w * scale) // 2 * 2)
    position_map = {
        "top-left": f"{margin}:{margin}",
        "top-right": f"W-w-{margin}:{margin}",
        "bottom-left": f"{margin}:H-h-{margin}",
        "bottom-right": f"W-w-{margin}:H-h-{margin}",
        "center": "(W-w)/2:(H-h)/2",
    }
    overlay_xy = position_map.get(foreground_position, position_map["bottom-right"])
    parts: list[str] = []
    if background_type == "color":
        color = background_color.strip().lstrip("#") or "202020"
        parts.append(f"color=c=0x{color}:s={main_w}x{main_h}:r=30[layoutbg];")
        foreground = "[0:v]setpts=PTS-STARTPTS,"
    elif background_type == "blur":
        radius = max(2, min(40, int(blur_strength or 18)))
        parts.append(
            f"[0:v]setpts=PTS-STARTPTS,split=2[layoutblur][layoutfgsrc];"
            f"[layoutblur]scale={main_w}:{main_h}:force_original_aspect_ratio=increase,"
            f"crop={main_w}:{main_h},boxblur=luma_radius={radius}:luma_power=1[layoutbg];"
        )
        foreground = "[layoutfgsrc]"
    else:
        if background_input is None:
            raise RuntimeError("画中画背景素材尚未选择。")
        parts.append(
            f"[{background_input}:v]setpts=PTS-STARTPTS,"
            f"scale={main_w}:{main_h}:force_original_aspect_ratio=increase,"
            f"crop={main_w}:{main_h}[layoutbg];"
        )
        foreground = "[0:v]setpts=PTS-STARTPTS,"
    parts.append(
        f"{foreground}scale={foreground_width}:-2:force_original_aspect_ratio=decrease,setsar=1[layoutfg];"
        f"[layoutbg][layoutfg]overlay={overlay_xy}:shortest=1[layout]"
    )
    return "".join(parts)


def build_broll_filter(
    clips: list[tuple[Any, ...]],
    visual_filter: str,
    main_w: int = 0,
    main_h: int = 0,
    base_stream: str = "0:v",
) -> str:
    """构建多段 B-roll overlay 的 filter_complex。

    ``clips`` 中每项至少是 (素材输入流序号, 插入时间点, 时长)，也可继续提供
    (mode, position, scale, margin) 画中画参数。默认 mode=replace，保证旧项目
    的 filter 调用和已保存任务保持原行为。``base_stream`` 可传入已经完成
    画中画布局的 ``layout`` 流；各素材按各自时间窗链式叠加到该底图上；
    字幕/标题等 visual_filter 最后应用，保证文字永远在最上层。
    """
    parts: list[str] = []
    # 当前承载主画面的流标签：默认从 0:v 开始，每叠一段换一个标签。
    current = base_stream
    for i, clip in enumerate(clips):
        input_index, start, duration = clip[:3]
        mode = str(clip[3] if len(clip) > 3 else "replace")
        position = str(clip[4] if len(clip) > 4 else "bottom-right")
        try:
            scale = max(0.1, min(0.8, float(clip[5] if len(clip) > 5 else 0.32)))
        except (TypeError, ValueError):
            scale = 0.32
        try:
            margin = max(0, min(300, int(clip[6] if len(clip) > 6 else 24)))
        except (TypeError, ValueError):
            margin = 24
        end = start + duration
        scaled = f"brollscaled{i}"
        overlaid = f"brollmixed{i}"
        if mode == "pip":
            # 画中画只缩放到主画面的一个比例，保持原始宽高比，不改变主画面尺寸。
            pip_width = str(max(1, int(main_w * scale))) if main_w else f"iw*{scale:.3f}"
            parts.append(
                f"[{input_index}:v]scale={pip_width}:-2:force_original_aspect_ratio=decrease,"
                f"setsar=1[{scaled}];"
            )
            position_map = {
                "top-left": f"{margin}:{margin}",
                "top-right": f"W-w-{margin}:{margin}",
                "bottom-left": f"{margin}:H-h-{margin}",
                "bottom-right": f"W-w-{margin}:H-h-{margin}",
                "center": "(W-w)/2:(H-h)/2",
            }
            overlay_xy = position_map.get(position, position_map["bottom-right"])
            parts.append(
                f"[{scaled}]trim=duration={duration:.3f},setpts=PTS-STARTPTS+{start:.3f}/TB[broll{i}];"
                f"[{current}][broll{i}]overlay={overlay_xy}:enable='between(t,{start:.3f},{end:.3f})'[{overlaid}];"
            )
        else:
            if main_w and main_h:
                # contain：保持宽高比完整显示整张图/视频，居中加黑边。
                # 之前用 scale2ref 直接拉伸到主画面尺寸，横版图片会被压扁变形，
                # 看起来像"只显示了图片的一部分"。
                parts.append(
                    f"[{input_index}:v]scale={main_w}:{main_h}:force_original_aspect_ratio=decrease,"
                    f"pad={main_w}:{main_h}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1[{scaled}];"
                )
            else:
                parts.append(
                    f"[{input_index}:v][{current}]scale2ref=w=main_w:h=main_h[{scaled}][brollbase{i}];"
                )
                current = f"brollbase{i}"
            parts.append(
                f"[{scaled}]trim=duration={duration:.3f},setpts=PTS-STARTPTS+{start:.3f}/TB[broll{i}];"
                f"[{current}][broll{i}]overlay=0:0:enable='between(t,{start:.3f},{end:.3f})'[{overlaid}];"
            )
        current = overlaid
    parts.append(f"[{current}]{visual_filter}[video]")
    return "".join(parts)


def _whisper_timeline(audio: Path) -> list[dict[str, Any]]:
    """本机 faster-whisper 兜底 ASR：返回 [{start,end,text}, ...]。

    不依赖云端，速度取决于 CPU/GPU。第一次调用会下载 small 模型（~460MB）。
    失败/未安装时返回空列表。
    """
    try:
        from faster_whisper import WhisperModel  # type: ignore
    except Exception:
        return []
    try:
        # int8 量化模型在 CPU 上够用；用 beam_size=1 加速。
        model = WhisperModel("small", device="cpu", compute_type="int8")
        segments_iter, _info = model.transcribe(
            str(audio), language="zh", beam_size=1, vad_filter=True,
        )
        out: list[dict[str, Any]] = []
        for seg in segments_iter:
            text = (seg.text or "").strip()
            if not text:
                continue
            out.append({"start": round(float(seg.start), 2), "end": round(float(seg.end), 2), "text": text})
        if out:
            safe_log(f"[whisper] 本机 ASR 完成：{len(out)} 句，时长约 {out[-1]['end']:.1f}s")
        return out
    except Exception as error:
        safe_log(f"[whisper] 本机 ASR 失败：{type(error).__name__}: {error}")
        return []


def _speech_timeline(probe: Path, subtitle_source: str) -> list[dict[str, Any]]:
    """用 ffmpeg silencedetect 从音轨推逐句字幕时间轴（纯本地，毫秒级）。

    原理：口播配音句与句之间有停顿（静音），静音边界就是每句的真实起止。
    文字按各语音段时长 × 语速分配，段边界对齐到最近的标点，保证每条
    字幕都是完整短句。比纯字数估算准得多：句间停顿、句长不均都被真实
    时间轴吸收。
    """
    try:
        total = duration_seconds(probe)
        if total <= 0 or not subtitle_source:
            return []
        proc = subprocess.run(
            [ffmpeg_path(), "-i", str(probe), "-af", "silencedetect=noise=-35dB:d=0.3", "-f", "null", "-"],
            capture_output=True, text=True,
        )
        log = proc.stderr or ""
        silences: list[tuple[float, float]] = []
        pending: float | None = None
        for match in re.finditer(r"silence_start: ([\d.]+)|silence_end: ([\d.]+)", log):
            if match.group(1) is not None:
                pending = float(match.group(1))
            elif pending is not None:
                silences.append((pending, float(match.group(2))))
                pending = None
        # 语音段 = 静音区间的补集（截到 [0, total]）
        speech: list[tuple[float, float]] = []
        cursor = 0.0
        for s_start, s_end in silences:
            if s_start > cursor + 0.1:
                speech.append((cursor, s_start))
            cursor = max(cursor, s_end)
        if total > cursor + 0.1:
            speech.append((cursor, total))
        # TTS 常在句中停顿（如「一晚上|两个多小时」），把过短段并入下一段，
        # 避免字幕碎成「一晚上两个多」这种半截句。
        merged: list[tuple[float, float]] = []
        for seg in speech:
            s, e = seg
            if merged and e - s < 1.5:
                merged[-1] = (merged[-1][0], e)
            else:
                merged.append((s, e))
        speech = [(s, e) for s, e in merged if e - s >= 0.4]
        if not speech:
            return []
        spoken_total = sum(e - s for s, e in speech)
        cps = max(2.0, min(8.0, len(subtitle_source) / spoken_total))
        text_len = len(subtitle_source)
        timeline: list[dict[str, Any]] = []
        pos = 0
        for seg_start, seg_end in speech:
            if pos >= text_len:
                break
            n = max(2, int(round((seg_end - seg_start) * cps)))
            end = min(text_len, pos + n)
            # 边界对齐到最近标点，避免半句
            for idx in range(end - 1, max(end - 6, pos), -1):
                if subtitle_source[idx] in "。！？，":
                    end = idx + 1
                    break
            text = subtitle_source[pos:end]
            pos = end
            # 超长段（>3.5s）按句号二次拆分，子句时长按字数比例内插
            seg_span = seg_end - seg_start
            if seg_span > 3.5 and any(ch in text for ch in "。！？"):
                clauses = [c for c in re.split(r"(?<=[。！？])", text) if c.strip()]
                if len(clauses) > 1:
                    weight = sum(len(c) for c in clauses) or 1
                    c_start = seg_start
                    for clause in clauses:
                        c_dur = seg_span * len(clause) / weight
                        clean = clause.strip("，。！？、 ")
                        if clean:
                            timeline.append({"start": round(c_start, 2), "end": round(min(seg_end, c_start + c_dur), 2), "text": clean})
                        c_start += c_dur
                    continue
            clean = text.strip("，。！？、 ")
            if clean:
                timeline.append({"start": round(seg_start, 2), "end": round(seg_end, 2), "text": clean})
        return timeline if len(timeline) >= len(speech) * 0.6 else []
    except Exception as error:
        safe_log(f"[timeline] 静音检测失败：{type(error).__name__}: {error}")
        return []


def _transcribe_realtime(audio: Path) -> list[dict[str, Any]]:
    """阿里云 paraformer-realtime-v2 实时 ASR：返回 [{start,end,text}, ...]（秒）。

    与挂掉的 qwen filetrans 用同一把 DASHSCOPE key（已实测连通）。注意：
    必须先转 16kHz 单声道 wav——TTS 输出是 24k/44.1k，按 16kHz 声明发送
    会让时间戳按采样率比例放大（24k 音频会得到 1.5 倍时长的时间戳）。
    """
    converted = audio.with_name(audio.stem + "-asr16k.wav")
    try:
        extract_audio(audio, converted)  # 统一转 16kHz 单声道
        events: queue.Queue = queue.Queue()

        class _Collector(RecognitionCallback):
            def on_complete(self) -> None:
                events.put(None)

            def on_error(self, result: Any) -> None:
                events.put(("error", result))

            def on_event(self, result: Any) -> None:
                events.put(("event", result))

        recognizer = Recognition(
            model="paraformer-realtime-v2",
            format="wav",
            sample_rate=16000,
            callback=_Collector(),
            workspace=BailianPipeline().workspace_id,
        )
        recognizer.start()
        with open(converted, "rb") as handle:
            while True:
                chunk = handle.read(3200)  # 100ms of 16kHz mono s16
                if not chunk:
                    break
                recognizer.send_audio_frame(chunk)
        recognizer.stop()

        sentences: list[dict[str, Any]] = []
        while True:
            item = events.get(timeout=60)
            if item is None:
                break
            kind, result = item
            if kind == "error":
                raise RuntimeError(f"paraformer 实时 ASR 失败：{getattr(result, 'message', str(result))[:300]}")
            sentence = getattr(result, "get_sentence", lambda: None)()
            if not sentence:
                continue
            # 流式中间态 begin_time 恒为 0 且 end_time 为 None；只收最终句。
            if sentence.get("end_time") is None:
                continue
            text = str(sentence.get("text", "")).strip()
            if text:
                sentences.append({
                    "start": round(float(sentence.get("begin_time", 0)) / 1000, 2),
                    "end": round(float(sentence["end_time"]) / 1000, 2),
                    "text": text,
                })
        if sentences:
            safe_log(f"[timeline] paraformer 实时 ASR 成功：{len(sentences)} 句")
        return sentences
    finally:
        safe_unlink(converted)


def subtitle_asr_timeline(audio: Path) -> list[dict[str, Any]]:
    """按「字幕时间轴」设置选择带句级时间戳的 ASR 引擎。

    ``auto`` 与 ``paraformer-realtime-v2`` 都走百炼实时识别（auto 的本地
    静音检测/whisper 兜底在调用方）；``qwen-audio-3.0-asr-flash-filetrans``
    用录音文件识别的句级时间戳；``local:funasr-*`` 走本机 FunASR 适配
    服务（引擎包运行时内，主程序按需自动拉起）。失败抛错，由调用方走
    本地兜底，不阻塞配音。
    """
    route = _load_model_routes().get("subtitle_asr", "auto")
    if route == "qwen-audio-3.0-asr-flash-filetrans":
        timeline = BailianPipeline().transcribe_timeline(audio)
        if timeline:
            safe_log(f"[timeline] qwen filetrans 时间轴：{len(timeline)} 句")
        return timeline
    if route.startswith("local:funasr-"):
        ensure_local_funasr_runtime()
        _, timeline = transcribe_timeline_with_funasr(audio, route)
        if timeline:
            safe_log(f"[timeline] 本地 FunASR 时间轴：{len(timeline)} 条")
        return timeline
    return _transcribe_realtime(audio)


def _source_voice_timeline(job: "Job", work: Path, source: Path) -> list[dict[str, Any]]:
    """真实字幕时间轴：云端 ASR > 静音检测 > 本机 whisper > 按字数估算（调用方处理）。

    返回的 list[{start,end,text}] 时间轴是配音音频的绝对时间，会被
    render_edit 按 trim_start/clip_duration 裁剪后烧录。
    """
    if job.voice_timeline:
        return job.voice_timeline
    probe = work / "timeline-probe.wav"
    try:
        extract_audio(source, probe)
    except Exception as error:
        safe_log(f"[timeline] 抽音轨失败：{type(error).__name__}: {error}")
        return []
    try:
        # 1) 「字幕时间轴」路由选定的云端 ASR（默认百炼 paraformer 实时，
        #    真毫秒时间戳；可在模型分配里换成 qwen filetrans）。
        try:
            sentences = subtitle_asr_timeline(probe)
            if sentences:
                timeline = sentences_to_subtitles(sentences)
                if timeline:
                    store.update(job, voice_timeline=timeline)
                    safe_log(f"[timeline] 云端 ASR 时间轴：{len(timeline)} 条")
                    return timeline
        except Exception as error:
            safe_log(f"[timeline] 云端 ASR 失败，回退到静音检测：{type(error).__name__}: {error}")
        # 2) 静音检测（本地、毫秒级、无模型依赖——首选兜底）。
        #    只对全文配音（preview.wav）做：result.mp4 的音轨就是 preview.wav
        #    的（前缀）裁切，两者时间轴同源；而文本对应关系只有全文音频才
        #    成立（result 只覆盖开头一小段）。原声模式没有配音，走调用方估算。
        if job.preview_audio_name:
            subtitle_source = (job.rewritten_text or "").strip()
            if not subtitle_source:
                subtitle_source = "".join(line.get("text", "") for line in job.timeline).strip()
            full_audio = work / job.preview_audio_name
            if subtitle_source and full_audio.exists():
                timeline = _speech_timeline(full_audio, subtitle_source)
                if timeline:
                    store.update(job, voice_timeline=timeline)
                    safe_log(f"[timeline] 静音检测成功：{len(timeline)} 条")
                    return timeline
        # 3) 本机 whisper 兜底（CPU 上较慢，仅在静音检测也无结果时）
        timeline = _whisper_timeline(probe)
        if timeline:
            store.update(job, voice_timeline=timeline)
            return timeline
    finally:
        safe_unlink(probe)
    return []


def pick_cover_frame(target: Path, clip_duration: float, trim_start: float, work: Path) -> tuple[Path, float]:
    """从成片中挑一帧适合做封面的画面。

    首帧往往是口型张开一半、表情最差的画面。这里在成片前 70% 时间范围内
    均匀抽 5 个候选帧，用 OpenCV 打分：清晰度（拉普拉斯方差，越高越锐）+
    亮度适中（过暗过曝都扣分），选综合分最高的帧存为 cover-frame.jpg。
    OpenCV 不可用时退回 25% 处的单帧（避开最容易翻车的首帧）。
    """
    probe = duration_seconds(target)
    total = probe if probe > 0 else clip_duration
    if total <= 0:
        total = 5.0
    candidates = [total * frac for frac in (0.15, 0.28, 0.42, 0.56, 0.70)]
    best_path = work / "cover-frame.jpg"
    try:
        import cv2

        best_score = -1.0
        best_time = candidates[0]
        for timestamp in candidates:
            frame_path = work / f"cover-cand-{int(timestamp * 100)}.jpg"
            proc = subprocess.run(
                [ffmpeg_path(), "-y", "-ss", str(max(0.0, trim_start + timestamp)),
                 "-i", str(target), "-frames:v", "1", str(frame_path)],
                capture_output=True,
            )
            if proc.returncode != 0 or not frame_path.exists():
                continue
            image = cv2.imread(str(frame_path))
            if image is None:
                frame_path.unlink(missing_ok=True)
                continue
            gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
            sharpness = float(cv2.Laplacian(gray, cv2.CV_64F).var())
            brightness = float(gray.mean())
            # 亮度理想区间 90~170（0~255），偏离按比例扣分；清晰度权重更高。
            if brightness < 90:
                brightness_score = max(0.0, brightness / 90.0)
            elif brightness > 170:
                brightness_score = max(0.0, (255.0 - brightness) / 85.0)
            else:
                brightness_score = 1.0
            score = sharpness * 0.01 + brightness_score * 10.0
            if score > best_score:
                best_score = score
                best_time = timestamp
                if best_path.exists():
                    best_path.unlink()
                frame_path.rename(best_path)
            else:
                frame_path.unlink(missing_ok=True)
        if best_path.exists():
            return best_path, best_time
    except Exception as error:
        safe_log(f"[cover] 智能选帧失败，退回单帧截取：{type(error).__name__}: {error}")
    fallback_time = total * 0.25
    subprocess.run(
        [ffmpeg_path(), "-y", "-ss", str(max(0.0, trim_start + fallback_time)),
         "-i", str(target), "-frames:v", "1", str(best_path)],
        capture_output=True,
    )
    return best_path, fallback_time


def render_cover_image(
    frame: Path, cover: Path, cover_text: str, subtitle_keywords: str = "",
    cover_style: str = "dark-bold", cover_subtitle: str = "",
) -> None:
    """把选中的帧加工成封面：顶部压暗 + 底部深色渐变 + 大字标题。

    构图对齐爆款口播封面：
    - 顶部 28% 高度黑色半透明压暗：压暗背景，让整体更聚焦。
    - 底部 38% 高度黑色半透明压暗：文字区的可读性底衬，
      替代整块灰色半透明底框（那是"AI 感"的主要来源）。
    - 大字：两行以内自动换行、粗描边 + 投影，关键词用黄色强调，无底框。
    """
    width = 1080
    height = 1920
    if cover_text:
        # 关键词高亮：封面大字里若包含字幕关键词则用黄色强调。
        highlight = ""
        for keyword in (subtitle_keywords or "").split(","):
            keyword = keyword.strip()
            if keyword and keyword in cover_text:
                highlight = keyword
                break
        # 四种模板共享同一张选帧图，但分别改变文字构图和背景处理。
        if cover_style == "vertical-cutout":
            lines = list(cover_text[:6])
        elif cover_style == "giant-headline":
            lines = [cover_text[i:i + 3] for i in range(0, len(cover_text), 3)][:2]
        else:
            lines = [cover_text[i:i + 5] for i in range(0, len(cover_text), 5)][:2]
        draw_parts: list[str] = []
        if cover_style == "giant-headline":
            line_gap, start_y, font_size = int(height * 0.15), int(height * 0.14), "h/6"
        elif cover_style == "vertical-cutout":
            line_gap, start_y, font_size = int(height * 0.095), int(height * 0.25), "h/10"
        elif cover_style == "center-sticker":
            line_gap, start_y, font_size = int(height * 0.09), int(height * 0.58), "h/8"
        else:
            line_gap, start_y, font_size = int(height * 0.085), int(height * 0.70), "h/9"
        for index, line in enumerate(lines):
            safe_line = line.replace("'", "\\'").replace(":", "\\:")
            if cover_style in {"diagonal-yellow", "yellow-block"}:
                color = "black"
            elif cover_style in {"clean-white", "giant-headline", "vertical-cutout"}:
                color = "white"
            elif cover_style == "center-sticker":
                color = "cyan" if index else "white"
            else:
                color = "yellow" if (highlight and highlight in line) else "white"
            border = "black@0.25" if cover_style in {"diagonal-yellow", "yellow-block"} else "black@0.92"
            if cover_style == "vertical-cutout":
                x_position = f"w*0.68-text_w/2"
            else:
                x_position = "(w-text_w)/2"
            draw_parts.append(
                f"drawtext=fontfile='{_FONT_FILE}':text='{safe_line}'"
                f":x={x_position}:y={start_y}+{index}*{line_gap}"
                f":fontsize={font_size}:fontcolor={color}"
                f":borderw=8:bordercolor={border}"
                f":shadowx=5:shadowy=5:shadowcolor=black@0.55"
            )
        subtitle_lines = [cover_subtitle.strip()[index:index + 14] for index in range(0, len(cover_subtitle.strip()), 14)][:2]
        if subtitle_lines:
            subtitle_y = int(height * (0.84 if cover_style in {"diagonal-yellow", "yellow-block"} else 0.87))
            for index, line in enumerate(subtitle_lines):
                safe_line = line.replace("'", "\\'").replace(":", "\\:")
                draw_parts.append(
                    f"drawtext=fontfile='{_FONT_FILE}':text='{safe_line}'"
                    f":x=(w-text_w)/2:y={subtitle_y}+{index}*{int(height * 0.045)}"
                    f":fontsize=h/20:fontcolor=white:borderw=5:bordercolor=black@0.86"
                    f":shadowx=3:shadowy=3:shadowcolor=black@0.5"
                )
        if cover_style == "diagonal-yellow":
            background = (
                f"drawbox=x=0:y={int(height * 0.59)}:w={width}:h={int(height * 0.25)}:color=yellow@0.92:t=fill,"
                f"drawbox=x=0:y={int(height * 0.84)}:w={width}:h={int(height * 0.04)}:color=0x0b1830@0.88:t=fill"
            )
        elif cover_style == "giant-headline":
            background = (
                f"drawbox=x=0:y=0:w={width}:h={int(height * 0.53)}:color=black@0.36:t=fill,"
                f"drawbox=x=0:y={int(height * 0.78)}:w={width}:h={int(height * 0.22)}:color=black@0.72:t=fill"
            )
        elif cover_style == "vertical-cutout":
            background = (
                "boxblur=10:2,"
                f"drawbox=x={int(width * 0.54)}:y=0:w={int(width * 0.46)}:h={height}:color=black@0.58:t=fill"
            )
        elif cover_style == "center-sticker":
            background = (
                f"drawbox=x=0:y={int(height * 0.54)}:w={width}:h={int(height * 0.30)}:color=black@0.42:t=fill"
            )
        elif cover_style == "yellow-block":
            background = (
                f"drawbox=x=0:y={int(height * 0.62)}:w={width}:h={int(height * 0.38)}:"
                "color=yellow@0.92:t=fill"
            )
        elif cover_style == "clean-white":
            background = f"drawbox=x=0:y={int(height * 0.62)}:w={width}:h={int(height * 0.38)}:color=black@0.25:t=fill"
        else:
            background = (
                f"drawbox=x=0:y=0:w={width}:h={int(height * 0.28)}:color=black@0.50:t=fill,"
                f"drawbox=x=0:y={int(height * 0.62)}:w={width}:h={int(height * 0.38)}:color=black@0.45:t=fill"
            )
        filter_complex = background + "," + ",".join(draw_parts)
        subprocess.run(
            [ffmpeg_path(), "-y", "-i", str(frame),
             "-vf", f"scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height},{filter_complex}",
             "-frames:v", "1", "-q:v", "2", str(cover)],
            capture_output=True, check=True,
        )
    else:
        subprocess.run(
            [ffmpeg_path(), "-y", "-i", str(frame),
             "-vf", f"scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height}",
             "-frames:v", "1", "-q:v", "2", str(cover)],
            capture_output=True, check=True,
        )


def render_edit(job_id: str) -> None:
    job = store.get(job_id)
    work = JOBS_DIR / job.id
    try:
        if not job.output_name:
            raise RuntimeError("请先完成第四步视频生成。")
        source = work / job.output_name
        target = work / "final.mp4"
        subtitle = work / "edit.srt"
        clip_duration = effective_clip_duration(job, work)
        # 字幕必须与成片配音一致。优先使用配音音频 ASR 得到的真实逐句
        # 时间轴（voice_timeline），按裁剪窗口平移截断；旧项目没有
        # 时间轴时才退回按字数比例估算。
        clipped_lines: list[dict[str, Any]] = []
        voice_timeline = _source_voice_timeline(job, work, source)
        if voice_timeline:
            clipped_lines = clip_timeline(voice_timeline, job.trim_start, clip_duration)
        if not clipped_lines:
            subtitle_source = (job.rewritten_text or "").strip()
            if not subtitle_source:
                subtitle_source = "".join(line.get("text", "") for line in job.timeline).strip()
            if subtitle_source:
                # 无真实时间轴（旧项目且 ASR 不可用）时按语速估算成片窗口实际说到的
                # 文本区间：从「trim_start 处说到的字」开始，而不是总从文案开头——
                # 否则成片用 -ss 裁掉片头后，字幕与音频完全对不上。
                text_len = len(subtitle_source)
                cps = 4.2  # 中文口播默认约 4.2 字/秒
                # 关键：cps = 配语文本字数 / 配音总时长，分子分母要匹配。
                # TTS 用 rewritten_text 全文合成 preview.wav，再裁到 preview-trimmed.wav
                # 喂给 VideoRetalk。preview.wav 时长 / 全文 = 全 TTS 真实 cps。
                # preview_duration 是脏数据时用 ffprobe 兜底。
                preview_full = work / "preview.wav"
                preview_dur = float(getattr(job, "preview_duration", 0) or 0)
                if preview_dur <= 0 and preview_full.exists():
                    preview_dur = duration_seconds(preview_full)
                if preview_dur > 0 and text_len > 0:
                    cps = max(2.5, min(7.0, text_len / preview_dur))
                char_start = max(0, int(round(job.trim_start * cps)))
                char_end = min(text_len, max(char_start + 1, int(round((job.trim_start + clip_duration) * cps))))
                # 把窗口边界对齐到最近的句末/逗号边界，避免「士，」「卖短视频口」这种
                # 残句开头。开头向后找最近的「，。！？」；结尾向前找最近的「。！？」；
                # 找不到时退回到字符边界。
                for idx in range(char_start, min(char_start + 6, text_len)):
                    if subtitle_source[idx] in "，。！？":
                        char_start = idx + 1
                        break
                for idx in range(char_end - 1, max(char_end - 8, char_start), -1):
                    if subtitle_source[idx] in "。！？":
                        char_end = idx + 1
                        break
                segments = split_subtitles(subtitle_source[char_start:char_end])
                total_weight = sum(len(s) for s in segments) or 1
                cursor = 0.0
                for index, seg in enumerate(segments):
                    if index == len(segments) - 1:
                        end = clip_duration
                    else:
                        # 剩余空间按字数比例分配，避免 cursor 累计超过 clip_duration
                        # 造成字幕延伸到成片外（用户看到成片结束后字幕还在动）。
                        remaining = max(0.0, clip_duration - cursor)
                        end = round(min(clip_duration, cursor + remaining * len(seg) / max(1, total_weight - sum(len(x) for x in segments[:index]))), 2)
                    clipped_lines.append({"start": round(cursor, 2), "end": end, "text": seg})
                    cursor = end
        subtitle.write_text(to_srt(clipped_lines), encoding="utf-8")
        if not clipped_lines:
            # 空字幕不再静默：留一条可检索的痕迹，便于定位是 ASR 失败
            # 还是文案/时间轴缺失。
            safe_log(
                "[subtitle] 警告：本次导出字幕为空。"
                f"voice_timeline={len(job.voice_timeline or [])} 条，"
                f"rewritten_text={len((job.rewritten_text or '').strip())} 字，"
                f"timeline={len(job.timeline or [])} 条。"
            )
        filters: list[str] = []
        if job.sticker:
            safe_sticker = job.sticker.replace("'", "\\'").replace(":", "\\:")
            filters.append(
                f"drawtext=fontfile='{_FONT_FILE}':text='{safe_sticker}':x=w*0.07:y=h*0.18"
                f":fontsize=h/26:fontcolor=yellow"
                f":borderw=4:bordercolor=black@0.85"
                f":shadowx=3:shadowy=3:shadowcolor=black@0.4"
            )
        # ── 字幕：统一走 ASS（PlayResY=1080 保证字号与分辨率无关）──
        # 旧逻辑"无关键词时直接烧 SRT"有严重 bug：SRT 无分辨率声明，
        # libass 按 384x288 默认虚拟画布解析，42 号字在竖屏视频上被放大约
        # 4.3 倍，渲染出占屏 40% 的巨型字幕糊在脸上。
        subtitle_path = subtitle
        subtitle_script = subtitle.read_text(encoding="utf-8") if subtitle.exists() and subtitle.stat().st_size else ""
        if (job.title or (job.subtitle_enabled and subtitle_script)):
            ass_path = work / "edit.ass"
            ratio_text = str(job.title_font_size).lstrip("h/") or "18"
            try:
                title_ratio = max(8, int(ratio_text))
            except ValueError:
                title_ratio = 18
            ass_path.write_text(
                to_ass(
                    clipped_lines,
                    font_size=job.subtitle_font_size,
                    primary=hex_to_ass(job.subtitle_color),
                    outline="&H40000000&",
                    margin_v=job.subtitle_margin_v,
                    keywords=job.subtitle_keywords,
                    keyword_color=hex_to_ass(job.subtitle_keyword_color),
                    title=job.title,
                    title_style=getattr(job, "subtitle_style", "inline-cyan"),
                    title_color_name=job.title_color,
                    title_font_ratio=title_ratio,
                    title_position=job.title_position,
                    duration=clip_duration,
                ),
                encoding="utf-8",
            )
            subtitle_path = ass_path
            safe_subtitle = str(subtitle_path).replace("\\", "/").replace(":", "\\:")
            filters.append(
                f"subtitles='{safe_subtitle}':fontsdir='{_FONTS_DIR}'"
            )
        command = [ffmpeg_path(), "-y"]
        if job.trim_start:
            command += ["-ss", str(job.trim_start)]
        command += ["-i", str(source)]
        layout_mode = "pip" if getattr(job, "layout_mode", "fullscreen") == "pip" else "fullscreen"
        layout_background_type = getattr(job, "layout_background_type", "blur")
        if layout_background_type not in {"broll", "image", "color", "blur"}:
            layout_background_type = "blur"
        main_w, main_h = video_dimensions(source)
        # 多段 B-roll：每段独立输入流（图片 -loop 1，视频 -stream_loop -1）。
        # 无论主体是全屏还是画中画，B-roll 都按其时间窗插入；画中画只是
        # B-roll 出现前的底图布局，不应让用户已添加的插片悄悄失效。
        # 输入序号从 1 开始（0 是主视频）；音乐输入放在背景输入之后。
        broll_inputs: list[tuple[int, Path, float, float, str, str, float, int]] = []
        if job.broll_enabled and job.broll_clips:
            for clip in job.broll_clips:
                if not clip.enabled:
                    continue
                clip_path = work / clip.name
                if not clip_path.exists():
                    safe_log(f"[render_edit] 警告：B-roll 文件缺失（{clip.name}），跳过该段插片。")
                    continue
                broll_inputs.append((
                    len(broll_inputs) + 1, clip_path, clip.start, clip.duration,
                    clip.mode, clip.pip_position, clip.pip_scale, clip.pip_margin,
                ))
        # 兼容旧项目：broll_clips 为空但旧单值字段有素材时，视为一段。
        if not broll_inputs and job.broll_enabled and job.broll_name:
            legacy = work / job.broll_name
            if legacy.exists():
                broll_inputs.append((
                    1, legacy, float(job.broll_start or 0), float(job.broll_duration or 4),
                    "replace", "bottom-right", 0.32, 24,
                ))
            else:
                safe_log(f"[render_edit] 警告：B-roll 文件缺失（{job.broll_name}），本次导出跳过插片。")
        has_broll = bool(broll_inputs)
        for input_index, clip_path, _s, _d, _mode, _position, _scale, _margin in broll_inputs:
            # A supplied B-roll clip is looped only as a visual overlay.  The
            # original talking-head audio remains the main audio track.
            # Images are looped frame-by-frame via -loop 1; videos via -stream_loop -1.
            command += broll_input_args(clip_path) + ["-i", str(clip_path)]
        layout_background_input: int | None = None
        has_extra_layout_background_input = False
        if layout_mode == "pip" and layout_background_type in {"broll", "image"}:
            background_name = (getattr(job, "layout_background_name", None) or "").strip()
            allowed_backgrounds = {clip.name for clip in job.broll_clips}
            if not background_name or background_name not in allowed_backgrounds:
                raise RuntimeError("请选择画中画背景素材。")
            background_path = work / background_name
            if not background_path.exists():
                raise RuntimeError("画中画背景素材文件不存在，请重新上传。")
            if layout_background_type == "broll" and is_image_path(background_path):
                raise RuntimeError("当前背景类型是 B-roll，请选择视频素材。")
            if layout_background_type == "image" and not is_image_path(background_path):
                raise RuntimeError("当前背景类型是图片，请选择图片素材。")
            # A selected B-roll can serve both as background and timed insert;
            # reuse its already-open input stream instead of adding it twice.
            layout_background_input = next(
                (input_index for input_index, clip_path, *_rest in broll_inputs if clip_path.resolve() == background_path.resolve()),
                None,
            )
            if layout_background_input is None:
                layout_background_input = 1 + len(broll_inputs)
                has_extra_layout_background_input = True
                command += broll_input_args(background_path) + ["-i", str(background_path)]
        music = work / job.music_name if job.music_name else None
        has_music = bool(music and music.exists())
        if has_music:
            command += ["-stream_loop", "-1", "-i", str(music)]
        if job.trim_end is not None and job.trim_end > job.trim_start:
            command += ["-t", str(job.trim_end - job.trim_start)]

        # 各段插入点/时长钳制到成片范围内；画中画和全屏共用同一份时间窗。
        clamped: list[tuple[Any, ...]] = []
        if has_broll:
            for input_index, _p, start, duration, mode, position, scale, margin in broll_inputs:
                start = max(0.0, min(float(start or 0), max(0.0, clip_duration - 0.2)))
                duration = max(0.2, min(float(duration or 4), clip_duration - start))
                clamped.append((input_index, start, duration, mode, position, scale, margin))

        if layout_mode == "pip":
            visual_filter = ",".join(filters) if filters else "null"
            layout_graph = build_layout_filter(
                background_type=layout_background_type,
                foreground_scale=getattr(job, "layout_foreground_scale", 0.32),
                foreground_position=getattr(job, "layout_foreground_position", "bottom-right"),
                foreground_margin=getattr(job, "layout_foreground_margin", 24),
                background_color=getattr(job, "layout_background_color", "202020"),
                blur_strength=getattr(job, "layout_blur", 18),
                main_w=main_w,
                main_h=main_h,
                background_input=layout_background_input,
            )
            filter_graph = (
                f"{layout_graph};{build_broll_filter(clamped, visual_filter, main_w, main_h, base_stream='layout')}"
                if clamped
                else f"{layout_graph};[layout]{visual_filter}[video]"
            )
            command += ["-filter_complex", filter_graph]
            if has_music:
                # 输入顺序：主体视频 + B-roll +（可选的额外）画中画背景 + 背景音乐。
                music_index = 1 + len(broll_inputs) + (1 if has_extra_layout_background_input else 0)
                volume = float(getattr(job, "music_volume", 0.14) or 0.14)
                command[-1] += (
                    f";[{music_index}:a]volume={volume}[music];"
                    f"[0:a][music]amix=inputs=2:duration=first[audio]"
                )
                command += ["-map", "[video]", "-map", "[audio]", "-c:a", "aac"]
            else:
                command += ["-map", "[video]", "-map", "0:a?", "-c:a", "aac"]
        elif has_broll:
            visual_filter = ",".join(filters) if filters else "null"
            # Put title/subtitles *after* the B-roll replacement so
            # readable text remains visible no matter which visual is shown.
            command += [
                "-filter_complex",
                build_broll_filter(clamped, visual_filter, main_w, main_h),
            ]
            if has_music:
                # 音乐是最后一个输入流：0 主视频 + N 个 B-roll + 音乐。
                music_index = 1 + len(broll_inputs)
                volume = float(getattr(job, "music_volume", 0.14) or 0.14)
                command[-1] += (
                    f";[{music_index}:a]volume={volume}[music];"
                    f"[0:a][music]amix=inputs=2:duration=first[audio]"
                )
                command += ["-map", "[video]", "-map", "[audio]", "-c:a", "aac"]
            else:
                command += ["-map", "[video]", "-map", "0:a?", "-c:a", "aac"]
        elif has_music:
            volume = float(getattr(job, "music_volume", 0.14) or 0.14)
            video_filter = ",".join(filters) if filters else "null"
            command += [
                "-filter_complex",
                f"[0:v]{video_filter}[video];[1:a]volume={volume}[music];"
                f"[0:a][music]amix=inputs=2:duration=first[audio]",
                "-map", "[video]", "-map", "[audio]", "-c:a", "aac",
            ]
        else:
            if filters:
                command += ["-vf", ",".join(filters)]
            command += ["-c:a", "copy"]
        command += ["-movflags", "+faststart", str(target)]
        store.update(job, status="running", stage="处理中 55%", progress=55, current_step=5, error=None)
        subprocess.run(command, capture_output=True, check=True)
        # 封面：用户上传过自定义封面则直接沿用，不再重新生成；只有未上传时才
        # 智能选帧（避开首帧口型翻车画面）+ 压暗底衬 + 大字构图。
        cover_name = job.cover_name or "cover.jpg"
        cover = work / cover_name
        if not job.cover_name:
            frame, _picked_time = pick_cover_frame(target, clip_duration, job.trim_start, work)
            render_cover_image(
                frame,
                cover,
                job.cover_text or job.title or "",
                job.subtitle_keywords or "",
                job.cover_style,
                cover_subtitle=job.title or "",
            )
        store.update(job, status="succeeded", stage="剪辑成片已导出", progress=100, edit_output_name=target.name, cover_name=cover_name)
    except Exception as error:
        store.update(job, status="failed", stage="剪辑导出失败", error=str(error))


app = FastAPI(title="afan Talking Video Agent")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# ── 本地接口防护 ─────────────────────────────────────────────
# 浏览器允许任意网页向 127.0.0.1 静默发送跨站 POST（响应读不到，但请求会生效）。
# 因此写操作必须携带页面里注入的握手 token：跨站脚本无法附加自定义头，
# 预检请求也会因为没有 CORS 放行而失败；同时校验 Host 头，封堵 DNS rebinding。
_API_TOKEN = secrets.token_urlsafe(24)

# CLI 等 Agent 工具不是网页，拿不到页面注入的握手 token；本机同用户读写自己的
# 数据目录与调用本机服务同级可信，因此把 token 落到数据目录供其读取。
# token 只在文件不存在时生成一次并持久复用：测试导入、第二实例启动都不会
# 覆盖它——若每次导入都重写，pytest 等进程会打断正在运行服务的 CLI 通道。
_TOKEN_FILE = USER_DATA_ROOT / "agent_token.txt"
try:
    _existing_token = _TOKEN_FILE.read_text(encoding="utf-8").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{20,64}", _existing_token):
        _existing_token = ""
except OSError:
    _existing_token = ""
if _existing_token:
    _API_TOKEN = _existing_token
else:
    try:
        USER_DATA_ROOT.mkdir(parents=True, exist_ok=True)
        _TOKEN_FILE.write_text(_API_TOKEN, encoding="utf-8")
    except OSError:
        pass  # 数据目录不可写时仅影响 CLI 调用，网页端不受影响


# 这两个 GET 会在桌面弹原生文件夹选择框；前端经 api() 调用已带 token，
# 一并纳入校验，防止恶意网页在后台反复弹窗骚扰。
_DIALOG_GET_PATHS = {"/api/data-location/choose", "/api/local-engines/choose-dir"}


@app.middleware("http")
async def local_api_guard(request: Request, call_next):
    host = (request.headers.get("host") or "").rsplit(":", 1)[0].strip("[]").lower()
    if host not in {"127.0.0.1", "localhost", "::1"}:
        return JSONResponse({"detail": "本服务只允许从 127.0.0.1 / localhost 访问。"}, status_code=403)
    needs_token = request.url.path in _DIALOG_GET_PATHS or (
        request.method in {"POST", "PUT", "DELETE", "PATCH"} and request.url.path.startswith("/api/")
    )
    if needs_token and request.headers.get("x-afan-token") != _API_TOKEN:
        return JSONResponse({"detail": "页面与本地服务的安全握手已失效，请刷新页面后重试。"}, status_code=403)
    return await call_next(request)


@app.on_event("startup")
def _reap_stale_local_adapters() -> None:
    """上次运行遗留的 GPU 适配器会持续占用内存额度，启动时统一清理。"""
    from app.local_runtime import reap_orphan_adapters

    killed = reap_orphan_adapters()
    if killed:
        print(f"[startup] 已清理遗留适配器进程: {killed}", flush=True)


@app.get("/", response_class=HTMLResponse)
def home() -> HTMLResponse:
    # 前端迭代频繁；避免浏览器继续使用旧 HTML，造成新脚本与旧页面结构不匹配。
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    # 把接口握手 token 注入页面，前端 api() 会把它放进请求头（见 local_api_guard）。
    html = html.replace(
        "<head>",
        f"<head><script>window.__AFAN_TOKEN__ = {json.dumps(_API_TOKEN)};</script>",
        1,
    )
    return HTMLResponse(
        html,
        headers={"Cache-Control": "no-store, max-age=0"},
    )


# ── 网页设置页：用户在本机填写自己的 API Key ─────────────────
PROVIDER_TITLES = {
    "mimo": "小米 MiMo",
    "dashscope": "阿里云百炼",
    "developer": "开发者连接",
}
SETTINGS_MODULES = [
    {
        "id": "script",
        "title": "AI 文案创作",
        "note": "根据主题直接生成口播稿",
        "models": ["auto", "mimo-v2.5", "deepseek-v4-flash", "qwen3.7-flash"],
        "supports_custom": True,
    },
    {
        "id": "rewrite",
        "title": "文案改写",
        "note": "把原文案改写成新口播稿",
        "models": ["auto", "mimo-v2.5", "mimo-v2.5-pro", "deepseek-v4-flash", "qwen3.7-flash"],
        "supports_custom": True,
    },
    {
        "id": "asr",
        "title": "视频文案提取（ASR）",
        "note": "将视频的声音识别为文案；本地 FunASR 需先安装本地 AI 依赖",
        "models": ["auto", "mimo-v2.5-asr", "qwen-audio-3.0-asr-flash-filetrans", "local:funasr-paraformer", "local:funasr-sensevoice"],
    },
    {
        "id": "subtitle_asr",
        "title": "字幕时间轴（ASR）",
        "note": "对齐配音与字幕的逐句时间轴；本地 FunASR 可直接返回时间戳",
        "models": ["auto", "paraformer-realtime-v2", "qwen-audio-3.0-asr-flash-filetrans", "local:funasr-paraformer", "local:funasr-sensevoice"],
    },
    {
        "id": "voice_clone",
        "title": "声音复刻",
        "note": "用人物原声或样音生成同类声音",
        "models": ["mimo-v2.5-tts-voiceclone", "cosyvoice-v3.5-plus", "qwen3-tts-vc", "qwen3-tts-local-base"],
    },
    {
        "id": "direct_tts",
        "title": "直接配音",
        "note": "不复刻声音时使用的配音模型",
        "models": ["mimo-v2.5-tts", "qwen-builtin-tts", "qwen3-tts-local-customvoice"],
    },
    {
        "id": "lipsync",
        "title": "视频改口型",
        "note": "可选云端 VideoRetalk，或接入本机 MuseTalk 1.5 / HeyGem",
        "models": ["videoretalk", "musetalk-1.5", "heygem"],
    },
    {
        "id": "edit_plan",
        "title": "智能剪辑方案",
        "note": "决定标题、字幕染色词与成片节奏（⑤ 一键成片）",
        "models": ["auto", "mimo-v2.5", "deepseek-v4-flash", "qwen3.7-flash"],
        "supports_custom": True,
    },
]

SETTINGS_FIELDS = [
    {
        "key": "DASHSCOPE_API_KEY",
        "secret": True,
        "group": "dashscope",
        "label": "API Key",
        "hint": "在 bailian.console.aliyun.com 申请，必须用「华北2（北京）」地域的 Key，并开通 VideoRetalk 服务",
    },
    {
        "key": "DASHSCOPE_WORKSPACE_ID",
        "secret": False,
        "group": "dashscope",
        "label": "业务空间 ID",
        "hint": "百炼控制台 → 业务空间 → 详情页里的「业务空间ID」（只有百炼需要填这个）",
    },
    {
        "key": "MIMO_API_KEY",
        "secret": True,
        "group": "mimo",
        "label": "API Key",
        "hint": "在 platform.xiaomimimo.com 申请，用小米账号登录即可，新用户有免费试用额度",
    },
    {
        "key": "FFMPEG_PATH",
        "secret": False,
        "group": "developer",
        "label": "FFmpeg 路径",
        "hint": "仅当 FFmpeg 未安装时才需要填，例如 C:\\ffmpeg\\bin\\ffmpeg.exe",
    },
    {
        "key": "MUSETALK_BASE_URL",
        "secret": False,
        "group": "developer",
        "label": "MuseTalk 远程服务地址",
        "hint": "可选；填写通过 SSH 隧道或局域网暴露的 FastAPI 地址，例如 http://127.0.0.1:8001。填写后优先使用远程服务。",
    },
    {
        "key": "MUSETALK_COMMAND",
        "secret": False,
        "group": "developer",
        "label": "MuseTalk 1.5 推理命令",
        "hint": "可填 JSON 命令数组；支持 {video}、{audio}、{output}、{output_dir} 占位符。模型权重需自行安装。",
    },
    {
        "key": "MUSETALK_WORKDIR",
        "secret": False,
        "group": "developer",
        "label": "MuseTalk 工作目录",
        "hint": "可选；留空使用当前项目任务目录。",
    },
    {
        "key": "MUSETALK_TIMEOUT",
        "secret": False,
        "group": "developer",
        "label": "MuseTalk 超时（秒）",
        "hint": "0 或留空 = 不限制（本地推理推荐）；默认不限制。",
    },
    {
        "key": "MUSETALK_ENGINE_ROOT",
        "secret": False,
        "group": "developer",
        "label": "MuseTalk 本地引擎目录",
        "hint": "可选；引擎下载后放在这里。建议使用空间充足的数据盘目录。",
    },
    {
        "key": "FUNASR_ENGINE_ROOT",
        "secret": False,
        "group": "developer",
        "label": "FunASR 模型目录",
        "hint": "可选；把已下载的 FunASR 模型文件夹路径填到这里，保存后立即重新检测。清空则恢复标准目录。",
    },
    {
        "key": "QWEN_TTS_ENGINE_ROOT",
        "secret": False,
        "group": "developer",
        "label": "Qwen3-TTS 模型目录",
        "hint": "可选；把已下载的 Qwen3-TTS 模型文件夹路径填到这里，保存后立即重新检测。清空则恢复标准目录。",
    },
    {
        "key": "QWEN_TTS_PYTHON",
        "secret": False,
        "group": "developer",
        "label": "Qwen3-TTS 解释器",
        "hint": "可选；模型包自带运行时里的 python.exe，「一键接入模型包」会自动填写。",
    },
    {
        "key": "MUSETALK_PYTHON",
        "secret": False,
        "group": "developer",
        "label": "MuseTalk 解释器",
        "hint": "可选；MuseTalk 包自带运行时里的 python.exe，「一键接入模型包」会自动填写。",
    },
    {
        "key": "FUNASR_PYTHON",
        "secret": False,
        "group": "developer",
        "label": "FunASR 解释器",
        "hint": "可选；voice 模型包自带运行时里的 python.exe，「一键接入模型包」会自动填写。",
    },
    {
        "key": "FUNASR_BASE_URL",
        "secret": False,
        "group": "developer",
        "label": "本地 FunASR 服务地址",
        "hint": "可选；软件会自动在引擎包运行时里启动本机服务，一般无需填写。",
    },
    {
        "key": "FFMPEG_PATH",
        "secret": False,
        "group": "developer",
        "label": "FFmpeg 路径",
        "hint": "可选；ffmpeg.exe 或其所在目录。留空按顺序尝试 tools\\ffmpeg、安装目录与 PATH。",
    },
    {
        "key": "MUSETALK_ENGINE_URL",
        "secret": False,
        "group": "developer",
        "label": "MuseTalk 引擎包下载地址",
        "hint": "可选；填写你准备好的 .zip/.tar.gz 引擎包地址，软件不会擅自猜测或下载未知模型。",
    },
    {
        "key": "QWEN_TTS_BASE_URL",
        "secret": False,
        "group": "developer",
        "label": "本地 Qwen3-TTS 服务地址",
        "hint": "可选；例如 http://127.0.0.1:8020。服务可运行在本机或 SSH 隧道后的 GPU 主机。",
    },
    {
        "key": "QWEN_TTS_TIMEOUT",
        "secret": False,
        "group": "developer",
        "label": "本地 Qwen3-TTS 超时（秒）",
        "hint": "0 或留空 = 不限制（本地推理推荐）；默认不限制。",
    },
    {
        "key": "HEYGEM_BASE_URL",
        "secret": False,
        "group": "developer",
        "label": "HeyGem 服务地址",
        "hint": "可选，默认 http://127.0.0.1:8383；需先启动 HeyGem 本地服务。",
    },
    {
        "key": "HEYGEM_MEDIA_ROOT",
        "secret": False,
        "group": "developer",
        "label": "HeyGem 共享素材目录",
        "hint": "Docker 部署时填写容器内映射目录；留空则使用 file:// 本机路径。",
    },
    {
        "key": "HEYGEM_HOST_MEDIA_ROOT",
        "secret": False,
        "group": "developer",
        "label": "HeyGem 主机共享目录",
        "hint": "Docker 部署时填写主机上的共享目录；程序会把本次输入复制到这里。",
    },
    {
        "key": "HEYGEM_TIMEOUT",
        "secret": False,
        "group": "developer",
        "label": "HeyGem 超时（秒）",
        "hint": "0 或留空 = 不限制；默认不限制。",
    },
    {
        "key": "HEYGEM_PACKAGE_DIR",
        "secret": False,
        "group": "developer",
        "label": "HeyGem Lite 模型包目录",
        "hint": "可选；HeyGem-Lite-模型包解压后所在目录（含 docker-compose-lite.yml），「一键接入模型包」会自动填写。",
    },
]


def _mask_secret(value: str) -> str:
    value = (value or "").strip()
    if not value:
        return ""
    if len(value) <= 8:
        return value[:2] + "****"
    return value[:4] + "****" + value[-4:]


def _save_dotenv(updates: dict[str, str]) -> None:
    """将更新项写回用户本机 .env，保留原有注释与格式；空值表示删除该项。"""
    path = CONFIG_PATH
    lines = path.read_text(encoding="utf-8-sig").splitlines() if path.is_file() else []
    existing = set()
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            existing.add(stripped.partition("=")[0].strip())
    for key, value in updates.items():
        if key not in existing and value:
            lines.append(f"{key}={value}")
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        name = stripped.partition("=")[0].strip()
        if name in updates:
            lines[i] = f"{name}={updates[name]}" if updates[name] else ""
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


@app.get("/api/providers/{provider}/models")
def list_provider_models(provider: str) -> dict[str, Any]:
    """拉取可安全登记为文本对话的 OpenAI 兼容模型。"""
    config = PROVIDER_COMPAT_ENDPOINTS.get(provider)
    if not config:
        raise HTTPException(404, "该供应商暂不支持在线获取模型列表。")
    key = (local_setting(config["key_setting"]) or "").strip()
    if not key:
        raise HTTPException(400, f"请先填写{config['title']}的 API Key，再获取模型列表。")
    try:
        response = httpx.get(
            f"{config['base_url'].rstrip('/')}/models",
            headers={"Authorization": f"Bearer {key}"},
            timeout=httpx.Timeout(connect=15, read=60, write=15, pool=15),
        )
    except (httpx.HTTPError, OSError) as error:
        raise HTTPException(502, f"无法连接{config['title']}：{error}") from error
    if not response.is_success:
        raise HTTPException(502, f"{config['title']} 返回 HTTP {response.status_code}：{response.text[:200]}")
    try:
        entries = response.json().get("data") or []
        all_models = {str(item.get("id")) for item in entries if isinstance(item, dict) and item.get("id")}
        models = sorted(model for model in all_models if is_generic_text_model(model))
    except ValueError as error:
        raise HTTPException(502, f"{config['title']} 返回的模型列表无法解析。") from error
    return {"models": models, "filtered": len(all_models) - len(models)}


@app.post("/api/providers/{provider}/add-models")
def add_provider_models(provider: str, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """把用户勾选的模型注册为该供应商的 OpenAI 兼容服务连接，立即在“模型分配”中可选。"""
    config = PROVIDER_COMPAT_ENDPOINTS.get(provider)
    if not config:
        raise HTTPException(404, "该供应商暂不支持添加在线模型。")
    submitted = payload.get("models")
    if not isinstance(submitted, list):
        raise HTTPException(400, "请先勾选要添加的模型。")
    cleaned = sorted({str(item).strip() for item in submitted if str(item).strip()})
    if not cleaned:
        raise HTTPException(400, "请先勾选要添加的模型。")
    if any(not is_generic_text_model(model) for model in cleaned):
        raise HTTPException(400, "语音模型需要专用适配，不能登记到通用 OpenAI 文本接口。")
    grouped = {"chat": cleaned}
    key = (local_setting(config["key_setting"]) or "").strip()
    existing = next(
        (item for item in _load_service_connections() if item.get("base_url") == config["base_url"]),
        None,
    )
    if existing:
        merged: dict[str, list[str]] = {}
        for connection in existing.get("connections", []):
            capability = str(connection.get("capability") or "")
            if capability == "chat":
                merged.setdefault(capability, []).extend(connection.get("models", []))
        for capability, models in grouped.items():
            merged[capability] = sorted(set(merged.setdefault(capability, [])) | set(models))
        update_service_connection(existing["id"], {
            "name": existing.get("name"),
            "base_url": config["base_url"],
            "api_key": key,
            "connections": [
                {"capability": capability, "models": models}
                for capability, models in sorted(merged.items())
            ],
        })
        service_id = existing["id"]
    else:
        result = add_service_connection({
            "name": f"{config['title']}（OpenAI 兼容）",
            "base_url": config["base_url"],
            "api_key": key,
            "connections": [
                {"capability": capability, "models": models}
                for capability, models in sorted(grouped.items())
            ],
        })
        service_id = result["service_connections"][0]["id"]
    return {"ok": True, "added": cleaned, "service_id": service_id}


@app.get("/api/settings")
def get_settings() -> dict[str, Any]:
    fields = []
    for field in SETTINGS_FIELDS:
        key = field["key"]
        value = (local_setting(key) or "").strip()
        fields.append(
            {
                **field,
                "configured": bool(value),
                "masked": _mask_secret(value),
                "source": "local" if value else "missing",
            }
        )
    modules = []
    for module in SETTINGS_MODULES:
        availability = {
            value: _is_builtin_model_available(module["id"], value)
            for value in module["models"]
        }
        # Keep the complete catalogue in the settings response.  The canvas
        # still receives only usable choices, but Settings can explain why a
        # local model is unavailable instead of making it look unsupported.
        models = [value for value in module["models"] if availability[value]]
        options = [
            {
                "value": value,
                "label": model_label(value),
                "source": "built-in",
                "available": availability[value],
                "disabled": not availability[value],
                "availability_note": (
                    "未安装 FunASR 依赖或运行时不可用"
                    if value.startswith("local:funasr-") and not availability[value]
                    else "尚未配置对应服务"
                ),
            }
            for value in module["models"]
        ]
        options.extend(_service_model_options(module["id"]))
        modules.append({**module, "models": models, "all_models": list(module["models"]), "options": options})
    return {
        "ok": True,
        "fields": fields,
        "modules": modules,
        "model_routes": _load_model_routes(),
        "provider_titles": PROVIDER_TITLES,
        "custom_providers": _public_custom_providers(),
        "local_ollama": _load_local_ollama(),
        "service_connections": _public_service_connections(),
        "digital_human_providers": [
            {"id": item.id, "label": item.label, "kind": item.kind, "configured": item.configured, "note": item.note}
            for item in provider_infos(_LOCAL_SETTINGS)
        ],
        "local_engines": {
            "ollama": local_ollama_engine_status(),
            "funasr": local_funasr_engine_status(),
            "musetalk": local_musetalk_engine_status(),
            "qwen3_tts": qwen_engine_status(USER_DATA_ROOT, _LOCAL_SETTINGS),
            "heygem": heygem_lite_status(USER_DATA_ROOT, _LOCAL_SETTINGS),
        },
        "service_capabilities": [
            {"id": key, "title": value["title"], "adapter": value["adapter"], "available": value["adapter"] in SUPPORTED_SERVICE_ADAPTERS}
            for key, value in SERVICE_CAPABILITIES.items()
        ],
    }


@app.get("/api/local-engines/musetalk")
def get_musetalk_engine() -> dict[str, Any]:
    """检查本机 MuseTalk 引擎，不启动推理、不下载文件。"""
    return local_musetalk_engine_status()


@app.post("/api/local-engines/musetalk/start")
def start_musetalk_engine() -> dict[str, Any]:
    """Start an installed MuseTalk engine on an internal loopback address."""
    try:
        ensure_local_musetalk_runtime()
    except RuntimeError as error:
        raise HTTPException(409, str(error)) from error
    return {"ok": True, "engine": local_musetalk_engine_status()}


@app.post("/api/local-engines/musetalk/download")
def download_musetalk_engine() -> dict[str, Any]:
    """Download the reviewed MuseTalk source/weights manifest in background."""
    return {"ok": True, "download": start_musetalk_download(USER_DATA_ROOT), "engine": local_musetalk_engine_status()}


@app.get("/api/local-engines/qwen3-tts")
def get_qwen3_tts_engine() -> dict[str, Any]:
    return qwen_engine_status(USER_DATA_ROOT, _LOCAL_SETTINGS)


@app.post("/api/local-engines/import-bundle")
def import_model_bundle(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """一键接入模型包：识别发布包里的模型目录与自带运行时并写入设置。

    path 可以是包根目录（afan-voice-engines / afan-musetalk-bundle），
    也可以是其上层文件夹——分析器向下最多看两层，自动发现包结构。
    只写入识别到的项；没识别到任何内容时返回提示，不改动现有配置。
    """
    raw = str(payload.get("path") or "").strip().strip('"')
    if not raw:
        raise HTTPException(400, "请先选择模型包所在文件夹。")
    root = Path(raw).expanduser()
    if not root.is_dir():
        raise HTTPException(400, "该路径不是文件夹，请重新选择。")
    analysis = analyze_model_bundle(root)
    updates: dict[str, str] = {}
    mapping = {
        "funasr_dir": "FUNASR_ENGINE_ROOT",
        "qwen_dir": "QWEN_TTS_ENGINE_ROOT",
        "musetalk_dir": "MUSETALK_ENGINE_ROOT",
        "heygem_dir": "HEYGEM_PACKAGE_DIR",
        "funasr_python": "FUNASR_PYTHON",
        "qwen_tts_python": "QWEN_TTS_PYTHON",
        "musetalk_python": "MUSETALK_PYTHON",
    }
    for key, setting in mapping.items():
        value = str(analysis.get(key) or "").strip()
        if value:
            updates[setting] = value
    if updates:
        _save_dotenv(updates)
        load_dotenv(CONFIG_PATH)
    found = [key for key in ("funasr_dir", "qwen_dir", "musetalk_dir", "heygem_dir", "funasr_python", "qwen_tts_python", "musetalk_python") if analysis.get(key)]
    return {
        "ok": True,
        "analysis": analysis,
        "applied": updates,
        "found": found,
        "settings": get_settings(),
    }


@app.post("/api/local-engines/qwen3-tts/start")
def start_qwen3_tts_engine() -> dict[str, Any]:
    try:
        ensure_local_qwen3_tts_runtime()
    except RuntimeError as error:
        raise HTTPException(409, str(error)) from error
    return {"ok": True, "engine": qwen_engine_status(USER_DATA_ROOT, _LOCAL_SETTINGS)}


@app.get("/api/local-engines/heygem")
def get_heygem_engine() -> dict[str, Any]:
    """只读检测 HeyGem Lite（Docker）模型包与本机服务状态。"""
    return heygem_lite_status(USER_DATA_ROOT, _LOCAL_SETTINGS)


@app.post("/api/local-engines/heygem/setup-env")
def setup_heygem_env() -> dict[str, Any]:
    """一键准备 Docker 运行环境：下载安装包 → 启用 WSL2 → 静默安装 → 启动 Docker。

    已装好的步骤自动跳过；期间会弹出 UAC 授权，某些机器完成后需要重启，
    重启后再次点击即可从断点继续。
    """
    env = heygem_preflight(data_root=USER_DATA_ROOT)
    if env.get("docker_running"):
        return {"ok": True, "env": env}
    if env.get("blocked") == "virtualization":
        raise HTTPException(409, "这台电脑没有开启 CPU 虚拟化（VT-x/AMDV），需要在 BIOS/UEFI 中开启后才能继续。")
    task = start_heygem_env_setup(USER_DATA_ROOT)
    if task.get("status") == "failed":
        raise HTTPException(409, str(task.get("error") or "环境准备失败。"))
    return {"ok": True, "env": heygem_preflight(data_root=USER_DATA_ROOT), "task": task}


@app.post("/api/local-engines/heygem/start")
def start_heygem_engine(payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
    """一键接入 HeyGem Lite：解压模型包 → 导入镜像 → 启动容器。

    服务已在运行时直接返回；否则在后台线程执行接入，前端通过
    GET /api/local-engines/heygem 轮询 task 进度。
    """
    engine = heygem_lite_status(USER_DATA_ROOT, _LOCAL_SETTINGS)
    if engine.get("service") or engine.get("container") == "running":
        return {"ok": True, "engine": engine}
    path = str(payload.get("path") or "").strip().strip('"')
    if path and not Path(path).expanduser().is_dir():
        raise HTTPException(400, "该路径不是文件夹，请重新选择。")
    task = start_heygem_prepare(
        USER_DATA_ROOT,
        _LOCAL_SETTINGS,
        path=path or None,
        persist=lambda updates: (_save_dotenv(updates), load_dotenv(CONFIG_PATH)),
    )
    if task.get("status") == "failed":
        raise HTTPException(409, str(task.get("error") or "HeyGem Lite 接入失败。"))
    return {"ok": True, "engine": heygem_lite_status(USER_DATA_ROOT, _LOCAL_SETTINGS)}


@app.post("/api/local-engines/musetalk/install")
def install_musetalk_engine(payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
    """异步安装已审核的 MuseTalk 引擎包。

    下载地址必须由用户/发布者明确提供，避免程序静默拉取未知模型。安装
    在后台线程执行，前端通过 GET 接口读取进度。
    """
    try:
        result = start_install(
            USER_DATA_ROOT,
            url=str(payload.get("url") or local_setting("MUSETALK_ENGINE_URL") or ""),
            root=str(payload.get("root") or local_setting("MUSETALK_ENGINE_ROOT") or "") or None,
            sha256=str(payload.get("sha256") or "").strip() or None,
        )
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    return {"ok": True, "engine": result}


@app.get("/api/data-location")
def get_data_location() -> dict[str, Any]:
    """当前数据目录信息：实际位置、默认位置、是否已自定义。"""
    return {
        "ok": True,
        "current": str(USER_DATA_ROOT),
        "is_custom": USER_DATA_ROOT != _default_data_root(),
        "default": str(_default_data_root()),
        "usage": _dir_usage(USER_DATA_ROOT),
        "requires_restart": True,
    }


def _pick_folder_dialog(title: str, initialdir: str | None = None) -> str:
    """弹出系统文件夹选择框，返回选中路径（取消返回空串）。

    优先 tkinter（源码开发环境）；打包版里 tkinter 不可用时退回
    PowerShell 的 FolderBrowserDialog——Windows 必有 PowerShell，零额外依赖。
    """
    try:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        try:
            kwargs: dict[str, Any] = {"title": title}
            if initialdir:
                kwargs["initialdir"] = initialdir
            return filedialog.askdirectory(**kwargs) or ""
        finally:
            root.destroy()
    except Exception:
        pass

    script = (
        "Add-Type -AssemblyName System.Windows.Forms | Out-Null; "
        "$d = New-Object System.Windows.Forms.FolderBrowserDialog; "
        f"$d.Description = '{title}'; "
        "if ($d.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) "
        "{ Write-Output $d.SelectedPath }"
    )
    result = subprocess.run(
        ["powershell", "-NoProfile", "-STA", "-Command", script],
        capture_output=True, text=True, timeout=600,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return result.stdout.strip()


@app.get("/api/data-location/choose")
def choose_data_location() -> dict[str, Any]:
    """打开系统文件夹选择器，返回用户选中的目录；取消时 path 为空。"""
    try:
        selected = _pick_folder_dialog(
            "选择数据保存文件夹",
            str(USER_DATA_ROOT if USER_DATA_ROOT.exists() else _default_data_root()),
        )
    except Exception as error:
        raise HTTPException(501, f"无法打开系统文件夹选择器：{error}") from error
    return {"ok": True, "path": selected}


@app.get("/api/local-engines/choose-dir")
def choose_engine_dir() -> dict[str, Any]:
    """打开系统文件夹选择器，返回选中的模型目录绝对路径；取消时 path 为空。

    网页自身拿不到本地文件的真实路径（浏览器安全沙箱限制，与前端框架
    无关），所以和「数据保存位置」一样由本机后端进程代开原生选择框。
    """
    try:
        selected = _pick_folder_dialog("选择模型包所在文件夹")
    except Exception as error:
        raise HTTPException(501, f"无法打开系统文件夹选择器：{error}") from error
    return {"ok": True, "path": selected}


@app.post("/api/data-location")
def set_data_location(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """把数据目录迁移到用户指定的新位置（拷贝 data/ 全部内容）。

    写入 data_location.txt 标记文件，重启后新位置生效；迁移成功后旧目录
    里的 data/ 改名为 data-migrated-<时间戳> 备份，不立即删除。
    """
    raw = str(payload.get("path") or "").strip().strip('"')
    if not raw:
        raise HTTPException(400, "请填写新的数据目录路径。")
    target = Path(raw)
    if not target.is_absolute():
        raise HTTPException(400, "请填写完整路径（例如 D:\\口播数据）。")
    if target == _default_data_root():
        raise HTTPException(400, "目标路径与默认位置相同，无需迁移。")
    if USER_DATA_ROOT == SOURCE_ROOT and not getattr(sys, "frozen", False):
        raise HTTPException(400, "开发模式下数据随项目目录，不支持迁移。")
    try:
        target.mkdir(parents=True, exist_ok=True)
        if not os.access(target, os.W_OK):
            raise OSError("目录不可写")
    except OSError as error:
        raise HTTPException(400, f"无法使用该目录：{error}") from error

    source_data = USER_DATA_ROOT / "data"
    target_data = target / "data"
    moved_jobs = 0
    config_copied = False
    if source_data.exists():
        if target_data.exists():
            raise HTTPException(400, "目标目录下已存在 data 文件夹，请换一个空目录。")
        shutil.copytree(source_data, target_data)
        try:
            moved_jobs = len([p for p in (target_data / "jobs").iterdir() if p.is_dir()])
        except OSError:
            moved_jobs = 0
    # API keys and other local settings live beside data/ and must follow the
    # data migration.  Never overwrite a config already present in the target.
    source_config = USER_DATA_ROOT / ".env"
    target_config = target / ".env"
    if source_config.is_file() and not target_config.exists():
        try:
            shutil.copy2(source_config, target_config)
            config_copied = True
        except OSError as error:
            raise HTTPException(400, f"数据已复制，但本地配置同步失败：{error}") from error
    if source_data.exists():
        # 旧目录改名备份（不删除，防迁移失败丢数据）。
        stamp = now().replace(":", "").replace("-", "")[:15]
        try:
            source_data.rename(source_data.with_name(f"data-migrated-{stamp}"))
        except OSError:
            pass  # 改名失败不致命：标记文件已写，重启后用新位置。
    _override_file.write_text(str(target), encoding="utf-8")
    config_note = "本地配置已同步。" if config_copied else "目标目录已有本地配置，未覆盖。"
    return {
        "ok": True,
        "target": str(target),
        "moved_jobs": moved_jobs,
        "message": f"迁移完成（{moved_jobs} 个项目）。{config_note}重启软件后新位置生效，旧数据已保留备份。",
    }


@app.post("/api/data-location/reset")
def reset_data_location() -> dict[str, Any]:
    """把自定义目录的数据和本地配置搬回默认位置，再清除位置标记。"""
    default_root = _default_data_root()
    if USER_DATA_ROOT == default_root:
        try:
            _override_file.unlink(missing_ok=True)
        except OSError as error:
            raise HTTPException(500, f"无法重置：{error}") from error
        return {"ok": True, "message": "当前已经是默认位置，重启软件后生效。"}

    source_data = USER_DATA_ROOT / "data"
    target_data = default_root / "data"
    stamp = now().replace(":", "").replace("-", "")[:15]
    backup_data: Path | None = None
    backup_config: Path | None = None
    try:
        default_root.mkdir(parents=True, exist_ok=True)
        if target_data.exists():
            backup_data = default_root / f"data-reset-backup-{stamp}"
            target_data.rename(backup_data)
        if source_data.exists():
            shutil.copytree(source_data, target_data)

        source_config = USER_DATA_ROOT / ".env"
        target_config = default_root / ".env"
        if source_config.is_file():
            if target_config.exists():
                backup_config = default_root / f".env-reset-backup-{stamp}"
                target_config.rename(backup_config)
            shutil.copy2(source_config, target_config)
        _override_file.unlink(missing_ok=True)
    except OSError as error:
        # Keep the custom location marker if anything failed; the running
        # process can continue using the original data after a restart.
        if target_data.exists() and backup_data is not None:
            try:
                shutil.rmtree(target_data)
                backup_data.rename(target_data)
            except OSError:
                pass
        raise HTTPException(500, f"恢复默认位置失败：{error}") from error

    backup_note = "默认位置原有数据已保留备份。" if backup_data else ""
    return {"ok": True, "message": f"数据已搬回默认位置。{backup_note}重启软件后生效。"}


@app.post("/api/settings")
def save_settings(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """保存密钥到本地 .env。空字符串 / 缺失字段表示不修改。仅监听本机地址。

    本地引擎目录字段例外：空字符串表示清除该目录、恢复默认标准目录。
    """
    allowed = {f["key"] for f in SETTINGS_FIELDS}
    engine_root_keys = {"MUSETALK_ENGINE_ROOT", "FUNASR_ENGINE_ROOT", "QWEN_TTS_ENGINE_ROOT", "HEYGEM_PACKAGE_DIR"}
    updates: dict[str, str] = {}
    for key, value in payload.items():
        if key not in allowed:
            continue
        text = str(value or "").strip()
        if not text:
            if key in engine_root_keys:
                updates[key] = ""
            continue
        if "\n" in text or "\r" in text or text.startswith("#"):
            raise HTTPException(400, f"{key} 的值包含非法字符，请检查后重试")
        updates[key] = text
    if updates:
        _save_dotenv(updates)
        load_dotenv(CONFIG_PATH)
    return get_settings()


@app.post("/api/settings/model-routes")
def save_model_routes(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """保存各工作环节的默认模型；只接受已实现的兼容协议。"""
    submitted = payload.get("routes", {})
    if not isinstance(submitted, dict):
        raise HTTPException(400, "模型设置格式不正确。")
    routes = _load_model_routes()
    for step in routes:
        if step not in submitted:
            continue
        model = str(submitted[step] or "").strip()
        if not _is_model_option(step, model):
            raise HTTPException(400, "所选模型不可用，请重新选择。")
        routes[step] = model
    _save_model_routes(routes)
    return get_settings()


def _public_service_connections() -> list[dict[str, Any]]:
    result = []
    for provider in _load_service_connections():
        connections = []
        for connection in provider["connections"]:
            if connection["adapter"] not in SUPPORTED_SERVICE_ADAPTERS:
                continue
            definition = SERVICE_CAPABILITIES[connection["capability"]]
            connections.append({
                "capability": connection["capability"], "title": definition["title"], "adapter": connection["adapter"],
                "models": connection["models"], "available": True,
            })
        if connections:
            result.append({"id": provider["id"], "name": provider["name"], "base_url": provider["base_url"], "kind": provider.get("kind", "compatible"),
                           "masked": _mask_secret(provider["api_key"]), "connections": connections})
    return result


PROVIDER_COMPAT_ENDPOINTS = {
    # 预设供应商的 OpenAI 兼容端点：用于在线拉取模型列表与执行用户添加的模型。
    "dashscope": {
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "key_setting": "DASHSCOPE_API_KEY",
        "title": "阿里云百炼",
    },
    "mimo": {
        "base_url": MIMO_API_URL.rsplit("/chat/completions", 1)[0],
        "key_setting": "MIMO_API_KEY",
        "title": "小米 MiMo",
    },
}


def infer_service_capability(model_id: str) -> str:
    """Generic OpenAI-compatible registrations are deliberately text-only.

    A model-list entry cannot prove that the provider implements the same wire
    protocol for speech. ASR/TTS must be exposed only after we add a dedicated
    provider adapter, so imported models always become chat models.
    """
    return "chat"


@app.post("/api/service-connections")
def add_service_connection(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    name = str(body.get("name") or "").strip()
    api_key = str(body.get("api_key") or "").strip()
    if not name or len(name) > 80 or "\n" in name or "\r" in name:
        raise HTTPException(400, "请填写不超过 80 个字符的供应商名称。")
    base_url = _clean_service_url(str(body.get("base_url") or ""))
    submitted = body.get("connections")
    if not isinstance(submitted, list):
        raise HTTPException(400, "请至少配置一项服务能力和模型名。")
    connections = []
    for item in submitted:
        if not isinstance(item, dict):
            continue
        capability = str(item.get("capability") or "")
        adapter = str(item.get("adapter") or SERVICE_CAPABILITIES.get(capability, {}).get("adapter") or "")
        models = _clean_service_models(item.get("models"))
        if capability not in SERVICE_CAPABILITIES or not models:
            continue
        if capability == "chat" and any(not is_generic_text_model(model) for model in models):
            raise HTTPException(400, "语音模型需要专用适配，不能登记到通用 OpenAI 文本接口。")
        # A provider-specific adapter may be saved for documentation, but it
        # is deliberately not selectable until application code supports it.
        connections.append({"capability": capability, "adapter": adapter, "models": models})
    if not connections:
        raise HTTPException(400, "请至少填写一个能力对应的模型名。")
    kind = str(body.get("kind") or "compatible")
    if kind not in {"known", "compatible", "local"}:
        raise HTTPException(400, "服务类型不正确。")
    if kind == "compatible" and any(connection["capability"] != "chat" for connection in connections):
        raise HTTPException(400, "通用 OpenAI 兼容服务目前只支持文本对话；语音识别和配音请使用已适配的专用供应商。")
    items = _load_service_connections()
    items.append({"id": uuid.uuid4().hex[:12], "name": name, "base_url": base_url, "api_key": api_key, "kind": kind, "connections": connections})
    _save_service_connections(items)
    return get_settings()


@app.post("/api/service-connections/discover-models")
def discover_service_models(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """Discover models before saving an OpenAI-compatible service account."""
    base_url = _clean_service_url(str(body.get("base_url") or ""))
    api_key = str(body.get("api_key") or "").strip()
    # When editing, an empty key means “keep the locally saved key”, just as
    # it does for the eventual save operation.
    if not api_key:
        provider_id = str(body.get("provider_id") or "")
        current = next((item for item in _load_service_connections() if item["id"] == provider_id), None)
        if current and current["base_url"] == base_url:
            api_key = current["api_key"]
    models = _discover_service_models(base_url, api_key)
    return {"ok": True, "models": models}


@app.post("/api/service-connections/{provider_id}/delete")
def delete_service_connection(provider_id: str) -> dict[str, Any]:
    _save_service_connections([item for item in _load_service_connections() if item["id"] != provider_id])
    return get_settings()


@app.post("/api/service-connections/{provider_id}/update")
def update_service_connection(provider_id: str, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """Update a local service account without requiring the user to retype its Key."""
    items = _load_service_connections()
    current = next((item for item in items if item["id"] == provider_id), None)
    if not current:
        raise HTTPException(404, "未找到这个服务账号。")
    name = str(body.get("name") or "").strip()
    if not name or len(name) > 80 or "\n" in name or "\r" in name:
        raise HTTPException(400, "请填写不超过 80 个字符的供应商名称。")
    base_url = _clean_service_url(str(body.get("base_url") or ""))
    submitted = body.get("connections")
    if not isinstance(submitted, list):
        raise HTTPException(400, "请至少配置一项服务能力和模型名。")
    connections = []
    for item in submitted:
        if not isinstance(item, dict):
            continue
        capability = str(item.get("capability") or "")
        adapter = str(item.get("adapter") or SERVICE_CAPABILITIES.get(capability, {}).get("adapter") or "")
        models = _clean_service_models(item.get("models"))
        if capability in SERVICE_CAPABILITIES and models:
            if capability == "chat" and any(not is_generic_text_model(model) for model in models):
                raise HTTPException(400, "语音模型需要专用适配，不能登记到通用 OpenAI 文本接口。")
            connections.append({"capability": capability, "adapter": adapter, "models": models})
    if not connections:
        raise HTTPException(400, "请至少填写一个能力对应的模型名。")
    api_key = str(body.get("api_key") or "").strip() or current.get("api_key", "")
    kind = str(body.get("kind") or current.get("kind") or "compatible")
    if kind not in {"known", "compatible", "local"}:
        raise HTTPException(400, "服务类型不正确。")
    if kind == "compatible" and any(connection["capability"] != "chat" for connection in connections):
        raise HTTPException(400, "通用 OpenAI 兼容服务目前只支持文本对话；语音识别和配音请使用已适配的专用供应商。")
    replacement = {"id": provider_id, "name": name, "base_url": base_url, "api_key": api_key, "kind": kind, "connections": connections}
    _save_service_connections([replacement if item["id"] == provider_id else item for item in items])
    return get_settings()


# ── 自定义供应商（OpenAI 兼容，可添加任意多家）──
def _public_custom_providers() -> list[dict[str, Any]]:
    return [
        {
            "id": c["id"],
            "name": c.get("name") or "自定义供应商",
            "base_url": c["base_url"],
            "model": c.get("model") or "gpt-4o-mini",
            "masked": _mask_secret(c["api_key"]),
        }
        for c in _load_custom_providers()
    ]


@app.get("/api/custom-providers")
def list_custom_providers() -> list[dict[str, Any]]:
    return _public_custom_providers()


@app.post("/api/custom-providers")
async def add_custom_provider(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    name = str(body.get("name") or "").strip()
    base_url = str(body.get("base_url") or "").strip()
    api_key = str(body.get("api_key") or "").strip()
    model = str(body.get("model") or "").strip() or "gpt-4o-mini"
    for label, value in (("名称", name), ("接口地址", base_url), ("API Key", api_key)):
        if not value:
            raise HTTPException(400, f"{label}不能为空")
        if "\n" in value or "\r" in value:
            raise HTTPException(400, f"{label}包含非法字符")
    items = _load_custom_providers()
    items.append({"id": uuid.uuid4().hex[:12], "name": name, "base_url": base_url, "api_key": api_key, "model": model, "protocol": "openai"})
    _save_custom_providers(items)
    return {"ok": True, "custom_providers": _public_custom_providers()}


@app.post("/api/custom-providers/{provider_id}/delete")
def delete_custom_provider(provider_id: str) -> dict[str, Any]:
    items = [c for c in _load_custom_providers() if c["id"] != provider_id]
    _save_custom_providers(items)
    # 删除供应商后，使用它的默认模型恢复为自动选择，避免后续任务引用失效配置。
    routes = _load_model_routes()
    for step, model in routes.items():
        if model == f"custom:{provider_id}":
            routes[step] = "auto"
    _save_model_routes(routes)
    return {"ok": True, "custom_providers": _public_custom_providers()}


def _ollama_tags_url(base_url: str) -> str:
    root = base_url.rstrip("/")
    if root.endswith("/v1"):
        root = root[:-3]
    return root + "/api/tags"


@app.post("/api/local-ollama/test")
def test_local_ollama(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """Check the user's local Ollama only; never proxy arbitrary remote hosts."""
    base_url = _normalize_local_ollama_url(str(body.get("base_url") or "http://127.0.0.1:11434/v1"))
    try:
        response = httpx.get(_ollama_tags_url(base_url), timeout=5, trust_env=False)
    except (httpx.HTTPError, OSError) as error:
        raise HTTPException(503, f"无法连接 Ollama：{error}") from error
    if not response.is_success:
        raise HTTPException(503, f"Ollama 返回 HTTP {response.status_code}，请确认服务正在运行。")
    try:
        models = [str(item["name"]).strip() for item in response.json().get("models", []) if item.get("name")]
    except (ValueError, AttributeError, TypeError) as error:
        raise HTTPException(503, "Ollama 返回的模型列表格式不正确。") from error
    return {"ok": True, "base_url": base_url, "models": models}


@app.post("/api/local-ollama")
def save_local_ollama(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    base_url = _normalize_local_ollama_url(str(body.get("base_url") or "http://127.0.0.1:11434/v1"))
    model = str(body.get("model") or "").strip()
    if not model:
        raise HTTPException(400, "请选择或填写 Ollama 已安装的模型名。")
    if "\n" in model or "\r" in model:
        raise HTTPException(400, "模型名包含非法字符。")
    _save_local_ollama({"base_url": base_url, "model": model})
    return get_settings()


@app.post("/api/local-ollama/delete")
def delete_local_ollama() -> dict[str, Any]:
    LOCAL_OLLAMA_PATH.unlink(missing_ok=True)
    routes = _load_model_routes()
    for step, model in routes.items():
        if model == "local:ollama":
            routes[step] = "auto"
    _save_model_routes(routes)
    return get_settings()


@app.post("/api/local-engines/ollama/select")
def select_detected_ollama_model(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """Attach one model already served by the local Ollama process.

    The endpoint deliberately has no URL input: this user-facing flow only
    trusts the conventional loopback process discovered by ``/api/tags``.
    """
    engine = local_ollama_engine_status()
    if not engine["running"]:
        raise HTTPException(409, "未检测到正在运行的本机 Ollama。请先启动 Ollama，再重新检测。")
    model = str(body.get("model") or "").strip()
    if model not in engine["models"]:
        raise HTTPException(400, "请选择本机 Ollama 已下载的模型。")
    _save_local_ollama({"base_url": str(engine["base_url"]), "model": model})
    return get_settings()


@app.get("/api/health")
def health() -> dict[str, Any]:
    """暴露全部启用模型与本地依赖的可用性，缺 key 时前端可直接降级对应选项。"""
    return {
        "ok": True,
        "dashscope_key": bool(local_setting("DASHSCOPE_API_KEY")),
        "dashscope_workspace": bool(local_setting("DASHSCOPE_WORKSPACE_ID")),
        "mimo_key": bool(local_setting("MIMO_API_KEY")),
        "funasr": funasr_ready(),
        "ollama": _load_local_ollama() is not None,
        "ffmpeg": bool(
            (local_setting("FFMPEG_PATH") or os.getenv("FFMPEG_PATH") or "").strip()
            or (RESOURCE_ROOT / "bin" / "ffmpeg.exe").is_file()
            or resolve_ffmpeg(USER_DATA_ROOT)
            or shutil.which("ffmpeg")
        ),
    }


@app.get("/api/workflow")
def workflow_contract() -> dict[str, Any]:
    """只读暴露工作流契约；UI 提示、CLI guide 与外部 AI 提示词共用同一份真相源。"""
    return {"ok": True, "workflow": _WORKFLOW_CONTRACT}


@app.get("/api/digital-human/providers")
def list_digital_human_providers() -> dict[str, Any]:
    """Return the local lip-sync adapters and their configuration status.

    This endpoint is deliberately diagnostic only: it never downloads model
    weights or starts a GPU process.  The UI can therefore explain what needs
    to be installed before a user selects MuseTalk or HeyGem.
    """
    return {
        "ok": True,
        "providers": [
            {
                "id": item.id,
                "label": item.label,
                "kind": item.kind,
                "configured": item.configured,
                "note": item.note,
            }
            for item in provider_infos(_LOCAL_SETTINGS)
        ],
    }


@app.get("/api/local-voice/qwen3-tts")
def qwen3_tts_health() -> dict[str, Any]:
    """按需启动并检查本机 Qwen3-TTS；普通用户不需要填写地址。"""
    if not (local_setting("QWEN_TTS_BASE_URL") or "").strip():
        try:
            ensure_local_qwen3_tts_runtime()
        except RuntimeError as error:
            return {"ok": False, "configured": False, "error": str(error)}
    return LocalQwenTTS(_LOCAL_SETTINGS).health()


@app.get("/api/jobs")
def list_jobs() -> list[dict[str, Any]]:
    return [asdict(job) for job in sorted(store.jobs.values(), key=lambda item: item.created_at, reverse=True) if store.is_saved(job.id)]


@app.get("/api/library")
def list_library(kind: str | None = None) -> list[dict[str, Any]]:
    """List the user's local reusable assets; files never leave this machine."""
    if kind and kind not in LIBRARY_KINDS:
        raise HTTPException(400, "未知的素材类型。")
    return [_library_public(item) for item in _read_library() if not kind or item.get("kind") == kind]


@app.post("/api/library/upload")
async def upload_library_asset(
    kind: str = Form(...),
    name: str = Form(""),
    text: str = Form(""),
    file: UploadFile | None = File(None),
) -> dict[str, Any]:
    """Save a reusable local voice, person video, video, or script."""
    if kind not in LIBRARY_KINDS:
        raise HTTPException(400, "未知的素材类型。")
    title = (name or "").strip()
    if not title:
        title = Path(file.filename).stem if file and file.filename else LIBRARY_KINDS[kind]
    asset_id = uuid.uuid4().hex[:12]
    item: dict[str, Any] = {
        "id": asset_id, "kind": kind, "name": title[:80], "created_at": now(),
    }
    if kind == "script":
        content = (text or "").strip()
        if not content:
            raise HTTPException(400, "请填写要保存的文案。")
        if len(content) > 20000:
            raise HTTPException(413, "文案不能超过 20000 个字符。")
        item["text"] = content
    else:
        if not file or not file.filename:
            raise HTTPException(400, "请选择要保存的文件。")
        suffix = Path(file.filename).suffix.lower()
        content_type = (file.content_type or "").lower()
        if kind == "voice" and (suffix not in LIBRARY_AUDIO_SUFFIXES or not content_type.startswith("audio/")):
            raise HTTPException(400, "音色素材请上传 MP3、WAV、M4A、FLAC、OGG 或 WebM 音频。")
        if kind in {"person", "video"} and (suffix not in LIBRARY_VIDEO_SUFFIXES or not content_type.startswith("video/")):
            raise HTTPException(400, "视频素材请上传 MP4、MOV、M4V、WebM、AVI 或 MKV。")
        target = LIBRARY_DIR / f"{asset_id}{suffix}"
        size = 0
        with target.open("wb") as output:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > 500 * 1024 * 1024:
                    target.unlink(missing_ok=True)
                    raise HTTPException(413, "素材不能超过 500 MB。")
                output.write(chunk)
        item.update(filename=target.name, original_name=file.filename, size=size, content_type=content_type)
    items = _read_library()
    items.insert(0, item)
    _write_library(items)
    return _library_public(item)


@app.get("/api/library/{asset_id}/file")
def library_file(asset_id: str) -> FileResponse:
    item = _library_item(asset_id)
    filename = item.get("filename")
    if not filename:
        raise HTTPException(404, "该文案没有文件。")
    path = (LIBRARY_DIR / filename).resolve()
    if path.parent != LIBRARY_DIR.resolve() or not path.is_file():
        raise HTTPException(404, "素材文件不存在。")
    return FileResponse(path, media_type=item.get("content_type") or "application/octet-stream", filename=item.get("original_name") or path.name)


@app.delete("/api/library/{asset_id}")
def delete_library_asset(asset_id: str) -> dict[str, bool]:
    item = _library_item(asset_id)
    if item.get("filename"):
        (LIBRARY_DIR / str(item["filename"])).unlink(missing_ok=True)
    _write_library([entry for entry in _read_library() if entry.get("id") != asset_id])
    return {"ok": True}


@app.post("/api/projects/{job_id}/library/{asset_id}/use")
async def use_library_asset(
    job_id: str,
    asset_id: str,
    background_tasks: BackgroundTasks,
    authorized: bool = Form(False),
    target: str = Form(""),
) -> dict[str, Any]:
    """Copy a library asset into the current project and apply it to the right step.

    ``target=broll`` 把视频素材追加为一段 B-roll 素材（与手动上传同一命名与
    时长校验规则），而不是覆盖人物视频。
    """
    job = store.get(job_id)
    item = _library_item(asset_id)
    kind = str(item["kind"])
    if kind == "script":
        text = str(item.get("text") or "").strip()
        store.update(job, rewritten_text=text, transcript=text, script_confirmed=False, preview_confirmed=False, current_step=2, stage="已从素材库选择文案")
        return asdict(store.get(job_id))
    source_name = item.get("filename")
    if not source_name:
        raise HTTPException(409, "该素材没有可用文件。")
    source = (LIBRARY_DIR / str(source_name)).resolve()
    if source.parent != LIBRARY_DIR.resolve() or not source.is_file():
        raise HTTPException(404, "素材文件不存在。")
    work = JOBS_DIR / job_id
    if kind in {"voice", "person", "video"} and not authorized:
        raise HTTPException(400, "请确认你拥有这份声音或视频素材的使用授权。")
    if kind == "voice":
        target = work / f"voice-sample-library{source.suffix.lower()}"
        shutil.copy2(source, target)
        store.update(job, voice_mode="upload", voice_sample_name=target.name, voice_sample_label=item.get("name") or item.get("original_name"), preview_audio_name=None, preview_confirmed=False, status="ready", error=None, stage="已从素材库选择声音样本")
    elif kind == "video" and target == "broll":
        # 与手动上传 upload_broll 保持一致：递增命名 broll_N、校验可播放时长、
        # 默认插入点 5s；标题用素材库里的名称便于在剪辑面板辨认。
        suffix = source.suffix.lower() or ".mp4"
        work.mkdir(parents=True, exist_ok=True)
        existing = {c.name for c in job.broll_clips}
        index = 1
        while f"broll_{index}{suffix}" in existing:
            index += 1
        clip_target = work / f"broll_{index}{suffix}"
        shutil.copy2(source, clip_target)
        try:
            clip_duration = duration_seconds(clip_target)
            if clip_duration < 0.2:
                raise RuntimeError("素材时长过短")
            clip_duration = min(60.0, clip_duration)
        except Exception as error:
            safe_unlink(clip_target)
            raise HTTPException(400, "素材库视频无法读取，请先确认该素材可正常播放。") from error
        clips = list(job.broll_clips)
        clips.append(BrollClip(name=clip_target.name, start=5.0, duration=clip_duration, enabled=True, title=str(item.get("name") or clip_target.stem)))
        store.update(job, broll_clips=clips, broll_name=clip_target.name, broll_enabled=True,
                     broll_start=5.0, broll_duration=clip_duration,
                     stage=f"已从素材库添加 B-roll 素材：{item.get('name') or clip_target.name}")
        return asdict(store.get(job_id))
    elif kind in {"person", "video"}:
        target = work / "person.mp4"
        shutil.copy2(source, target)
        # Use the user's library label in the UI; the original filename remains
        # available in the library record and is not a useful workflow label.
        store.update(job, person_name=item.get("name") or item.get("original_name"), person_video_name=target.name, person_status="等待检测人物视频", voice_id=None, voice_sample_name=None, voice_sample_label=None, preview_audio_name=None, preview_duration=0, preview_confirmed=False, output_name=None, edit_output_name=None, error=None)
        background_tasks.add_task(run_person_video, job_id)
    else:
        raise HTTPException(400, "该素材类型暂不支持应用到项目。")
    return asdict(store.get(job_id))


@app.get("/api/jobs/{job_id}")
def read_job(job_id: str) -> dict[str, Any]:
    result = asdict(store.get(job_id))
    result["saved"] = store.is_saved(job_id)
    return result


@app.get("/api/jobs/{job_id}/download")
def download_result(job_id: str) -> FileResponse:
    job = store.get(job_id)
    if not job.output_name:
        raise HTTPException(409, "成片尚未生成")
    path = JOBS_DIR / job.id / job.output_name
    if not path.exists():
        raise HTTPException(404, "成片文件已不存在")
    return FileResponse(path, media_type="video/mp4", filename=f"talking-video-{job.id}.mp4")


@app.post("/api/jobs")
async def create_job(
    background_tasks: BackgroundTasks,
    video: UploadFile = File(...),
    instruction: str = Form(...),
    create_voice: bool = Form(True),
    voice_id: str | None = Form(None),
    consent: bool = Form(False),
) -> dict[str, Any]:
    if not consent:
        raise HTTPException(400, "需要确认已取得人物肖像与声音授权。")
    if not video.filename or not video.content_type.startswith("video/"):
        raise HTTPException(400, "请上传视频文件。")
    if not instruction.strip():
        raise HTTPException(400, "请填写文案改写要求。")
    if not create_voice and not (voice_id or "").strip():
        raise HTTPException(400, "复用音色时必须填写已有 voice_id。")
    job_id = uuid.uuid4().hex[:12]
    work = JOBS_DIR / job_id
    work.mkdir(parents=True)
    suffix = Path(video.filename).suffix.lower() or ".mp4"
    source = work / f"source{suffix}"
    with source.open("wb") as output:
        while chunk := await video.read(1024 * 1024):
            output.write(chunk)
    if source.stat().st_size > 500 * 1024 * 1024:
        source.unlink(missing_ok=True)
        raise HTTPException(413, "视频不能超过 500 MB。")
    try:
        seconds = duration_seconds(source)
    except subprocess.CalledProcessError as error:
        source.unlink(missing_ok=True)
        raise HTTPException(400, "无法读取视频，请使用标准 MP4/MOV 文件。") from error
    if seconds and (seconds < 2 or seconds > MAX_VIDEO_SECONDS):
        source.unlink(missing_ok=True)
        raise HTTPException(400, f"当前版本支持 2–{MAX_VIDEO_SECONDS} 秒人物视频；当前为 {seconds:.1f} 秒。")
    job = Job(
        id=job_id,
        source_name=video.filename,
        instruction=instruction.strip() or "改写为自然、通顺、可直接朗读的口播表达。",
        create_voice=create_voice,
        voice_id=(voice_id or "").strip() or None,
    )
    store.add(job)
    background_tasks.add_task(run_job, job.id)
    return asdict(job)


@app.get("/api/projects")
def list_projects() -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for job in sorted(store.jobs.values(), key=lambda item: item.created_at, reverse=True):
        if not store.is_saved(job.id):
            continue
        # 自我修复：记录里引用的素材文件已丢失（如历史清理）时，清空记录，
        # 避免界面上显示“已添加”却导出时静默跳过。
        stale: dict[str, Any] | None = None
        if job.broll_name and not (JOBS_DIR / job.id / job.broll_name).exists():
            stale = {"broll_name": None, "broll_enabled": False}
        elif job.music_name and not (JOBS_DIR / job.id / job.music_name).exists():
            stale = {"music_name": None}
        if stale:
            store.update(job, **stale)
        results.append(asdict(job))
    return results


@app.post("/api/projects/draft")
def create_draft_project(source_name: str = Form("素材库项目")) -> dict[str, Any]:
    """Create a local empty project for starting from a saved library asset.

    The material library is intentionally usable before the user has generated
    a script.  A draft has no model call and no uploaded media; selecting an
    asset fills the relevant workflow step afterwards.
    """
    project_id = uuid.uuid4().hex[:12]
    (JOBS_DIR / project_id).mkdir(parents=True)
    job = Job(
        id=project_id,
        source_name=source_name.strip()[:80] or "素材库项目",
        instruction=DEFAULT_REWRITE_INSTRUCTION,
        create_voice=True,
        voice_id=None,
        status="ready",
        stage="等待从素材库选择内容",
        current_step=1,
    )
    store.add(job)
    return asdict(job)


@app.post("/api/projects/{job_id}/rename")
def rename_project(job_id: str, name: str = Form(...)) -> dict[str, Any]:
    new_name = name.strip()[:80]
    if not new_name:
        raise HTTPException(400, "项目名称不能为空。")
    store.update(store.get(job_id), source_name=new_name)
    return {"id": job_id, "source_name": new_name}


@app.post("/api/projects/{job_id}/save")
def save_project(job_id: str) -> dict[str, Any]:
    job = store.get(job_id)
    store.persist(job_id)
    return {"id": job_id, "saved": True, "updated_at": job.updated_at}


@app.post("/api/projects/{job_id}/forget")
def forget_project(job_id: str) -> dict[str, Any]:
    store.forget(job_id)
    return {"id": job_id, "forgotten": True}


@app.post("/api/projects/{job_id}/delete")
def delete_project(job_id: str) -> dict[str, Any]:
    """永久删除项目：清除历史记录、内存数据和工作目录。"""
    store.get(job_id)  # validate exists
    store.forget(job_id)
    work_dir = JOBS_DIR / job_id
    if work_dir.exists():
        shutil.rmtree(work_dir, ignore_errors=True)
    return {"id": job_id, "deleted": True}


@app.post("/api/projects/extract-upload")
async def extract_project_upload(
    background_tasks: BackgroundTasks,
    video: UploadFile = File(...),
    instruction: str = Form(""),
    asr_model: str | None = Form(None),
    rewrite_model: str | None = Form(None),
    reference_content_authorized: bool = Form(False),
) -> dict[str, Any]:
    """从本地上传的参考视频提取文案：不经任何下载环节，发行版与受限内容均可使用。"""
    if not reference_content_authorized:
        raise HTTPException(400, "请先确认你拥有该参考内容，或已取得必要的使用授权。")
    if not video.filename or not video.content_type.startswith("video/"):
        raise HTTPException(400, "请上传视频文件。")
    asr_model = selected_model("asr", asr_model)
    rewrite_model = selected_text_model("rewrite", rewrite_model)
    project_id = uuid.uuid4().hex[:12]
    work = JOBS_DIR / project_id
    work.mkdir(parents=True)
    source = work / "source.mp4"
    with source.open("wb") as output:
        while chunk := await video.read(1024 * 1024):
            output.write(chunk)
    if source.stat().st_size > 500 * 1024 * 1024:
        source.unlink(missing_ok=True)
        raise HTTPException(413, "视频不能超过 500 MB。")
    try:
        seconds = duration_seconds(source)
    except subprocess.CalledProcessError as error:
        source.unlink(missing_ok=True)
        raise HTTPException(400, "无法读取视频，请使用标准 MP4/MOV 文件。") from error
    if seconds and seconds < 2:
        source.unlink(missing_ok=True)
        raise HTTPException(400, "视频过短，无法稳定处理口播内容。")
    job = Job(
        id=project_id,
        source_name=Path(video.filename).name[:80] or "本地视频",
        reference_content_authorized=True,
        create_voice=True,
        voice_id=None,
        asr_model=asr_model,
        instruction=instruction.strip() or DEFAULT_REWRITE_INSTRUCTION,
        rewrite_model=rewrite_model,
        stage="等待处理参考内容",
    )
    store.add(job)
    background_tasks.add_task(run_extraction, project_id)
    return asdict(job)


@app.post("/api/projects/ai-script")
def create_ai_script(
    background_tasks: BackgroundTasks,
    prompt: str = Form(...),
    script_model: str | None = Form(None),
) -> dict[str, Any]:
    brief = prompt.strip()
    if not brief:
        raise HTTPException(400, "请输入文案需求。")
    script_model = selected_text_model("script", script_model)
    project_id = uuid.uuid4().hex[:12]
    (JOBS_DIR / project_id).mkdir(parents=True)
    job = Job(
        id=project_id,
        source_name="AI 生成文案",
        instruction=brief,
        create_voice=True,
        voice_id=None,
        rewrite_model=script_model,
        stage="等待 AI 创作文案",
    )
    store.add(job)
    background_tasks.add_task(run_ai_script, project_id, brief, script_model)
    return asdict(job)


@app.post("/api/projects/{job_id}/rewrite")
def rewrite_project(
    job_id: str,
    background_tasks: BackgroundTasks,
    instruction: str = Form(...),
    rewrite_model: str | None = Form(None),
) -> dict[str, Any]:
    job = store.get(job_id)
    if job.status == "running":
        raise HTTPException(409, "当前项目仍在处理中。")
    if not job.transcript:
        raise HTTPException(409, "请先完成第一步。")
    rewrite_model = selected_text_model("rewrite", rewrite_model)
    background_tasks.add_task(run_rewrite, job_id, instruction.strip(), rewrite_model)
    return {"id": job_id, "accepted": True}


@app.post("/api/projects/{job_id}/transcript")
def save_transcript(job_id: str, transcript: str = Form(...)) -> dict[str, Any]:
    job = store.get(job_id)
    if not transcript.strip():
        raise HTTPException(400, "文案不能为空。")
    lines = timed_lines(transcript.strip(), job.reference_duration or job.duration or max(0.0, len(transcript.strip()) / 4.2))
    subtitle = JOBS_DIR / job_id / "source.srt"
    subtitle.write_text(to_srt(lines), encoding="utf-8")
    store.update(job, transcript=transcript.strip(), timeline=lines, subtitle_name=subtitle.name, stage="已保存手动修改的文案")
    return asdict(job)


@app.post("/api/projects/{job_id}/rewritten")
def save_rewritten(job_id: str, rewritten_text: str = Form(...)) -> dict[str, Any]:
    job = store.get(job_id)
    if not rewritten_text.strip():
        raise HTTPException(400, "新文案不能为空。")
    estimated = round(len(rewritten_text.strip()) / 4.2, 1)
    if not job.person_duration:
        stage = "新文案已确认；上传人物视频后将校准真实时长。"
    elif estimated <= job.person_duration:
        stage = "新文案已确认，预计时长符合人物视频。"
    else:
        stage = "新文案已确认，但预计配音可能超出人物视频时长。"
    store.update(job, rewritten_text=rewritten_text.strip(), stage=stage, current_step=2, script_confirmed=True, preview_confirmed=False)
    return asdict(job)


@app.post("/api/projects/{job_id}/person-video")
async def upload_person_video(
    job_id: str,
    background_tasks: BackgroundTasks,
    video: UploadFile = File(...),
    consent: bool = Form(False),
) -> dict[str, Any]:
    if not consent:
        raise HTTPException(400, "请确认已取得人物视频中人物的肖像使用授权。")
    job = store.get(job_id)
    if not video.filename or not (video.content_type or "").startswith("video/"):
        raise HTTPException(400, "请上传 MP4 或 MOV 人物视频。")
    target = JOBS_DIR / job_id / "person.mp4"
    with target.open("wb") as output:
        while chunk := await video.read(1024 * 1024):
            output.write(chunk)
    if target.stat().st_size > 500 * 1024 * 1024:
        target.unlink(missing_ok=True)
        raise HTTPException(413, "人物视频不能超过 500 MB。")
    store.update(
        job, person_name=video.filename, person_video_name=target.name, person_status="等待检测人物视频",
        voice_id=None, voice_reference_hash=None, preview_audio_name=None, preview_duration=0, preview_confirmed=False,
        output_name=None, edit_output_name=None, error=None,
    )
    background_tasks.add_task(run_person_video, job_id)
    return {"id": job_id, "accepted": True}


@app.post("/api/projects/{job_id}/voice-preview")
async def voice_preview(
    job_id: str,
    background_tasks: BackgroundTasks,
    mode: str = Form("upload"),
    voice_id: str | None = Form(None),
    voice_consent: bool = Form(False),
    speed: str = Form("standard"),
    emotion: str = Form("natural"),
    voice_clone_model: str | None = Form(None),
    direct_tts_model: str | None = Form(None),
    fish_model: str = Form("s2-pro"),
    fish_style: str = Form(""),
    fish_speed: float = Form(1.0),
    fish_volume: float = Form(0.0),
    fish_temperature: float = Form(0.5),
    fish_top_p: float = Form(0.7),
    # HTML checkbox values are omitted when unchecked.  Keep it as an optional
    # form value so users can genuinely turn the quality guard off.
    fish_quality_guard: str | None = Form(None),
    reference_text: str = Form(""),
    sample: UploadFile | None = File(None),
    voice_rate: float = Form(1.0),
    voice_volume: int = Form(50),
    voice_pitch: float = Form(1.0),
    voice_seed: int = Form(0),
    voice_lang: str = Form("auto"),
    qwen_voice_lang: str = Form(""),
    voice_instruction: str = Form(""),
    direct_tts_voice: str = Form(""),
    minimax_emotion: str = Form(""),
    minimax_tts_model: str = Form("speech-2.8-hd"),
    minimax_speed: float = Form(1.0),
    minimax_volume: float = Form(1.0),
    minimax_pitch: int = Form(0),
    minimax_language_boost: str = Form("auto"),
    minimax_text_norm: str | None = Form(None),
    minimax_latex: str | None = Form(None),
    minimax_pronunciation: str = Form(""),
    minimax_sound_effect: str = Form(""),
    mimo_style: str = Form(""),
) -> dict[str, Any]:
    if mode == "original":
        raise HTTPException(400, "「人物视频原声音色」已下线：请上传声音样音、使用已保存音色或系统音色。")
    if mode not in {"upload", "saved", "direct"}:
        raise HTTPException(400, "未知的音色来源。")
    if mode != "direct" and not voice_consent:
        raise HTTPException(400, "请确认已取得所上传或使用的声音样音授权。")
    if speed not in {"slow", "standard", "fast"} or emotion not in {"natural", "warm", "steady"}:
        raise HTTPException(400, "未知的声音控制选项。")
    if fish_model not in {"s2-pro"}:
        raise HTTPException(400, "未知的 Fish Audio 模型。")
    if not 0.5 <= fish_speed <= 2.0:
        raise HTTPException(400, "Fish Audio 语速必须在 0.5 到 2.0 之间。")
    if not -20 <= fish_volume <= 20:
        raise HTTPException(400, "Fish Audio 音量必须在 -20 到 20 dB 之间。")
    if not 0 <= fish_temperature <= 1 or not 0 <= fish_top_p <= 1:
        raise HTTPException(400, "Fish Audio 采样参数必须在 0 到 1 之间。")
    if len(fish_style) > 800 or len(reference_text) > 12000:
        raise HTTPException(400, "声音控制文本或参考音频逐字稿过长。")
    # 阿里云 CosyVoice 精细控制参数校验（与官方 API 取值范围一致）。
    if not 0.5 <= voice_rate <= 2.0:
        raise HTTPException(400, "CosyVoice 语速必须在 0.5 到 2.0 之间。")
    if not 0 <= voice_volume <= 100:
        raise HTTPException(400, "CosyVoice 音量必须在 0 到 100 之间。")
    if not 0.5 <= voice_pitch <= 2.0:
        raise HTTPException(400, "CosyVoice 音高必须在 0.5 到 2.0 之间。")
    if not 0 <= voice_seed <= 65535:
        raise HTTPException(400, "CosyVoice 随机种子必须在 0 到 65535 之间。")
    if voice_lang not in {"auto", "zh", "en", "fr", "de", "ja", "ko", "ru", "pt", "th", "id", "vi"}:
        raise HTTPException(400, "未知的发音语言选项。")
    if qwen_voice_lang.strip():
        if qwen_voice_lang not in {"auto", "zh", "en", "de", "it", "pt", "es", "ja", "ko", "fr", "ru"}:
            raise HTTPException(400, "未知的 Qwen3-TTS 发音语言选项。")
        voice_lang = qwen_voice_lang
    if len(voice_instruction) > 200:
        # 官方 instruction 限 100 字符（汉字按 2 字符计），放宽到 200 字符防止误伤。
        raise HTTPException(400, "CosyVoice 表达指令过长（官方限制约 100 字符）。")
    if len(mimo_style) > 800:
        raise HTTPException(400, "MiMo 表达指令过长（限 800 字符）。")
    job = store.get(job_id)
    custom_sample: Path | None = None
    if mode == "upload":
        # Browsers include an empty UploadFile for an unselected file input in
        # FormData.  When a library sample is already attached, that empty
        # placeholder must not be mistaken for a newly uploaded, invalid file.
        if sample and sample.filename:
            suffix = Path(sample.filename or "sample.wav").suffix.lower()
            if suffix not in LIBRARY_AUDIO_SUFFIXES or not (sample.content_type or "").startswith("audio/"):
                raise HTTPException(400, "声音样本格式不支持，请上传 MP3、WAV、M4A、FLAC、OGG 或 WebM 音频。")
            custom_sample = JOBS_DIR / job_id / f"voice-sample{suffix}"
            upload_target = JOBS_DIR / job_id / f"voice-sample-upload{suffix}"
            size = 0
            with upload_target.open("wb") as output:
                while chunk := await sample.read(1024 * 1024):
                    size += len(chunk)
                    if size > 10 * 1024 * 1024:
                        output.close()
                        upload_target.unlink(missing_ok=True)
                        raise HTTPException(413, "声音样本不能超过 10 MB。")
                    output.write(chunk)
            upload_target.replace(custom_sample)
            store.update(job, voice_sample_name=custom_sample.name, voice_sample_label=None)
        elif job.voice_sample_name:
            custom_sample = JOBS_DIR / job_id / job.voice_sample_name
            if not custom_sample.is_file():
                raise HTTPException(409, "素材库中的声音样本已不存在，请重新选择。")
        else:
            raise HTTPException(400, "请上传新的声音样本，或从素材库选择声音。")
    if mode == "saved" and not (voice_id or "").strip():
        raise HTTPException(400, "请选择已保存的音色 ID。")
    # Keep the legacy MiniMax form field compatible with the shared preview
    # worker.  The Lite build no longer exposes MiniMax controls, but an empty
    # list must still be passed instead of the old undefined local variable.
    minimax_rules = [line.strip() for line in minimax_pronunciation.splitlines() if line.strip()][:50]
    # 只校验当前声音来源真正会用到的模型。此前这里无条件校验两条路由，
    # 导致用户只配置本地 Qwen3-TTS 时，即使选择“直接配音”也会被缺失的
    # 云端声音复刻 Key 拦住，反之亦然。
    if mode == "direct":
        voice_clone_model = normalize_clone_model(voice_clone_model or "")
        direct_tts_model = selected_model("direct_tts", direct_tts_model)
    else:
        voice_clone_model = normalize_clone_model(selected_model("voice_clone", voice_clone_model))
        direct_tts_model = (direct_tts_model or _load_model_routes().get("direct_tts") or "")
    if voice_clone_model in {"qwen3-tts-vc", "qwen3-tts-local-base"} and mode == "upload" and custom_sample:
        try:
            sample_duration = duration_seconds(custom_sample)
        except (OSError, subprocess.SubprocessError, ValueError):
            raise HTTPException(400, "无法读取样音时长，请转换为清晰的 MP3、WAV 或 M4A 后重试。")
        if sample_duration > 60.0:
            custom_sample.unlink(missing_ok=True)
            raise HTTPException(400, "Qwen3-TTS 样音最长 60 秒；请裁剪后再上传。建议 10–20 秒。")
        if sample_duration < 5.0:
            custom_sample.unlink(missing_ok=True)
            raise HTTPException(400, "Qwen3-TTS 样音需至少 5 秒清晰人声；官方建议 10–20 秒。")
    background_tasks.add_task(
        run_voice_preview,
        job_id,
        mode,
        (voice_id or "").strip() or None,
        speed,
        emotion,
        voice_clone_model,
        direct_tts_model,
        fish_model,
        fish_style.strip(),
        fish_speed,
        fish_volume,
        fish_temperature,
        fish_top_p,
        fish_quality_guard is not None,
        reference_text.strip(),
        custom_sample,
        voice_rate,
        voice_volume,
        voice_pitch,
        voice_seed,
        voice_lang,
        voice_instruction.strip(),
        direct_tts_voice.strip(),
        minimax_emotion.strip(),
        minimax_tts_model=minimax_tts_model,
        minimax_speed=minimax_speed,
        minimax_volume=minimax_volume,
        minimax_pitch=minimax_pitch,
        minimax_language_boost=minimax_language_boost,
        minimax_text_normalization=minimax_text_norm is not None,
        minimax_latex_read=minimax_latex is not None,
        minimax_pronunciation=minimax_rules,
        minimax_sound_effect=minimax_sound_effect.strip(),
        mimo_style=mimo_style.strip(),
    )
    return {"id": job_id, "accepted": True}


@app.post("/api/projects/{job_id}/voice-confirm")
def confirm_voice_preview(job_id: str) -> dict[str, Any]:
    job = store.get(job_id)
    if not job.preview_audio_name:
        raise HTTPException(409, "请先生成试听音频。")
    store.update(job, preview_confirmed=True, current_step=4, stage="声音试听已确认，可生成改口型视频")
    return asdict(job)


@app.post("/api/projects/{job_id}/generate-video")
def generate_project_video(
    job_id: str, background_tasks: BackgroundTasks, strategy: str = Form("keep_video"),
    lipsync_model: str | None = Form(None),
) -> dict[str, Any]:
    job = store.get(job_id)
    if job.status == "running":
        raise HTTPException(409, "当前项目仍在处理中。")
    if strategy not in {"keep_video", "trim_tail"}:
        raise HTTPException(400, "未知的时长处理策略。")
    lipsync_model = selected_model("lipsync", lipsync_model)
    if not job.preview_confirmed:
        raise HTTPException(409, "请先确认声音试听。")
    if not job.person_duration:
        raise HTTPException(409, "请先上传人物视频。")
    if job.preview_duration > MAX_VIDEO_SECONDS:
        raise HTTPException(409, f"当前配音超过本版本支持的 {MAX_VIDEO_SECONDS} 秒上限，请重新生成较短配音后再试。")
    tolerance = max(0.8, job.person_duration * 0.05)
    if job.duration_delta > tolerance:
        raise HTTPException(409, "新配音比人物视频长，请重新生成较短配音或准备更长的人物视频。")
    store.update(job, duration_strategy=strategy, lipsync_model=lipsync_model, cancel_requested=False)
    background_tasks.add_task(run_video_generation, job_id)
    billed_duration = job.preview_duration
    estimated_cost = round(billed_duration * 0.08, 2) if lipsync_model == "videoretalk" else 0
    return {"id": job_id, "accepted": True, "lipsync_model": lipsync_model, "estimated_lipsync_cost": estimated_cost}


@app.post("/api/projects/{job_id}/cancel-video")
def cancel_project_video(job_id: str) -> dict[str, Any]:
    job = store.get(job_id)
    if job.current_step != 4 or job.status != "running":
        raise HTTPException(409, "当前没有可取消的改口型任务。")
    store.update(job, cancel_requested=True, stage="处理中…")
    return {"id": job_id, "accepted": True}


def apply_edit_form(
    job: Job,
    trim_start: float | None,
    trim_end: float | None,
    title: str,
    sticker: str,
    subtitle_enabled: bool,
    title_font_size: str,
    title_color: str,
    title_position: str,
    subtitle_style: str,
    subtitle_font_size: int,
    subtitle_color: str,
    subtitle_margin_v: int,
    subtitle_keywords: str,
    subtitle_keyword_color: str,
    cover_text: str,
    cover_style: str,
    music_volume: float,
    broll_enabled: bool,
    broll_start: float | None,
    broll_duration: float | None,
    layout_mode: str,
    layout_background_type: str,
    layout_background_name: str,
    layout_background_color: str,
    layout_foreground_scale: float,
    layout_foreground_position: str,
    layout_foreground_margin: int,
    layout_blur: int,
) -> None:
    """校验并写入剪辑/包装表单参数（edit_project 与 auto-edit 共用）。

    Lite 版不提供额外裁剪入口；保留旧字段仅为兼容旧客户端，并在保存时
    清除旧项目残留的裁剪值，确保导出始终使用完整改口型结果。
    """
    if title_position not in {"top", "center", "bottom"}:
        raise HTTPException(400, "未知的标题位置。")
    if subtitle_style not in {"inline-cyan", "classic-yellow", "highlight-red", "soft-white"}:
        raise HTTPException(400, "未知的字幕模板。")
    if layout_mode not in {"fullscreen", "pip"}:
        raise HTTPException(400, "未知的主体视频布局。")
    if layout_background_type not in {"broll", "image", "color", "blur"}:
        raise HTTPException(400, "未知的画中画背景类型。")
    if layout_foreground_position not in {"top-left", "top-right", "bottom-left", "bottom-right", "center"}:
        raise HTTPException(400, "未知的画中画主体位置。")
    normalized_color = layout_background_color.strip().lstrip("#").upper()
    if not re.fullmatch(r"[0-9A-F]{6}", normalized_color):
        normalized_color = "202020"
    normalized_scale = layout_foreground_scale / 100 if layout_foreground_scale > 1 else layout_foreground_scale
    if cover_style not in {"diagonal-yellow", "giant-headline", "vertical-cutout", "center-sticker", "dark-bold", "yellow-block", "clean-white"}:
        raise HTTPException(400, "未知的封面模板。")
    try:
        title_font_size = int(str(title_font_size).lstrip("h/"))
        title_font_size = f"h/{max(6, min(72, title_font_size))}"
    except ValueError:
        title_font_size = "h/18"
    # B-roll 插片窗口必须落在成片范围内，避免手动误填/AI 决策越界导致插片不显示。
    # 钳制时按当前成片目标时长做上限；这里只做硬上限保护，详细漂移留给 render_edit。
    safe_broll_start = max(0.0, min(600.0, broll_start)) if broll_start is not None else None
    safe_broll_duration = max(0.2, min(60.0, broll_duration)) if broll_duration is not None else None
    updates: dict[str, Any] = dict(
        trim_start=0,
        trim_end=None,
        title=title.strip()[:80], sticker=sticker.strip()[:40],
        subtitle_enabled=subtitle_enabled,
        title_font_size=title_font_size, title_color=title_color, title_position=title_position,
        subtitle_style=subtitle_style,
        subtitle_font_size=max(30, min(96, subtitle_font_size)),
        # ASS MarginV is measured from the nearest vertical edge.  Keeping the
        # range below 900 leaves enough room for large subtitles on 1080p video
        # while allowing the user to move them well above the default bottom.
        subtitle_color=subtitle_color, subtitle_margin_v=max(0, min(900, subtitle_margin_v)),
        subtitle_keywords=subtitle_keywords.strip()[:200],
        subtitle_keyword_color=subtitle_keyword_color,
        cover_text=cover_text.strip()[:40],
        cover_style=cover_style,
        music_volume=max(0.0, min(1.0, music_volume)),
        broll_enabled=broll_enabled,
        layout_mode=layout_mode,
        layout_background_type=layout_background_type,
        layout_background_name=layout_background_name.strip()[:120] or None,
        layout_background_color=normalized_color,
        layout_foreground_scale=max(0.15, min(0.95, normalized_scale)),
        layout_foreground_position=layout_foreground_position,
        layout_foreground_margin=max(0, min(300, layout_foreground_margin)),
        layout_blur=max(2, min(40, layout_blur)),
    )
    if safe_broll_start is not None:
        updates["broll_start"] = safe_broll_start
    if safe_broll_duration is not None:
        updates["broll_duration"] = safe_broll_duration
    store.update(job, **updates)


@app.post("/api/projects/{job_id}/edit")
def edit_project(
    job_id: str,
    background_tasks: BackgroundTasks,
    trim_start: float | None = Form(None),
    trim_end: float | None = Form(None),
    title: str = Form(""),
    sticker: str = Form(""),
    subtitle_enabled: bool = Form(False),
    title_font_size: str = Form("h/18"),
    title_color: str = Form("white"),
    title_position: str = Form("top"),
    subtitle_style: str = Form("inline-cyan"),
    # Keep the generated subtitle readable on the 576x1024 portrait videos.
    # The old default of 18 was silently sent back by the form during export.
    subtitle_font_size: int = Form(42),
    subtitle_color: str = Form("FFFFFF"),
    subtitle_margin_v: int = Form(72),
    subtitle_keywords: str = Form(""),
    subtitle_keyword_color: str = Form("FFFF00"),
    cover_text: str = Form(""),
    cover_style: str = Form("diagonal-yellow"),
    music_volume: float = Form(0.14),
    broll_enabled: str | None = Form(None),
    broll_start: float | None = Form(None),
    broll_duration: float | None = Form(None),
    layout_mode: str = Form("fullscreen"),
    layout_background_type: str = Form("blur"),
    layout_background_name: str = Form(""),
    layout_background_color: str = Form("202020"),
    layout_foreground_scale: float = Form(0.32),
    layout_foreground_position: str = Form("bottom-right"),
    layout_foreground_margin: int = Form(24),
    layout_blur: int = Form(18),
) -> dict[str, Any]:
    job = store.get(job_id)
    apply_edit_form(
        job, trim_start, trim_end, title, sticker, subtitle_enabled, title_font_size,
        title_color, title_position, subtitle_style, subtitle_font_size, subtitle_color, subtitle_margin_v,
        subtitle_keywords, subtitle_keyword_color, cover_text, cover_style, music_volume,
        broll_enabled is not None, broll_start, broll_duration,
        layout_mode, layout_background_type, layout_background_name, layout_background_color,
        layout_foreground_scale, layout_foreground_position, layout_foreground_margin, layout_blur,
    )
    background_tasks.add_task(render_edit, job_id)
    return {"id": job_id, "accepted": True}


@app.post("/api/projects/{job_id}/auto-edit")
def auto_edit_project(
    job_id: str,
    background_tasks: BackgroundTasks,
    trim_start: float = Form(0),
    trim_end: float | None = Form(None),
    title: str = Form(""),
    sticker: str = Form(""),
    subtitle_enabled: bool = Form(False),
    title_font_size: str = Form("h/18"),
    title_color: str = Form("white"),
    title_position: str = Form("top"),
    subtitle_style: str = Form("inline-cyan"),
    subtitle_font_size: int = Form(42),
    subtitle_color: str = Form("FFFFFF"),
    subtitle_margin_v: int = Form(72),
    subtitle_keywords: str = Form(""),
    subtitle_keyword_color: str = Form("FFFF00"),
    cover_text: str = Form(""),
    cover_style: str = Form("diagonal-yellow"),
    music_volume: float = Form(0.14),
    broll_enabled: str | None = Form(None),
    broll_start: float | None = Form(None),
    broll_duration: float | None = Form(None),
    layout_mode: str = Form("fullscreen"),
    layout_background_type: str = Form("blur"),
    layout_background_name: str = Form(""),
    layout_background_color: str = Form("202020"),
    layout_foreground_scale: float = Form(0.32),
    layout_foreground_position: str = Form("bottom-right"),
    layout_foreground_margin: int = Form(24),
    layout_blur: int = Form(18),
    locked: str = Form(""),
) -> dict[str, Any]:
    """AI 一键成片：先同步剪辑表单当前值，再由 AI 补齐未锁定的决策并直接导出成片。"""
    job = store.get(job_id)
    if not job.output_name:
        raise HTTPException(409, "请先生成改口型视频。")
    if job.status == "running":
        raise HTTPException(409, "当前项目仍在处理中。")
    apply_edit_form(
        job, trim_start, trim_end, title, sticker, subtitle_enabled, title_font_size,
        title_color, title_position, subtitle_style, subtitle_font_size, subtitle_color, subtitle_margin_v,
        subtitle_keywords, subtitle_keyword_color, cover_text, cover_style, music_volume,
        broll_enabled is not None, broll_start, broll_duration,
        layout_mode, layout_background_type, layout_background_name, layout_background_color,
        layout_foreground_scale, layout_foreground_position, layout_foreground_margin, layout_blur,
    )
    background_tasks.add_task(run_auto_edit, job_id, locked)
    return {"id": job_id, "accepted": True}


@app.post("/api/projects/{job_id}/music")
async def upload_music(job_id: str, music: UploadFile = File(...)) -> dict[str, Any]:
    job = store.get(job_id)
    if not (music.content_type or "").startswith("audio/"):
        raise HTTPException(400, "请上传音频文件。")
    suffix = Path(music.filename or "music.mp3").suffix.lower() or ".mp3"
    target = JOBS_DIR / job_id / f"music{suffix}"
    with target.open("wb") as output:
        while chunk := await music.read(1024 * 1024):
            output.write(chunk)
    if target.stat().st_size > 100 * 1024 * 1024:
        target.unlink(missing_ok=True)
        raise HTTPException(413, "背景音乐不能超过 100 MB。")
    store.update(job, music_name=target.name)
    return asdict(job)


@app.post("/api/projects/{job_id}/music/remove")
def remove_music(job_id: str) -> dict[str, Any]:
    """移除项目当前挂载的背景音乐：清记录 + 删文件。

    之前只上传不清理：测试上传过的音频会一直挂在记录上，UI 又没有
    任何显示/移除入口，用户以为没加音乐，导出时却被混进去。
    """
    job = store.get(job_id)
    if job.music_name:
        (JOBS_DIR / job_id / job.music_name).unlink(missing_ok=True)
        store.update(job, music_name=None)
    return asdict(job)


@app.post("/api/projects/{job_id}/broll")
async def upload_broll(job_id: str, broll: UploadFile = File(...)) -> dict[str, Any]:
    """Store a user-owned local B-roll clip (video or image) for the final edit.

    每次上传追加为新的一段（broll_1/broll_2/... 递增命名，不覆盖已有素材），
    默认插入点 5s、时长 4s，可在剪辑面板逐段调整。
    """
    job = store.get(job_id)
    content_type = (broll.content_type or "").lower()
    is_image = content_type.startswith("image/") or is_image_path(Path(broll.filename or ""))
    if not (content_type.startswith("video/") or is_image):
        raise HTTPException(400, "B-roll 素材请上传视频或图片文件。")
    suffix = Path(broll.filename or "broll.mp4").suffix.lower() or (".png" if is_image else ".mp4")
    work = JOBS_DIR / job_id
    work.mkdir(parents=True, exist_ok=True)
    # 递增命名：broll_1.png、broll_2.mp4 ... 避免覆盖已上传素材。
    existing = {c.name for c in job.broll_clips}
    index = 1
    while f"broll_{index}{suffix}" in existing:
        index += 1
    target = work / f"broll_{index}{suffix}"
    with target.open("wb") as output:
        while chunk := await broll.read(1024 * 1024):
            output.write(chunk)
    if target.stat().st_size > 500 * 1024 * 1024:
        safe_unlink(target)
        raise HTTPException(413, "B-roll 素材不能超过 500 MB。")
    # 静态图片 ffprobe 时长恒为 0，跳过时长校验；视频必须可正常播放。
    clip_duration = 4.0
    if not is_image:
        try:
            clip_duration = duration_seconds(target)
            if clip_duration < 0.2:
                raise RuntimeError("素材时长过短")
            clip_duration = min(60.0, clip_duration)
        except Exception as error:
            safe_unlink(target)
            raise HTTPException(400, "B-roll 素材无法读取，请上传可播放的视频文件。") from error
    clips = list(job.broll_clips)
    clips.append(BrollClip(name=target.name, start=5.0, duration=clip_duration, enabled=True, title=Path(broll.filename or target.name).stem))
    store.update(job, broll_clips=clips, broll_name=target.name, broll_enabled=True,
                 broll_start=5.0, broll_duration=clip_duration)
    return asdict(job)


@app.delete("/api/projects/{job_id}/broll/{index}")
def delete_broll_clip(job_id: str, index: int) -> dict[str, Any]:
    """删除第 index 段 B-roll（0 起），同时清理对应素材文件。"""
    job = store.get(job_id)
    clips = list(job.broll_clips)
    if index < 0 or index >= len(clips):
        raise HTTPException(404, "B-roll 片段不存在。")
    removed = clips.pop(index)
    work = JOBS_DIR / job_id
    candidate = work / removed.name
    # 仅当没有其他段引用同一文件时才删除磁盘文件。
    if not any(c.name == removed.name for c in clips):
        safe_unlink(candidate)
    if not clips:
        store.update(job, broll_clips=clips, broll_name=None, broll_enabled=False)
    else:
        store.update(job, broll_clips=clips)
    return asdict(job)


@app.post("/api/projects/{job_id}/broll-clips")
def update_broll_clips(job_id: str, clips: str = Form(...)) -> dict[str, Any]:
    """批量更新各段 B-roll 的插入点/时长/开关（JSON 数组：[{name,start,duration,enabled}]）。"""
    job = store.get(job_id)
    try:
        raw_clips = json.loads(clips)
        if not isinstance(raw_clips, list):
            raise ValueError("必须是数组")
    except ValueError as error:
        raise HTTPException(400, "B-roll 片段数据格式错误。") from error
    existing = {c.name for c in job.broll_clips}
    merged: list[BrollClip] = []
    for item in raw_clips:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "")
        if name not in existing:
            continue  # 只允许更新已上传的素材，防止伪造文件名
        merged.append(BrollClip(
            name=name,
            start=max(0.0, min(600.0, float(item.get("start") or 0))),
            duration=max(0.2, min(60.0, float(item.get("duration") or 4))),
            enabled=bool(item.get("enabled", True)),
            title=str(item.get("title") or "")[:80],
            mode=(str(item.get("mode") or "replace") if str(item.get("mode") or "replace") in {"replace", "pip"} else "replace"),
            pip_position=(
                str(item.get("pip_position") or "bottom-right")
                if str(item.get("pip_position") or "bottom-right") in {
                    "top-left", "top-right", "bottom-left", "bottom-right", "center",
                } else "bottom-right"
            ),
            pip_scale=max(0.1, min(0.8, float(item.get("pip_scale") or 0.32))),
            pip_margin=max(0, min(300, int(item.get("pip_margin") or 24))),
        ))
    if not merged:
        store.update(job, broll_clips=[], broll_name=None, broll_enabled=False)
    else:
        store.update(job, broll_clips=merged, broll_name=merged[0].name, broll_enabled=True,
                     broll_start=merged[0].start, broll_duration=merged[0].duration)
    return asdict(job)


@app.post("/api/projects/{job_id}/cover")
async def upload_cover(job_id: str, cover: UploadFile = File(...)) -> dict[str, Any]:
    job = store.get(job_id)
    if not (cover.content_type or "").startswith("image/"):
        raise HTTPException(400, "请上传图片封面。")
    suffix = Path(cover.filename or "cover.jpg").suffix.lower() or ".jpg"
    target = JOBS_DIR / job_id / f"cover{suffix}"
    with target.open("wb") as output:
        while chunk := await cover.read(1024 * 1024):
            output.write(chunk)
    store.update(job, cover_name=target.name)
    return asdict(job)


@app.get("/api/projects/{job_id}/media/{name}")
def project_media(job_id: str, name: str) -> FileResponse:
    allowed = {"source.mp4", "source.wav", "person.mp4", "person.wav", "person-trimmed.mp4", "person-freeze_tail.mp4", "person-loop_video.mp4", "preview.wav", "preview-trimmed.wav", "result.mp4", "final.mp4", "cover.jpg", "source.srt"}
    job = store.get(job_id)
    allowed.update(value for value in (job.cover_name, job.music_name, job.broll_name) if value)
    allowed.update(clip.name for clip in job.broll_clips if clip.name)
    if name not in allowed:
        raise HTTPException(404, "文件不存在")
    path = JOBS_DIR / job_id / name
    if not path.exists():
        raise HTTPException(404, "文件尚未生成")
    media = {".mp4": "video/mp4", ".wav": "audio/wav", ".jpg": "image/jpeg", ".png": "image/png", ".webp": "image/webp", ".gif": "image/gif", ".srt": "text/plain"}.get(path.suffix, "application/octet-stream")
    # 视频导出后可能被覆盖重出，必须禁用浏览器缓存，否则播放器会一直放旧文件。
    return FileResponse(path, media_type=media, filename=path.name, headers={"Cache-Control": "no-store"})


@app.get("/api/projects/{job_id}/download")
def project_download(job_id: str) -> FileResponse:
    job = store.get(job_id)
    filename = job.edit_output_name or job.output_name
    if not filename:
        raise HTTPException(409, "成片尚未生成")
    path = JOBS_DIR / job_id / filename
    if not path.exists():
        raise HTTPException(404, "成片文件不存在")
    return FileResponse(path, media_type="video/mp4", filename=f"talking-video-{job.id}.mp4")


@app.get("/api/projects/{job_id}/download/{artifact}")
def project_intermediate_download(job_id: str, artifact: str) -> FileResponse:
    """Download a generated intermediate artifact without exposing arbitrary files."""
    job = store.get(job_id)
    artifacts = {
        "voice-preview": (job.preview_audio_name, "audio/wav", f"talking-video-{job.id}-voice-preview.wav"),
        "lipsync-video": (job.output_name, "video/mp4", f"talking-video-{job.id}-lipsync.mp4"),
    }
    if artifact not in artifacts:
        raise HTTPException(404, "未知的中间产物。")
    filename, media_type, download_name = artifacts[artifact]
    if not filename:
        raise HTTPException(409, "该中间产物尚未生成。")
    work = (JOBS_DIR / job.id).resolve()
    path = (work / filename).resolve()
    if path.parent != work:
        raise HTTPException(400, "文件路径无效。")
    if not path.exists():
        raise HTTPException(404, "中间产物文件不存在。")
    return FileResponse(path, media_type=media_type, filename=download_name)


def _project_artifact_path(job: Job, artifact: str) -> Path:
    """Resolve a generated local artifact without allowing path traversal."""
    artifacts = {
        "voice-preview": job.preview_audio_name,
        "lipsync-video": job.output_name,
        "final": job.edit_output_name or job.output_name,
    }
    filename = artifacts.get(artifact)
    if not filename:
        raise HTTPException(409, "该文件尚未生成。")
    work = (JOBS_DIR / job.id).resolve()
    path = (work / filename).resolve()
    if path.parent != work:
        raise HTTPException(400, "文件路径无效。")
    if not path.exists():
        raise HTTPException(404, "文件不存在。")
    return path


@app.post("/api/projects/{job_id}/open-location/{artifact}")
def open_project_artifact_location(job_id: str, artifact: str) -> dict[str, Any]:
    """Open Windows Explorer and select a generated local artifact."""
    if os.name != "nt":
        raise HTTPException(501, "打开文件位置目前仅支持 Windows 桌面端。")
    path = _project_artifact_path(store.get(job_id), artifact)
    try:
        subprocess.Popen(["explorer.exe", "/select,", str(path)])
    except OSError as exc:
        raise HTTPException(500, f"无法打开文件位置：{exc}") from exc
    return {"ok": True, "artifact": artifact}
