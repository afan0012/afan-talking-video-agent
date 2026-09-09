from pathlib import Path

from app.local_runtime import qwen_engine_status
from app.qwen_tts_local import CUSTOM_VOICES, LocalQwenTTS


def test_qwen_local_health_is_unconfigured_without_url():
    result = LocalQwenTTS({}).health()
    assert result["ok"] is False
    assert result["configured"] is False


def test_qwen_custom_voice_inventory_is_explicit():
    assert "Vivian" in CUSTOM_VOICES
    assert "Uncle_Fu" in CUSTOM_VOICES


def _touch(root: Path, relative: str) -> None:
    path = root / relative / "config.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}", encoding="utf-8")


def test_qwen_engine_status_detects_official_layout(tmp_path):
    _touch(tmp_path / "engines" / "qwen3-tts", "Qwen3-TTS-Tokenizer-12Hz")
    _touch(tmp_path / "engines" / "qwen3-tts", "Qwen3-TTS-12Hz-0.6B-Base")
    result = qwen_engine_status(tmp_path, {})
    assert result["custom"] is False
    assert result["installed"] is True
    assert result["tokenizer"] is True
    assert result["voice_clone"] is True
    assert result["custom_voice"] is False


def test_qwen_engine_status_accepts_custom_root_and_bundle_names(tmp_path):
    root = tmp_path / "bundle" / "qwen3-tts"
    _touch(root, "tokenizer")
    _touch(root, "custom-voice-0.6b")
    result = qwen_engine_status(tmp_path, {"QWEN_TTS_ENGINE_ROOT": str(root)})
    assert result["custom"] is True
    assert result["root"] == str(root)
    assert result["tokenizer"] is True
    assert result["custom_voice"] is True
    assert result["installed"] is True
    assert result["custom_model_dir"].endswith("custom-voice-0.6b")


def test_qwen_engine_status_missing_tokenizer_is_not_installed(tmp_path):
    root = tmp_path / "bundle" / "qwen3-tts"
    _touch(root, "custom-voice-0.6b")
    result = qwen_engine_status(tmp_path, {"QWEN_TTS_ENGINE_ROOT": str(root)})
    assert result["installed"] is False

