"""FastAPI adapter for local FunASR models.

Run this script inside the engine package's own Python runtime (for example
``afan-voice-engines/runtime-py312-cu128``).  The desktop application talks to
it over loopback HTTP only, so funasr/torch never enter the main program's
environment and the application stays light.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel


ROOT = Path(os.getenv("FUNASR_ENGINE_ROOT", "engines/funasr")).expanduser().resolve()
DEVICE = os.getenv("FUNASR_DEVICE", "cuda:0")

# 布局与 app.funasr_local.FUNASR_ENGINE_LAYOUT 保持一致；本文件刻意独立成篇，
# 不 import 应用包，确保任何带 funasr 的解释器都能直接运行它。
LAYOUT: dict[str, dict[str, str | None]] = {
    "local:funasr-sensevoice": {"model": "SenseVoiceSmall", "vad": "fsmn-vad", "punc": None},
    "local:funasr-paraformer": {"model": "paraformer-zh", "vad": "fsmn-vad", "punc": "ct-punc"},
}

app = FastAPI(title="FunASR local adapter", version="1.0")
_models: dict[str, Any] = {}
_model_lock = threading.Lock()


def _has_config(path: Path) -> bool:
    return path.is_dir() and any((path / name).is_file() for name in ("config.json", "configuration.json"))


def _layout_installed(model_id: str) -> bool:
    layout = LAYOUT[model_id]
    parts = [str(layout["model"]), str(layout["vad"])]
    if layout.get("punc"):
        parts.append(str(layout["punc"]))
    return all(_has_config(ROOT / name) for name in parts)


def _load(model_id: str) -> Any:
    with _model_lock:
        if model_id in _models:
            return _models[model_id]
        try:
            from funasr import AutoModel
        except Exception as error:  # pragma: no cover - depends on external runtime
            raise RuntimeError(f"当前运行时未安装 funasr：{error}") from error
        layout = LAYOUT[model_id]
        kwargs: dict[str, Any] = {
            "model": str(ROOT / str(layout["model"])),
            "vad_model": str(ROOT / str(layout["vad"])),
            "vad_kwargs": {"max_single_segment_time": 30000},
            "device": DEVICE,
            "disable_update": True,
        }
        if layout.get("punc"):
            kwargs["punc_model"] = str(ROOT / str(layout["punc"]))
        try:
            _models[model_id] = AutoModel(**kwargs)
        except Exception as error:
            raise RuntimeError(f"本地 FunASR 模型加载失败：{error}") from error
        return _models[model_id]


def _extract_text(result: Any) -> str:
    import json
    import re

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
        # SenseVoice 的原始输出夹带 <|zh|><|NEUTRAL|> 等内部标记，去掉再返回。
        return "\n".join(re.sub(r"<\|[^|>]*\|>", "", line).strip() for line in sentences)
    return str(result if isinstance(result, str) else json.dumps(result, ensure_ascii=False))


def _walk_timeline(value: Any) -> list[dict[str, Any]]:
    timeline: list[dict[str, Any]] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if isinstance(node.get("text"), str) and node.get("text", "").strip():
                start = node.get("start")
                end = node.get("end")
                if isinstance(start, (int, float)) and isinstance(end, (int, float)):
                    # 官方接口中 1000 恰为毫秒分界：>=1000 视为毫秒，否则按秒。
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


class TranscribeRequest(BaseModel):
    audio: str
    model: str = "local:funasr-sensevoice"
    timeline: bool = False


@app.get("/health")
def health() -> dict[str, Any]:
    return {
        "ok": True,
        "service": "FunASR",
        "device": DEVICE,
        "engine_root": str(ROOT),
        "models": [
            {"id": model_id, "installed": _layout_installed(model_id)}
            for model_id in LAYOUT
        ],
        "loaded": sorted(_models),
    }


@app.post("/v1/transcribe")
def transcribe(payload: TranscribeRequest) -> dict[str, Any]:
    audio = Path(payload.audio).expanduser()
    if not audio.is_file():
        raise HTTPException(400, f"音频文件不存在：{audio}")
    if payload.model not in LAYOUT:
        raise HTTPException(400, f"不支持的模型：{payload.model}")
    if not _layout_installed(payload.model):
        raise HTTPException(409, f"本地模型尚未安装完整：{payload.model}")
    try:
        result = _load(payload.model).generate(input=str(audio))
    except HTTPException:
        raise
    except Exception as error:  # pragma: no cover - depends on external runtime
        raise HTTPException(500, f"本地转写失败：{error}") from error
    text = _extract_text(result)
    timeline = _walk_timeline(result) if payload.timeline else []
    return {"text": text, "timeline": timeline}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=os.getenv("FUNASR_HOST", "127.0.0.1"), port=int(os.getenv("FUNASR_PORT", "8030")))
