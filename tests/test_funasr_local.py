from pathlib import Path

from app.funasr_local import FunASRAdapter, extract_text, load_config, model_spec, walk_timeline
from app.local_engine_registry import funasr_engine_status


def test_walk_timeline_treats_exactly_1000_as_milliseconds():
    timeline = walk_timeline({"sentence_info": [{"text": "你好", "start": 0, "end": 1000}]})
    assert timeline == [{"start": 0.0, "end": 1.0, "text": "你好"}]


def test_extract_text_prefers_sentence_records_over_aggregate_text():
    result = [{"text": "这是整段文本", "sentence_info": [{"text": "第一句"}, {"text": "第二句"}]}]
    assert extract_text(result) == "第一句\n第二句"


def test_local_config_sanitizes_device_and_model_path(tmp_path: Path):
    config = tmp_path / "local_funasr.json"
    config.write_text('{"device":"cuda:bogus","model_path":"  /models/funasr  "}', encoding="utf-8")
    assert load_config(config, {"FUNASR_DEVICE": "cpu"}) == {"device": "cpu", "model_path": "/models/funasr"}


def test_adapter_does_not_load_model_until_transcribe(monkeypatch, tmp_path: Path):
    adapter = FunASRAdapter(tmp_path / "missing.json", {})
    monkeypatch.setattr("app.funasr_local.funasr_available", lambda *args, **kwargs: False)
    assert adapter.available() is False


def test_standard_engine_layout_supplies_local_paths_without_cache_download(tmp_path: Path):
    root = tmp_path / "engines" / "funasr"
    for name in ("paraformer-zh", "fsmn-vad", "ct-punc"):
        folder = root / name
        folder.mkdir(parents=True)
        (folder / "config.json").write_text("{}", encoding="utf-8")
    spec = model_spec("local:funasr-paraformer", tmp_path / "settings.json", engine_root=root)
    assert spec["model"] == str(root / "paraformer-zh")
    assert spec["vad_model"] == str(root / "fsmn-vad")
    assert spec["punc_model"] == str(root / "ct-punc")


def test_engine_status_requires_each_official_funasr_component(monkeypatch, tmp_path: Path):
    monkeypatch.setattr("app.local_engine_registry.funasr_available", lambda *args, **kwargs: True)
    monkeypatch.setattr("app.local_engine_registry.discover_runtime_python", lambda *args, **kwargs: "")
    root = tmp_path / "engines" / "funasr"
    for name in ("paraformer-zh", "fsmn-vad", "ct-punc"):
        folder = root / name
        folder.mkdir(parents=True)
        (folder / "configuration.json").write_text("{}", encoding="utf-8")
    status = funasr_engine_status(tmp_path)
    paraformer = next(model for model in status["models"] if model["id"] == "local:funasr-paraformer")
    assert paraformer["installed"] is True
    sensevoice = next(model for model in status["models"] if model["id"] == "local:funasr-sensevoice")
    assert sensevoice["installed"] is False
