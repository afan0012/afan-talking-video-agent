from __future__ import annotations

import json
from pathlib import Path

import httpx

from scripts.afan_agent_cli import ApiClient, _command_parser, execute


def test_cli_parser_exposes_stable_core_commands():
    parser = _command_parser()
    args = parser.parse_args(["create-script", "--prompt", "写一段介绍"])
    assert args.command == "create-script"
    assert args.prompt == "写一段介绍"


def test_cli_create_script_uses_http_api(monkeypatch):
    calls = {}

    def fake_json(self, method, path, **kwargs):
        calls.update(method=method, path=path, kwargs=kwargs)
        return {"id": "abc123", "status": "queued"}

    monkeypatch.setattr(ApiClient, "json", fake_json)
    args = _command_parser().parse_args(["create-script", "--prompt", "写一段介绍", "--model", "local:ollama:qwen"])
    result = execute(args, ApiClient("http://127.0.0.1:8000"))
    assert result["id"] == "abc123"
    assert calls["method"] == "POST"
    assert calls["path"] == "/api/projects/ai-script"
    assert calls["kwargs"]["data"]["script_model"] == "local:ollama:qwen"


def test_cli_error_payload_is_machine_readable(monkeypatch, capsys):
    def fail(*_args, **_kwargs):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(httpx, "request", fail)
    from scripts.afan_agent_cli import main

    assert main(["health"]) == 1
    error = json.loads(capsys.readouterr().err)
    assert error["ok"] is False
    assert "无法连接" in error["error"]
