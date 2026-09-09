from pathlib import Path

import pytest

from app.musetalk_downloader import MANIFEST, _safe_target, manifest_info


def test_manifest_is_fixed_and_has_no_user_supplied_urls():
    assert manifest_info()["download_available"] is True
    assert len(MANIFEST) >= 10
    assert any(item.get("repo") == "TMElyralab/MuseTalk" for item in MANIFEST)
    assert all(".." not in str(item["path"]).replace("\\", "/").split("/") for item in MANIFEST)


def test_safe_target_rejects_path_escape(tmp_path: Path):
    with pytest.raises(ValueError):
        _safe_target(tmp_path, "../outside.bin")
    with pytest.raises(ValueError):
        _safe_target(tmp_path, "C:/outside.bin")
    assert _safe_target(tmp_path, "models/whisper/config.json").parent.name == "whisper"
