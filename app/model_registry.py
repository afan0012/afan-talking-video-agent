"""Central catalogue and pure helpers for model capabilities.

This module deliberately contains no FastAPI state, filesystem access, or
network calls.  Keeping the capability catalogue in one place means adding a
model does not require editing the request handlers and the media pipeline at
the same time.
"""

from __future__ import annotations

from app.qwen_tts_local import CUSTOM_VOICES as LOCAL_QWEN_VOICES


ASR_MODELS = {
    "auto": "自动选择",
    "paraformer-realtime-v2": "百炼 Paraformer 实时",
    "mimo-v2.5-asr": "MiMo ASR v2.5",
    "qwen-audio-3.0-asr-flash-filetrans": "百炼 Qwen Audio 3.0",
    "local:funasr-paraformer": "本地 FunASR Paraformer",
    "local:funasr-sensevoice": "本地 FunASR SenseVoice",
}

REWRITE_MODELS = {
    "auto": "自动选择",
    "mimo-v2.5": "MiMo v2.5",
    "mimo-v2.5-pro": "MiMo v2.5 Pro",
    "qwen3.7-flash": "百炼 Qwen3.7 Flash",
}

VOICE_CLONE_MODELS = {
    "mimo-v2.5-tts-voiceclone": "MiMo v2.5 声音复刻",
    "cosyvoice-v3.5-plus": "阿里云百炼 CosyVoice",
    "qwen-voice": "阿里云百炼 CosyVoice",
    "qwen3-tts-vc": "百炼 Qwen3-TTS 复刻",
    "qwen3-tts-local-base": "Qwen3-TTS Base（本地声音复刻）",
}

# Historical project records may still contain these identifiers.  They are
# kept only so the UI can explain that the old route is no longer available.
REMOVED_CLONE_MODELS = {"fish-s2-pro", "minimax-voiceclone", "siliconflow-cosyvoice2"}
CLONE_MODEL_ALIASES = {"qwen-voice": "cosyvoice-v3.5-plus"}

DIRECT_TTS_MODELS = {
    "qwen-builtin-tts": "百炼 Qwen Audio TTS",
    "mimo-v2.5-tts": "MiMo v2.5 TTS",
    "qwen3-tts-local-customvoice": "Qwen3-TTS CustomVoice（本地内置音色）",
}

DIRECT_TTS_VOICES = {
    "qwen-builtin-tts": {"longanlingxin": "龙安灵心（百炼）"},
    "mimo-v2.5-tts": {"冰糖": "冰糖（MiMo）"},
    "qwen3-tts-local-customvoice": LOCAL_QWEN_VOICES,
}

LIPSYNC_MODELS = {
    "videoretalk": "百炼 VideoRetalk（云端）",
    "musetalk-1.5": "MuseTalk 1.5（本地）",
    "heygem": "HeyGem（本地服务）",
}

GENERIC_SPEECH_MODEL_MARKERS = (
    "asr", "sensevoice", "paraformer", "whisper", "transcription",
    "tts", "cosyvoice", "voice-clone", "speech-syn",
)


def normalize_clone_model(model: str | None) -> str:
    """Map historical voice-clone identifiers to the current route."""
    return CLONE_MODEL_ALIASES.get(model or "", model or "")


def cosyvoice_instruction(speed: str, emotion: str, custom: str = "") -> str:
    """Translate the UI voice controls to CosyVoice's instruction text."""
    pace = {"slow": "语速稍慢", "standard": "语速适中", "fast": "语速稍快"}.get(speed, "语速适中")
    tone = {
        "natural": "语气自然、亲切",
        "warm": "语气热情、有感染力",
        "steady": "语气沉稳、可信",
    }.get(emotion, "语气自然、亲切")
    if custom.strip():
        return f"保持参考音频本人的音色与发声习惯。{custom.strip()}"
    return f"保持参考音频本人的音色与发声习惯。{pace}，{tone}，像本人面对镜头做自然口播分享；不要播音腔，不要夸张表演。"


def is_generic_text_model(model_id: str) -> bool:
    """Return whether a discovered model is safe to expose as a chat model."""
    lowered = model_id.lower()
    return not any(marker in lowered for marker in GENERIC_SPEECH_MODEL_MARKERS)


MODEL_ROUTE_OPTIONS = {
    "script": {"auto", "mimo-v2.5", "qwen3.7-flash"},
    "rewrite": {"auto", "mimo-v2.5", "mimo-v2.5-pro", "qwen3.7-flash"},
    "asr": set(ASR_MODELS),
    "subtitle_asr": {
        "auto", "paraformer-realtime-v2", "qwen-audio-3.0-asr-flash-filetrans",
        "local:funasr-paraformer", "local:funasr-sensevoice",
    },
    "voice_clone": set(VOICE_CLONE_MODELS),
    "direct_tts": set(DIRECT_TTS_MODELS),
    "lipsync": set(LIPSYNC_MODELS),
    "edit_plan": {"auto", "mimo-v2.5", "qwen3.7-flash"},
}


ALL_MODEL_LABELS = {
    **ASR_MODELS,
    **REWRITE_MODELS,
    **VOICE_CLONE_MODELS,
    **DIRECT_TTS_MODELS,
    **LIPSYNC_MODELS,
}


def model_label(model: str) -> str:
    return ALL_MODEL_LABELS.get(model, model)
