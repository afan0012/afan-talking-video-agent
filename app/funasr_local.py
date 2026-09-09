"""Local FunASR integration that keeps the desktop application light.

The application never imports funasr or torch itself.  Transcription happens
in :mod:`scripts.funasr_server`, a loopback-only FastAPI adapter started with
the engine package's own Python runtime (for example
``afan-voice-engines/runtime-py312-cu128``).  This module only discovers that
runtime, reports readiness, and forwards transcription requests over HTTP.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from typing import Any, Mapping

import httpx

from app.storage import read_json


FUNASR_ENGINE_LAYOUT: dict[str, dict[str, str]] = {
    "local:funasr-paraformer": {
        "model": "paraformer-zh",
        "vad": "fsmn-vad",
        "punc": "ct-punc",
    },
    "local:funasr-sensevoice": {
        "model": "SenseVoiceSmall",
        "vad": "fsmn-vad",
    },
}

FUNASR_MODEL_SPECS: dict[str, dict[str, str]] = {
    "local:funasr-paraformer": {"label": "本地 FunASR Paraformer"},
    "local:funasr-sensevoice": {"label": "本地 FunASR SenseVoice"},
}

DEFAULT_BASE_URL = "http://127.0.0.1:8030"
_PROBE_TTL_SECONDS = 120
_PROBE_CACHE: dict[str, tuple[float, bool]] = {}


def _has_config(path: Path) -> bool:
    return any((path / name).is_file() for name in ("config.json", "configuration.json"))


def _safe_device(value: str | None) -> str:
    candidate = (value or "cpu").strip().lower()
    if candidate == "cpu" or candidate == "mps":
        return candidate
    if candidate == "cuda" or (candidate.startswith("cuda:") and candidate[5:].isdigit()):
        return candidate
    return "cpu"


def load_config(config_path: Path, settings: Mapping[str, str] | None = None) -> dict[str, str]:
    settings = settings or {}
    config = read_json(config_path, default={}) if config_path.is_file() else {}
    if not isinstance(config, dict):
        config = {}
    return {
        "device": _safe_device(settings.get("FUNASR_DEVICE") or config.get("device") or "cpu"),
        "model_path": str(settings.get("FUNASR_ENGINE_ROOT") or config.get("model_path") or "").strip(),
    }


def model_spec(
    model_id: str,
    config_path: Path,
    settings: Mapping[str, str] | None = None,
    *,
    engine_root: Path | None = None,
) -> dict[str, str | None]:
    config = load_config(config_path, settings)
    configured = str(config.get("model_path") or "").strip()
    root = Path(configured).expanduser() if configured else (engine_root or Path("engines/funasr"))
    layout = FUNASR_ENGINE_LAYOUT[model_id]
    return {
        "model": str(root / layout["model"]),
        "vad_model": str(root / layout["vad"]),
        "punc_model": str(root / layout["punc"]) if "punc" in layout else None,
        "device": config["device"],
    }


def service_alive(url: str, *, timeout: float = 2.0) -> bool:
    if not url:
        return False
    try:
        response = httpx.get(url.rstrip("/") + "/health", timeout=timeout, trust_env=False)
        return 200 <= response.status_code < 300
    except Exception:
        return False


def discover_runtime_python(engine_root: Path | None, settings: Mapping[str, str] | None = None) -> str:
    """Locate a Python interpreter that belongs to the engine package.

    Priority: the interpreter saved by「一键接入」(FUNASR_PYTHON) → sibling
    runtimes of the bundle (``afan-voice-engines/runtime-py312-cu128`` …) →
    a private venv inside the engine directory.  The application's own
    interpreter is deliberately not considered: importing funasr there would
    drag torch into the desktop program.
    """
    settings = settings or {}
    candidates: list[Path] = []
    configured = str(settings.get("FUNASR_PYTHON") or os.getenv("FUNASR_PYTHON", "")).strip()
    if configured:
        candidates.append(Path(configured).expanduser())
    if engine_root:
        root = Path(engine_root).expanduser().resolve()
        bundle = root.parent.parent
        for relative in (
            "runtime-py312-cu128/Scripts/python.exe",
            "runtime-py312/python.exe",
        ):
            candidates.append(bundle / relative)
        for relative in (".venv/Scripts/python.exe", "venv/Scripts/python.exe"):
            candidates.append(root / relative)
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return ""


def _probe_python(executable: str) -> bool:
    if not executable:
        return False
    now = time.monotonic()
    cached = _PROBE_CACHE.get(executable)
    if cached and now - cached[0] < _PROBE_TTL_SECONDS:
        return cached[1]
    try:
        result = subprocess.run(
            [executable, "-c", "import funasr, torch"],
            capture_output=True,
            timeout=120,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        ok = result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        ok = False
    _PROBE_CACHE[executable] = (now, ok)
    return ok


def funasr_available(
    engine_root: Path | None = None,
    settings: Mapping[str, str] | None = None,
) -> bool:
    """Whether transcription can run: a live adapter service or a package runtime."""
    settings = settings or {}
    url = str(settings.get("FUNASR_BASE_URL") or os.getenv("FUNASR_BASE_URL", "")).strip()
    if url and service_alive(url):
        return True
    return _probe_python(discover_runtime_python(engine_root, settings))


def walk_timeline(value: Any) -> list[dict[str, Any]]:
    timeline: list[dict[str, Any]] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if isinstance(node.get("text"), str) and node.get("text", "").strip():
                start = node.get("start")
                end = node.get("end")
                if isinstance(start, (int, float)) and isinstance(end, (int, float)):
                    # FunASR 的时间戳 1000 恰为毫秒分界：达到 1000 视为毫秒。
                    if start >= 1000 or end >= 1000:
                        start, end = start / 1000.0, end / 1000.0
                    timeline.append({"start": float(start), "end": float(end), "text": node["text"].strip()})
            for item in node.values():
                walk(item)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(value)
    return sorted(timeline, key=lambda item: item["start"])


def extract_text(result: Any) -> str:
    def walk(value: Any) -> list[str]:
        found: list[str] = []
        if isinstance(value, dict):
            if isinstance(value.get("text"), str) and value.get("text").strip():
                found.append(value["text"].strip())
            for item in value.values():
                found.extend(walk(item))
        elif isinstance(value, list):
            for item in value:
                found.extend(walk(item))
        return found

    sentences = walk(result)
    if sentences:
        return "\n".join(sentences)
    return str(result if isinstance(result, str) else "")


class FunASRAdapter:
    """Application-facing adapter that delegates to the loopback FunASR service."""

    def __init__(
        self,
        config_path: Path,
        settings: Mapping[str, str] | None = None,
        *,
        engine_root: Path | None = None,
        service_url: str | None = None,
    ) -> None:
        self.config_path = config_path
        self.settings = settings or {}
        self.engine_root = engine_root
        self._service_url = (service_url or "").strip().rstrip("/")

    def available(self) -> bool:
        return funasr_available(self.engine_root, self.settings)

    def _url(self) -> str:
        url = self._service_url or str(self.settings.get("FUNASR_BASE_URL") or "").strip().rstrip("/")
        if url and service_alive(url):
            return url
        raise RuntimeError("本地 FunASR 服务未运行；请在「设置 → 本地 AI 引擎」重新检测后再试。")

    def _post(self, audio: Path, model_id: str, timeline: bool) -> dict[str, Any]:
        try:
            response = httpx.post(
                self._url() + "/v1/transcribe",
                json={"audio": str(Path(audio).expanduser().resolve()), "model": model_id, "timeline": timeline},
                timeout=900,
                trust_env=False,
            )
            response.raise_for_status()
            return response.json()
        except httpx.HTTPError as error:
            raise RuntimeError(f"本地 FunASR 转写失败：{error}") from error

    def transcribe(self, audio: Path, model_id: str) -> str:
        return str(self._post(audio, model_id, False).get("text") or "")

    def transcribe_timeline(self, audio: Path, model_id: str) -> tuple[str, list[dict[str, Any]]]:
        payload = self._post(audio, model_id, True)
        return str(payload.get("text") or ""), list(payload.get("timeline") or [])
