from pathlib import Path
import zipfile

import pytest

from app.local_engine import _extract_archive, inspect_engine, resolve_ffmpeg, tools_ffmpeg_dir


def _make_engine(root: Path) -> None:
    (root / "models" / "musetalkV15").mkdir(parents=True)
    (root / "models" / "whisper").mkdir()
    (root / "scripts").mkdir()
    (root / "models" / "musetalkV15" / "unet.pth").write_bytes(b"weights")
    (root / "models" / "musetalkV15" / "musetalk.json").write_text("{}", encoding="utf-8")
    (root / "scripts" / "inference.py").write_text("# test", encoding="utf-8")


def test_inspect_engine_reports_required_files(tmp_path):
    engine = tmp_path / "engine"
    _make_engine(engine)
    result = inspect_engine(engine)
    assert result["installed"] is True
    assert result["missing"] == []


def test_extract_archive_rejects_path_traversal(tmp_path):
    archive = tmp_path / "bad.zip"
    with zipfile.ZipFile(archive, "w") as source:
        source.writestr("../escape.txt", "no")
    with pytest.raises(ValueError, match="不安全路径"):
        _extract_archive(archive, tmp_path / "out")


def test_resolve_ffmpeg_prefers_data_tools_dir(tmp_path, monkeypatch):
    monkeypatch.delenv("FFMPEG_PATH", raising=False)
    exe = tools_ffmpeg_dir(tmp_path) / "ffmpeg.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"stub")
    assert resolve_ffmpeg(tmp_path) == str(exe)


def test_resolve_ffmpeg_accepts_settings_dir(tmp_path, monkeypatch):
    monkeypatch.delenv("FFMPEG_PATH", raising=False)
    bin_dir = tmp_path / "custom" / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "ffmpeg.exe").write_bytes(b"stub")
    assert resolve_ffmpeg(tmp_path, {"FFMPEG_PATH": str(bin_dir)}) == str(bin_dir / "ffmpeg.exe")


def test_resolve_ffmpeg_falls_back_to_path(tmp_path, monkeypatch):
    monkeypatch.delenv("FFMPEG_PATH", raising=False)
    monkeypatch.setattr("app.local_engine.shutil.which", lambda name: r"C:\Fake\ffmpeg.exe" if name == "ffmpeg" else None)
    assert resolve_ffmpeg(tmp_path) == r"C:\Fake\ffmpeg.exe"


def test_resolve_ffmpeg_returns_empty_when_nothing_found(tmp_path, monkeypatch):
    monkeypatch.delenv("FFMPEG_PATH", raising=False)
    monkeypatch.setattr("app.local_engine.shutil.which", lambda name: None)
    assert resolve_ffmpeg(tmp_path) == ""

