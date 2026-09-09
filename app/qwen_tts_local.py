"""Client adapter for a local/remote Qwen3-TTS FastAPI service."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx


def _optional_timeout(raw) -> float | None:
    """0、留空或非法值 = 不限制；本地模型推理耗时不稳定，默认不设上限。"""
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        value = float(text)
    except ValueError:
        return None
    return value if value > 0 else None


LANGUAGES = {
    "auto": "Auto",
    "zh": "Chinese",
    "en": "English",
    "de": "German",
    "it": "Italian",
    "pt": "Portuguese",
    "es": "Spanish",
    "ja": "Japanese",
    "ko": "Korean",
    "fr": "French",
    "ru": "Russian",
}

CUSTOM_VOICES = {
    "Vivian": "Vivian（中文女声）",
    "Serena": "Serena（中文女声）",
    "Uncle_Fu": "Uncle_Fu（中文男声）",
    "Dylan": "Dylan（北京男声）",
    "Eric": "Eric（四川男声）",
    "Ryan": "Ryan（英文男声）",
    "Aiden": "Aiden（美式男声）",
    "Ono_Anna": "Ono_Anna（日文女声）",
    "Sohee": "Sohee（韩文女声）",
}


class LocalQwenTTSError(RuntimeError):
    """A user-facing local Qwen3-TTS error."""


class LocalQwenTTS:
    def __init__(self, settings: dict[str, str] | None = None) -> None:
        self.settings = settings or {}

    @property
    def base_url(self) -> str:
        return str(self.settings.get("QWEN_TTS_BASE_URL") or "").strip().rstrip("/")

    def _language(self, value: str) -> str:
        return LANGUAGES.get((value or "auto").strip().lower(), "Auto")

    def health(self) -> dict[str, Any]:
        if not self.base_url:
            return {"ok": False, "configured": False, "error": "未配置 QWEN_TTS_BASE_URL"}
        try:
            response = httpx.get(f"{self.base_url}/health", timeout=10, trust_env=False)
            body = response.json()
        except (httpx.HTTPError, OSError, ValueError) as error:
            return {"ok": False, "configured": True, "error": str(error)}
        if not isinstance(body, dict):
            return {"ok": False, "configured": True, "error": "服务返回格式异常"}
        return {"ok": response.is_success and bool(body.get("ok")), "configured": True, **body}

    @staticmethod
    def _raise(response: httpx.Response, prefix: str) -> None:
        try:
            body = response.json()
        except ValueError:
            body = {}
        message = body.get("detail") or body.get("error") or f"HTTP {response.status_code}"
        raise LocalQwenTTSError(f"{prefix}：{message}")

    def _write_response(self, response: httpx.Response, target: Path, prefix: str) -> Path:
        if not response.is_success:
            self._raise(response, prefix)
        content_type = (response.headers.get("content-type") or "").lower()
        if "audio" not in content_type and not response.content.startswith(b"RIFF"):
            raise LocalQwenTTSError(f"{prefix}：服务没有返回 WAV 音频。")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(response.content)
        if not target.is_file() or target.stat().st_size == 0:
            raise LocalQwenTTSError(f"{prefix}：返回了空音频。")
        return target

    def synthesize_custom_voice(
        self,
        text: str,
        target: Path,
        *,
        voice: str = "Vivian",
        language: str = "auto",
        instruction: str = "",
    ) -> Path:
        if not self.base_url:
            raise LocalQwenTTSError("未配置本地 Qwen3-TTS 服务，请在设置中填写 QWEN_TTS_BASE_URL。")
        if voice not in CUSTOM_VOICES:
            raise LocalQwenTTSError(f"未知的 Qwen3-TTS 内置音色：{voice}")
        try:
            response = httpx.post(
                f"{self.base_url}/v1/tts",
                data={"text": text, "speaker": voice, "language": self._language(language), "instruct": instruction},
                timeout=_optional_timeout(self.settings.get("QWEN_TTS_TIMEOUT")),
                trust_env=False,
            )
        except (httpx.HTTPError, OSError) as error:
            raise LocalQwenTTSError(f"本地 Qwen3-TTS 连接失败：{error}") from error
        return self._write_response(response, target, "Qwen3-TTS 内置音色生成失败")

    def synthesize_clone(
        self,
        text: str,
        sample: Path,
        target: Path,
        *,
        reference_text: str = "",
        language: str = "auto",
    ) -> Path:
        if not self.base_url:
            raise LocalQwenTTSError("未配置本地 Qwen3-TTS 服务，请在设置中填写 QWEN_TTS_BASE_URL。")
        if not sample.is_file():
            raise LocalQwenTTSError("声音样音不存在或无法读取。")
        try:
            with sample.open("rb") as stream:
                response = httpx.post(
                    f"{self.base_url}/v1/voice-clone",
                    data={"text": text, "language": self._language(language), "ref_text": reference_text},
                    files={"audio": (sample.name, stream, "audio/wav")},
                    timeout=_optional_timeout(self.settings.get("QWEN_TTS_TIMEOUT")),
                    trust_env=False,
                )
        except (httpx.HTTPError, OSError) as error:
            raise LocalQwenTTSError(f"本地 Qwen3-TTS 连接失败：{error}") from error
        return self._write_response(response, target, "Qwen3-TTS 声音复刻失败")

