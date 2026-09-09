import os
from pathlib import Path

from app.local_engine_registry import analyze_model_bundle


def _touch(path: Path, name: str = "config.json") -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / name).write_text("{}", encoding="utf-8")


def _make_voice_bundle(root: Path) -> None:
    _touch(root / "models" / "funasr" / "SenseVoiceSmall", "configuration.json")
    _touch(root / "models" / "funasr" / "fsmn-vad", "configuration.json")
    _touch(root / "models" / "qwen3-tts" / "tokenizer")
    _touch(root / "models" / "qwen3-tts" / "custom-voice-0.6b")
    venv = root / "runtime-py312-cu128"
    _touch(venv / "Scripts", "python.exe")
    _touch(venv / "Lib" / "site-packages" / "qwen_tts", "__init__.py")
    _touch(venv / "Lib" / "site-packages" / "funasr", "__init__.py")
    base = root / "runtime-py312"
    _touch(base, "python.exe")
    _touch(base / "Lib" / "site-packages" / "torch", "__init__.py")
    # venv 指向已不存在的 D 盘 base——模拟整体挪盘后的损坏状态。
    (venv / "pyvenv.cfg").write_text(
        "home = D:\\model\\afan-voice-engines\\runtime-py312\n"
        "include-system-site-packages = false\n"
        "version = 3.12.14\n"
        "executable = D:\\model\\afan-voice-engines\\runtime-py312\\python.exe\n",
        encoding="utf-8",
    )


def _make_musetalk_bundle(root: Path) -> None:
    engine = root / "engines" / "musetalk-1.5"
    _touch(engine / "models" / "musetalkV15", "unet.pth")
    _touch(engine / "models" / "musetalkV15", "musetalk.json")
    _touch(engine / "models" / "whisper")
    _touch(engine / "scripts", "inference.py")
    py = root / "runtime" / "python" / "python310"
    _touch(py, "python.exe")
    _touch(py / "Lib" / "site-packages" / "torch", "__init__.py")
    _touch(py / "Lib" / "site-packages" / "diffusers", "__init__.py")


def _norm(path) -> str:
    return os.path.normcase(str(path))


def test_analyze_finds_voice_bundle_from_parent(tmp_path):
    _make_voice_bundle(tmp_path / "afan-voice-engines")
    _make_musetalk_bundle(tmp_path / "afan-musetalk-bundle")
    analysis = analyze_model_bundle(tmp_path)
    assert _norm(analysis["funasr_dir"]).endswith(_norm("afan-voice-engines\\models\\funasr"))
    assert _norm(analysis["qwen_dir"]).endswith(_norm("models\\qwen3-tts"))
    assert _norm(analysis["musetalk_dir"]).endswith(_norm("engines\\musetalk-1.5"))
    assert _norm(analysis["qwen_tts_python"]).endswith(_norm("runtime-py312-cu128\\Scripts\\python.exe"))
    assert _norm(analysis["musetalk_python"]).endswith(_norm("python310\\python.exe"))
    # 挪盘损坏的 venv 被自动修复，home 指回包内的 base。
    assert len(analysis["fixes"]) == 1
    cfg = (tmp_path / "afan-voice-engines" / "runtime-py312-cu128" / "pyvenv.cfg").read_text(encoding="utf-8")
    assert "D:\\model" not in cfg
    assert "runtime-py312" in cfg


def test_analyze_accepts_bundle_root_directly(tmp_path):
    _make_voice_bundle(tmp_path / "afan-voice-engines")
    analysis = analyze_model_bundle(tmp_path / "afan-voice-engines")
    assert analysis["bundle_root"] is not None
    assert analysis["funasr_dir"] is not None
    assert analysis["qwen_dir"] is not None
    assert analysis["qwen_tts_python"] is not None


def test_analyze_reports_nothing_for_empty_dir(tmp_path):
    analysis = analyze_model_bundle(tmp_path)
    assert analysis["funasr_dir"] is None
    assert analysis["qwen_dir"] is None
    assert analysis["musetalk_dir"] is None
    assert analysis["qwen_tts_python"] is None
    assert analysis["musetalk_python"] is None
