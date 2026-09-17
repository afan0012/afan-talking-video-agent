from __future__ import annotations

import io
import json
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

import scripts.afan_agent_cli as cli
import scripts.build_workbuddy_skill as builder
from scripts.afan_agent_cli import ApiClient, _command_parser, execute


REQUIRED_FRONTMATTER = (
    "name", "display_name", "display_name_en", "description",
    "description_zh", "description_en", "category", "version", "author",
)


def test_build_produces_workbuddy_skill_zip(tmp_path):
    zip_path = builder.build(tmp_path)
    with zipfile.ZipFile(zip_path) as bundle:
        names = set(bundle.namelist())
    assert {
        f"{builder.SKILL_NAME}/SKILL.md",
        f"{builder.SKILL_NAME}/scripts/afan_agent_cli.py",
        f"{builder.SKILL_NAME}/references/agent-api.md",
    } <= names


def test_skill_md_has_workbuddy_required_frontmatter(tmp_path):
    builder.build(tmp_path)
    content = (tmp_path / builder.SKILL_NAME / "SKILL.md").read_text(encoding="utf-8")
    for key in REQUIRED_FRONTMATTER:
        line = next((item for item in content.splitlines() if item.startswith(f"{key}:")), None)
        assert line, f"frontmatter 缺少 {key}"
        assert line.partition(":")[2].strip(), f"{key} 不能为空"


def test_bundled_cli_matches_repo_source(tmp_path):
    builder.build(tmp_path)
    bundled = (tmp_path / builder.SKILL_NAME / "scripts" / "afan_agent_cli.py").read_bytes()
    assert bundled == Path(cli.__file__).read_bytes()


def test_cli_works_without_httpx(monkeypatch):
    def fake_urllib_request(method, url, headers, kwargs, timeout):
        assert url.endswith("/api/health")
        return cli._UrllibResponse(200, json.dumps({"ok": True}).encode("utf-8"))

    monkeypatch.setattr(cli, "httpx", None)
    monkeypatch.setattr(cli, "_urllib_request", fake_urllib_request)
    result = execute(_command_parser().parse_args(["health"]), ApiClient("http://127.0.0.1:8000"))
    assert result == {"ok": True}


def test_urllib_request_maps_http_error_to_response(monkeypatch):
    def raise_http_error(request, timeout):
        raise urllib.error.HTTPError(request.full_url, 409, "conflict", hdrs=None, fp=io.BytesIO('{"detail":"冲突"}'.encode("utf-8")))

    monkeypatch.setattr(urllib.request, "urlopen", raise_http_error)
    response = cli._urllib_request("POST", "http://127.0.0.1:8000/api/x", {}, {}, 5)
    assert response.is_error and response.status_code == 409 and response.json()["detail"] == "冲突"


def test_multipart_body_encodes_fields_and_files(tmp_path):
    sample = tmp_path / "s.wav"
    sample.write_bytes(b"RIFF-data")
    with sample.open("rb") as handle:
        body, boundary = cli._multipart_body({"mode": "upload"}, {"sample": ("s.wav", handle, "audio/wav")})
    text = body.decode("utf-8", errors="replace")
    assert boundary in text
    assert 'name="mode"' in text and "upload" in text
    assert 'name="sample"; filename="s.wav"' in text and "RIFF-data" in text
    assert body.endswith(f"--{boundary}--\r\n".encode("utf-8"))
