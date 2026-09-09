import json

from app.service_connections import (
    clean_service_models,
    clean_service_url,
    load_model_routes,
    load_service_connections,
    service_connection_for_model,
    service_model_options,
)


def test_service_inputs_are_normalized_and_deduplicated():
    assert clean_service_url("https://example.com/v1/") == "https://example.com/v1"
    assert clean_service_models("qwen, qwen，deepseek, bad\nitem") == ["qwen", "deepseek"]


def test_service_connection_lookup_and_step_options(tmp_path):
    database = tmp_path / "services.json"
    database.write_text(json.dumps([{
        "id": "demo",
        "name": "Demo",
        "base_url": "https://example.com/v1",
        "api_key": "secret",
        "connections": [
            {"capability": "chat", "adapter": "openai-chat", "models": ["qwen3"]},
            {"capability": "tts", "adapter": "openai-speech", "models": ["voice"]},
        ],
    }]), encoding="utf-8")

    match = service_connection_for_model("service:demo:chat:qwen3", "chat", database)
    assert match and match[0]["name"] == "Demo" and match[2] == "qwen3"
    assert service_connection_for_model("service:demo:tts:voice", "tts", database) is None
    assert service_model_options("script", database)[0]["label"] == "Demo · qwen3"
    assert service_model_options("direct_tts", database) == []


def test_route_loader_keeps_defaults_when_saved_route_is_invalid(tmp_path):
    database = tmp_path / "routes.json"
    database.write_text(json.dumps({"script": "qwen3", "rewrite": "blocked"}), encoding="utf-8")
    defaults = {"script": "auto", "rewrite": "auto"}

    routes = load_model_routes(database, defaults, lambda step, model: model == "qwen3")

    assert routes == {"script": "qwen3", "rewrite": "auto"}


def test_invalid_service_records_are_ignored(tmp_path):
    database = tmp_path / "services.json"
    database.write_text(json.dumps([{
        "id": "bad",
        "name": "Bad",
        "base_url": "file:///secret",
        "connections": [],
    }]), encoding="utf-8")

    assert load_service_connections(database) == []
