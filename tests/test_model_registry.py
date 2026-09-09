from app.model_registry import (
    MODEL_ROUTE_OPTIONS,
    cosyvoice_instruction,
    is_generic_text_model,
    normalize_clone_model,
)
from app import main


def test_model_registry_keeps_routes_and_historical_aliases():
    assert "musetalk-1.5" in MODEL_ROUTE_OPTIONS["lipsync"]
    assert "local:funasr-paraformer" in MODEL_ROUTE_OPTIONS["asr"]
    assert "local:funasr-sensevoice" in MODEL_ROUTE_OPTIONS["subtitle_asr"]
    assert normalize_clone_model("qwen-voice") == "cosyvoice-v3.5-plus"


def test_model_registry_filters_speech_models_from_generic_chat():
    assert is_generic_text_model("qwen3.7-flash")
    assert not is_generic_text_model("qwen-audio-3.0-asr-flash-filetrans")


def test_cosyvoice_instruction_preserves_custom_instruction():
    assert cosyvoice_instruction("fast", "warm", "更有感染力") == "保持参考音频本人的音色与发声习惯。更有感染力"


def test_auto_asr_prefers_cloud_before_optional_local_runtime(monkeypatch):
    monkeypatch.setattr(main, "_has_settings", lambda *names: names == ("DASHSCOPE_API_KEY", "DASHSCOPE_WORKSPACE_ID"))
    monkeypatch.setattr(main, "funasr_available", lambda: True)
    assert main._resolve_asr_model("auto") == "qwen-audio-3.0-asr-flash-filetrans"


def test_auto_asr_uses_local_only_without_cloud_credentials(monkeypatch):
    monkeypatch.setattr(main, "_has_settings", lambda *names: False)
    monkeypatch.setattr(main, "funasr_ready", lambda model_id=None: model_id == "local:funasr-paraformer")
    assert main._resolve_asr_model("auto") == "local:funasr-paraformer"
