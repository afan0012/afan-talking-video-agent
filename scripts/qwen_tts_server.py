"""FastAPI adapter for local Qwen3-TTS models.

Run this script inside the isolated Qwen3-TTS environment on a GPU machine.
The controller only sends text/sample audio and receives a WAV, so the large
torch/model dependencies do not enter the desktop application environment.
"""

from __future__ import annotations

import io
import os
import threading
import tempfile
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import Response


ROOT = Path(os.getenv("QWEN_TTS_MODEL_ROOT", "/root/models")).expanduser().resolve()
CUSTOM_MODEL = Path(os.getenv("QWEN_TTS_CUSTOM_MODEL", str(ROOT / "Qwen3-TTS-12Hz-1.7B-CustomVoice"))).expanduser()
BASE_MODEL = Path(os.getenv("QWEN_TTS_BASE_MODEL", str(ROOT / "Qwen3-TTS-12Hz-1.7B-Base"))).expanduser()
DEVICE = os.getenv("QWEN_TTS_DEVICE", "cuda:0")
DTYPE = os.getenv("QWEN_TTS_DTYPE", "bfloat16").lower()
MAX_SAMPLE_BYTES = int(os.getenv("QWEN_TTS_MAX_SAMPLE_BYTES", str(10 * 1024 * 1024)))

app = FastAPI(title="Qwen3-TTS local adapter", version="1.0")
_models: dict[str, Any] = {}
_model_lock = threading.Lock()


def _model_status(path: Path) -> dict[str, Any]:
    return {"path": str(path), "available": path.is_dir() and (path / "config.json").is_file()}


def _dtype():
    import torch

    return {
        "bfloat16": torch.bfloat16,
        "bf16": torch.bfloat16,
        "float16": torch.float16,
        "fp16": torch.float16,
        "float32": torch.float32,
        "fp32": torch.float32,
    }.get(DTYPE, torch.bfloat16)


def _load(kind: str):
    with _model_lock:
        if kind in _models:
            return _models[kind]
        try:
            from qwen_tts import Qwen3TTSModel
        except Exception as error:  # pragma: no cover - depends on external runtime
            raise RuntimeError(f"未安装 qwen-tts：{error}") from error
        path = CUSTOM_MODEL if kind == "custom" else BASE_MODEL
        if not path.is_dir():
            raise RuntimeError(f"模型目录不存在：{path}")
        kwargs = {"device_map": DEVICE, "dtype": _dtype()}
        try:
            model = Qwen3TTSModel.from_pretrained(path, attn_implementation="flash_attention_2", **kwargs)
        except Exception:
            # FlashAttention 不可用时仍允许使用 PyTorch 原生注意力。
            model = Qwen3TTSModel.from_pretrained(path, attn_implementation="eager", **kwargs)
        _models[kind] = model
        return model


def _wav_response(wavs: list[Any], sample_rate: int) -> Response:
    try:
        import soundfile as sf

        output = io.BytesIO()
        sf.write(output, wavs[0], sample_rate, format="WAV", subtype="PCM_16")
        return Response(output.getvalue(), media_type="audio/wav")
    except Exception as error:  # pragma: no cover - depends on external runtime
        raise HTTPException(500, f"WAV 编码失败：{error}") from error


@app.get("/health")
def health() -> dict[str, Any]:
    return {
        "ok": True,
        "service": "Qwen3-TTS",
        "device": DEVICE,
        "dtype": DTYPE,
        "custom_voice": _model_status(CUSTOM_MODEL),
        "voice_clone": _model_status(BASE_MODEL),
        "loaded": sorted(_models),
    }


@app.post("/v1/tts")
def custom_voice_tts(
    text: str = Form(...),
    speaker: str = Form("Vivian"),
    language: str = Form("Auto"),
    instruct: str = Form(""),
) -> Response:
    if not text.strip():
        raise HTTPException(400, "text 不能为空")
    try:
        model = _load("custom")
        supported = {str(item).lower() for item in model.get_supported_speakers()}
        if speaker.lower() not in supported:
            raise ValueError(f"不支持的内置音色：{speaker}")
        wavs, sample_rate = model.generate_custom_voice(
            text=text,
            language=language or "Auto",
            speaker=speaker,
            instruct=instruct.strip() or None,
        )
        return _wav_response(wavs, sample_rate)
    except HTTPException:
        raise
    except Exception as error:  # pragma: no cover - depends on external runtime
        raise HTTPException(500, f"Qwen3-TTS 内置音色生成失败：{error}") from error


@app.post("/v1/voice-clone")
def voice_clone(
    text: str = Form(...),
    language: str = Form("Auto"),
    ref_text: str = Form(""),
    audio: UploadFile = File(...),
) -> Response:
    if not text.strip():
        raise HTTPException(400, "text 不能为空")
    raw = audio.file.read(MAX_SAMPLE_BYTES + 1)
    if len(raw) > MAX_SAMPLE_BYTES:
        raise HTTPException(413, "参考音频不能超过 10 MB")
    if not raw:
        raise HTTPException(400, "参考音频为空")
    try:
        model = _load("base")
        # 官方示例以本地文件路径传入参考音频。当前 qwen-tts 版本对
        # (waveform, sample_rate) 元组的内部处理存在兼容性问题，因此先写入
        # 临时 WAV，再传路径；这也让 mp3/m4a 等上传格式由模型的音频解析器统一处理。
        with tempfile.NamedTemporaryFile(prefix="qwen3-clone-", suffix=".audio", delete=False) as temp:
            temp.write(raw)
            sample_path = temp.name
        try:
            x_vector_only = not ref_text.strip()
            wavs, output_rate = model.generate_voice_clone(
                text=text,
                language=language or "Auto",
                ref_audio=sample_path,
                ref_text=ref_text.strip() or None,
                x_vector_only_mode=x_vector_only,
            )
            return _wav_response(wavs, output_rate)
        finally:
            try:
                os.unlink(sample_path)
            except OSError:
                pass
    except HTTPException:
        raise
    except Exception as error:  # pragma: no cover - depends on external runtime
        raise HTTPException(500, f"Qwen3-TTS 声音复刻失败：{error}") from error


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=os.getenv("QWEN_TTS_HOST", "127.0.0.1"), port=int(os.getenv("QWEN_TTS_PORT", "8020")))
