from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

import scripts.afan_agent_cli as cli
from scripts.afan_agent_cli import ApiClient, _command_parser, compute_next_actions, execute


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


# ── schema / guide：AI 自描述入口 ──


def test_schema_covers_every_parser_command():
    parser = _command_parser()
    parser_names = set(parser._subparsers._group_actions[0].choices)
    args = parser.parse_args(["schema"])
    catalog = execute(args, ApiClient("http://127.0.0.1:8000"))
    schema_names = {item["name"] for item in catalog["commands"]}
    assert schema_names == parser_names
    by_name = {item["name"]: item for item in catalog["commands"]}
    voice = by_name["voice-preview"]
    flag_names = {entry["name"] for entry in voice["args"]}
    assert {"--mode", "--sample", "--voice-id", "--consent", "--set"} <= flag_names


def test_guide_falls_back_to_builtin_contract(monkeypatch):
    def refused(self, method, path, **kwargs):
        raise cli.CliError("服务请求失败（HTTP 404）")

    monkeypatch.setattr(ApiClient, "json", refused)
    args = _command_parser().parse_args(["guide"])
    result = execute(args, ApiClient("http://127.0.0.1:8000"))
    assert result["source"] == "builtin"
    assert result["workflow"]["constraints"]["max_preview_seconds"] == 120


def test_guide_prefers_server_contract(monkeypatch):
    def ok(self, method, path, **kwargs):
        return {"ok": True, "workflow": {"version": 99, "steps": []}}

    monkeypatch.setattr(ApiClient, "json", ok)
    args = _command_parser().parse_args(["guide"])
    result = execute(args, ApiClient("http://127.0.0.1:8000"))
    assert result == {"ok": True, "source": "server", "workflow": {"version": 99, "steps": []}}


# ── next_actions：状态机引导 ──


def test_next_actions_running_suggests_wait():
    actions = compute_next_actions({"id": "j1", "status": "running", "stage": "配音中", "progress": 40})
    assert actions[0]["command"] == "wait j1"


def test_next_actions_failed_carries_error():
    actions = compute_next_actions({"id": "j1", "status": "failed", "error": "TTS 超时"})
    assert "TTS 超时" in actions[0]["reason"]


def test_next_actions_pipeline_progression():
    empty = compute_next_actions({"id": "j1", "status": "ready"})
    assert any("create-script" in item["command"] for item in empty)

    raw_text = compute_next_actions({"id": "j1", "status": "ready", "transcript": "原文"})
    assert any(item["command"].startswith("rewrite j1") for item in raw_text)
    assert any(item["command"].startswith("save-rewritten j1") for item in raw_text)

    person = compute_next_actions({"id": "j1", "status": "ready", "rewritten_text": "文案", "person_duration": 10.0})
    assert any(item["command"].startswith("voice-preview j1") for item in person)

    confirm = compute_next_actions(
        {"id": "j1", "status": "ready", "rewritten_text": "文案", "person_duration": 10.0, "preview_audio_name": "a.wav"}
    )
    assert confirm[0]["command"] == "voice-confirm j1"

    generate = compute_next_actions(
        {"id": "j1", "status": "ready", "rewritten_text": "文案", "person_duration": 10.0,
         "preview_audio_name": "a.wav", "preview_confirmed": True, "preview_duration": 9.0}
    )
    assert generate[0]["command"] == "generate-video j1"

    export = compute_next_actions(
        {"id": "j1", "status": "ready", "rewritten_text": "文案", "person_duration": 10.0,
         "preview_confirmed": True, "output_name": "out.mp4", "edit_output_name": "final.mp4"}
    )
    assert any("--artifact final" in item["command"] for item in export)


def test_next_actions_flags_overlong_preview():
    actions = compute_next_actions(
        {"id": "j1", "status": "ready", "rewritten_text": "文案", "person_duration": 10.0,
         "preview_confirmed": True, "preview_duration": 30.0}
    )
    assert "重新试听" in actions[0]["reason"] or "voice-preview" in actions[0]["command"]


def test_status_command_attaches_next_actions(monkeypatch):
    def fake_json(self, method, path, **kwargs):
        assert path == "/api/jobs/j1"
        return {"id": "j1", "status": "ready", "rewritten_text": "文案"}

    monkeypatch.setattr(ApiClient, "json", fake_json)
    args = _command_parser().parse_args(["status", "j1"])
    result = execute(args, ApiClient("http://127.0.0.1:8000"))
    assert result["next_actions"]
    args = _command_parser().parse_args(["status", "j1", "--brief"])
    brief = execute(args, ApiClient("http://127.0.0.1:8000"))
    assert set(brief) <= set(cli.BRIEF_FIELDS) | {"next_actions"}


# ── 新命令的参数 → HTTP 映射 ──


@pytest.mark.parametrize(
    ("argv", "expected_method", "expected_path", "expected_data_keys"),
    [
        (["rewrite", "j1", "--instruction", "更口语", "--model", "m"], "POST", "/api/projects/j1/rewrite", {"instruction", "rewrite_model"}),
        (["transcript", "j1", "--text", "文案"], "POST", "/api/projects/j1/transcript", {"transcript"}),
        (["voice-confirm", "j1"], "POST", "/api/projects/j1/voice-confirm", set()),
        (["cancel-video", "j1"], "POST", "/api/projects/j1/cancel-video", set()),
        (["draft", "--name", "新项目"], "POST", "/api/projects/draft", {"source_name"}),
        (["rename", "j1", "--name", "改名"], "POST", "/api/projects/j1/rename", {"name"}),
        (["save", "j1"], "POST", "/api/projects/j1/save", set()),
        (["delete", "j1"], "POST", "/api/projects/j1/delete", set()),
        (["library"], "GET", "/api/library", set()),
        (["library", "--kind", "voice"], "GET", "/api/library?kind=voice", set()),
        (["providers"], "GET", "/api/digital-human/providers", set()),
    ],
)
def test_new_command_mappings(monkeypatch, argv, expected_method, expected_path, expected_data_keys):
    calls = {}

    def fake_json(self, method, path, **kwargs):
        calls.update(method=method, path=path, kwargs=kwargs)
        return {"ok": True}

    monkeypatch.setattr(ApiClient, "json", fake_json)
    result = execute(_command_parser().parse_args(argv), ApiClient("http://127.0.0.1:8000"))
    assert result == {"ok": True}
    assert calls["method"] == expected_method
    assert calls["path"] == expected_path
    assert set(calls["kwargs"].get("data") or {}) == expected_data_keys


def test_edit_command_maps_flags_and_sets(monkeypatch):
    calls = {}

    def fake_json(self, method, path, **kwargs):
        calls.update(method=method, path=path, kwargs=kwargs)
        return {"id": "j1", "accepted": True}

    monkeypatch.setattr(ApiClient, "json", fake_json)
    argv = [
        "edit", "j1", "--title", "标题", "--no-subtitle-enabled", "--subtitle-font-size", "52",
        "--layout-mode", "pip", "--music-volume", "0.2", "--set", "broll_enabled=true",
        "--set", "sticker=贴纸",
    ]
    execute(_command_parser().parse_args(argv), ApiClient("http://127.0.0.1:8000"))
    data = calls["kwargs"]["data"]
    assert calls["path"] == "/api/projects/j1/edit"
    assert data["title"] == "标题"
    assert data["subtitle_enabled"] == "false"
    assert data["subtitle_font_size"] == "52"
    assert data["layout_mode"] == "pip"
    assert data["music_volume"] == "0.2"
    assert data["broll_enabled"] == "true"
    assert data["sticker"] == "贴纸"


def test_auto_edit_command_sends_locked(monkeypatch):
    calls = {}

    def fake_json(self, method, path, **kwargs):
        calls.update(method=method, path=path, kwargs=kwargs)
        return {"id": "j1", "accepted": True}

    monkeypatch.setattr(ApiClient, "json", fake_json)
    execute(_command_parser().parse_args(["auto-edit", "j1", "--locked", "title,cover_text"]), ApiClient("http://127.0.0.1:8000"))
    assert calls["path"] == "/api/projects/j1/auto-edit"
    assert calls["kwargs"]["data"]["locked"] == "title,cover_text"


def test_voice_preview_maps_fields_and_passthrough(monkeypatch, tmp_path):
    calls = {}

    def fake_json(self, method, path, **kwargs):
        calls.update(method=method, path=path, kwargs=kwargs)
        return {"id": "j1", "accepted": True}

    monkeypatch.setattr(ApiClient, "json", fake_json)
    sample = tmp_path / "sample.wav"
    sample.write_bytes(b"riff")
    argv = [
        "voice-preview", "j1", "--mode", "upload", "--sample", str(sample), "--consent",
        "--speed", "fast", "--emotion", "warm", "--model", "clone-x",
        "--set", "voice_rate=1.2",
    ]
    execute(_command_parser().parse_args(argv), ApiClient("http://127.0.0.1:8000"))
    data = calls["kwargs"]["data"]
    assert calls["path"] == "/api/projects/j1/voice-preview"
    assert data["mode"] == "upload"
    assert data["voice_consent"] == "true"
    assert data["speed"] == "fast"
    assert data["emotion"] == "warm"
    assert data["voice_clone_model"] == "clone-x"
    assert data["voice_rate"] == "1.2"
    assert "sample" in calls["kwargs"]["files"]

    calls.clear()
    argv_direct = ["voice-preview", "j1", "--mode", "direct", "--model", "tts-x"]
    execute(_command_parser().parse_args(argv_direct), ApiClient("http://127.0.0.1:8000"))
    assert calls["kwargs"]["data"]["direct_tts_model"] == "tts-x"
    assert calls["kwargs"]["files"] is None


def test_download_artifact_variants(monkeypatch, tmp_path):
    requested = []

    def fake_request(self, method, path, **kwargs):
        requested.append(path)

        class Response:
            content = b"mp4"

        return Response()

    monkeypatch.setattr(ApiClient, "request", fake_request)
    output = tmp_path / "out.mp4"
    execute(_command_parser().parse_args(["download", "j1", "--output", str(output)]), ApiClient("http://127.0.0.1:8000"))
    assert requested[-1] == "/api/jobs/j1/download"
    execute(
        _command_parser().parse_args(["download", "j1", "--output", str(output), "--artifact", "final"]),
        ApiClient("http://127.0.0.1:8000"),
    )
    assert requested[-1] == "/api/projects/j1/download"
    execute(
        _command_parser().parse_args(["download", "j1", "--output", str(output), "--artifact", "voice-preview"]),
        ApiClient("http://127.0.0.1:8000"),
    )
    assert requested[-1] == "/api/projects/j1/download/voice-preview"


def test_wait_polls_until_terminal(monkeypatch, capsys):
    responses = iter([
        {"id": "j1", "status": "running", "stage": "合成", "progress": 10},
        {"id": "j1", "status": "failed", "error": "MuseTalk 崩溃"},
    ])
    monkeypatch.setattr(ApiClient, "json", lambda self, method, path, **kwargs: next(responses))
    monkeypatch.setattr(cli.time, "sleep", lambda _s: None)
    ticks = iter([0.0, 1.0, 2.0])
    monkeypatch.setattr(cli.time, "monotonic", lambda: next(ticks))
    assert cli.main(["wait", "j1", "--interval", "1", "--timeout", "100"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "failed"
    assert payload["next_actions"]


def test_wait_timeout_reports_error(monkeypatch, capsys):
    monkeypatch.setattr(ApiClient, "json", lambda self, method, path, **kwargs: {"id": "j1", "status": "running"})
    monkeypatch.setattr(cli.time, "sleep", lambda _s: None)
    ticks = iter([0.0, 5.0, 10.0])
    monkeypatch.setattr(cli.time, "monotonic", lambda: next(ticks))
    assert cli.main(["wait", "j1", "--interval", "1", "--timeout", "9"]) == 1
    error = json.loads(capsys.readouterr().err)
    assert error["ok"] is False
    assert "超时" in error["error"]


def test_request_retries_next_token_on_403(monkeypatch):
    client = ApiClient("http://127.0.0.1:8000")
    client.tokens = ["stale-token", "fresh-token"]
    used_tokens = []
    responses = iter([
        cli._UrllibResponse(403, '{"detail":"握手失效"}'.encode("utf-8")),
        cli._UrllibResponse(200, '{"id":"j1"}'.encode("utf-8")),
    ])

    def fake_send(self, method, url, headers, kwargs):
        used_tokens.append(headers.get("x-afan-token"))
        return next(responses)

    monkeypatch.setattr(ApiClient, "_send", fake_send)
    result = client.request("POST", "/api/projects/draft", data={"source_name": "x"})
    assert result.status_code == 200
    assert used_tokens == ["stale-token", "fresh-token"]


def test_get_requests_do_not_send_token(monkeypatch):
    client = ApiClient("http://127.0.0.1:8000")
    client.tokens = ["some-token"]
    seen = {}

    def fake_send(self, method, url, headers, kwargs):
        seen.update(headers)
        return cli._UrllibResponse(200, '{"ok":true}'.encode("utf-8"))

    monkeypatch.setattr(ApiClient, "_send", fake_send)
    client.request("GET", "/api/health")
    assert "x-afan-token" not in seen


def test_key_value_parser_rejects_bad_input():
    with pytest.raises(SystemExit):
        _command_parser().parse_args(["edit", "j1", "--set", "没有等号"])
