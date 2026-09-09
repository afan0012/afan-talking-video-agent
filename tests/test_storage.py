import json

from app.storage import read_json, write_json_atomic


def test_json_store_writes_atomically_and_reads_back(tmp_path):
    path = tmp_path / "nested" / "state.json"
    value = {"items": ["字幕", "封面"], "ready": True}
    write_json_atomic(path, value)
    assert read_json(path, {}) == value
    assert not path.with_suffix(path.suffix + ".tmp").exists()


def test_json_store_returns_default_for_invalid_data(tmp_path):
    path = tmp_path / "broken.json"
    path.write_text("not-json", encoding="utf-8")
    assert read_json(path, []) == []
