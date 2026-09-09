"""Command-line client for the local afan Talking Head Agent service.

The CLI deliberately talks to the same FastAPI endpoints as the web UI.  It
does not execute arbitrary shell commands or import the application in-process;
this keeps it useful for other AI clients and makes failures observable at the
HTTP boundary.  All successful commands write JSON to stdout and diagnostics
go to stderr.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Iterable

import httpx


DEFAULT_BASE_URL = "http://127.0.0.1:8000"
DEFAULT_TIMEOUT = 30.0


class CliError(RuntimeError):
    """An expected, user-actionable CLI failure."""


class ApiClient:
    def __init__(self, base_url: str, timeout: float = DEFAULT_TIMEOUT) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            response = httpx.request(method, f"{self.base_url}{path}", timeout=self.timeout, **kwargs)
        except httpx.HTTPError as error:
            raise CliError(
                f"无法连接口播智能体服务：{self.base_url}。请先启动服务，或使用 --base-url 指定地址。\n{error}"
            ) from error
        if response.is_error:
            detail: Any = None
            try:
                detail = response.json().get("detail")
            except (ValueError, TypeError):
                pass
            message = str(detail or response.text or f"HTTP {response.status_code}").strip()
            raise CliError(f"服务请求失败（HTTP {response.status_code}）：{message}")
        return response

    def json(self, method: str, path: str, **kwargs: Any) -> Any:
        response = self.request(method, path, **kwargs)
        try:
            return response.json()
        except ValueError as error:
            raise CliError("服务返回的不是有效 JSON。") from error


def _positive_timeout(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("超时时间必须是数字。") from error
    if parsed <= 0:
        raise argparse.ArgumentTypeError("超时时间必须大于 0。")
    return parsed


def _existing_file(value: str) -> str:
    path = Path(value).expanduser()
    if not path.is_file():
        raise argparse.ArgumentTypeError(f"文件不存在：{value}")
    return str(path)


def _add_project_id(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("job_id", help="项目或任务 ID")


def _form_bool(value: bool) -> str:
    return "true" if value else "false"


def _command_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="afan-agent",
        description="通过本机 HTTP 服务调用 afan 口播智能体。默认输出 JSON。",
    )
    parser.add_argument(
        "--base-url",
        default=os.environ.get("AFAN_AGENT_URL", DEFAULT_BASE_URL),
        help="服务地址，默认读取 AFAN_AGENT_URL 或 http://127.0.0.1:8000",
    )
    parser.add_argument(
        "--timeout",
        type=_positive_timeout,
        default=DEFAULT_TIMEOUT,
        help="单次 HTTP 请求超时秒数；视频任务本身通过 job_id 异步执行",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("health", help="检查服务和模型适配器状态")
    subparsers.add_parser("projects", help="列出已保存项目")
    subparsers.add_parser("jobs", help="列出任务")
    subparsers.add_parser("models", help="列出当前可用模型和模型路由")

    status = subparsers.add_parser("status", help="查询任务/项目状态")
    _add_project_id(status)

    download = subparsers.add_parser("download", help="下载已生成的成片")
    _add_project_id(download)
    download.add_argument("--output", type=Path, required=True, help="保存到本地的 MP4 路径")

    create_job = subparsers.add_parser("create-job", help="提交人物视频和文案，启动完整旧版流水线")
    create_job.add_argument("--video", type=_existing_file, required=True, help="人物视频路径")
    create_job.add_argument("--instruction", required=True, help="文案改写要求")
    create_job.add_argument("--consent", action="store_true", help="确认拥有肖像和声音使用授权")
    voice_group = create_job.add_mutually_exclusive_group()
    voice_group.add_argument("--voice-id", help="复用已有音色 ID")
    voice_group.add_argument("--no-create-voice", action="store_true", help="不创建新音色")

    script = subparsers.add_parser("create-script", help="仅根据需求创建一个 AI 文案项目")
    script.add_argument("--prompt", required=True, help="文案需求")
    script.add_argument("--model", help="可选的文案模型 ID")

    extract = subparsers.add_parser("extract-reference", help="从本地参考视频提取文案并改写")
    extract.add_argument("--video", type=_existing_file, required=True, help="本地参考视频路径")
    extract.add_argument("--instruction", default="", help="改写要求")
    extract.add_argument("--asr-model", help="可选的 ASR 模型 ID")
    extract.add_argument("--rewrite-model", help="可选的改写模型 ID")
    extract.add_argument("--authorized", action="store_true", help="确认拥有参考内容的使用授权")

    save_text = subparsers.add_parser("save-rewritten", help="保存用户确认后的口播文案")
    _add_project_id(save_text)
    save_text.add_argument("--text", required=True, help="改写后的文案")

    person = subparsers.add_parser("upload-person-video", help="给已有项目上传人物视频")
    _add_project_id(person)
    person.add_argument("--video", type=_existing_file, required=True)
    person.add_argument("--consent", action="store_true", help="确认拥有肖像使用授权")

    generate = subparsers.add_parser("generate-video", help="启动改口型视频生成")
    _add_project_id(generate)
    generate.add_argument("--strategy", choices=("keep_video", "trim_tail"), default="keep_video")
    generate.add_argument("--lipsync-model", help="可选的改口型模型 ID")
    return parser


def _multipart_file(path: str) -> tuple[str, Any, str]:
    file_path = Path(path)
    content_type = "video/mp4" if file_path.suffix.lower() in {".mp4", ".mov", ".m4v", ".webm", ".avi", ".mkv"} else "application/octet-stream"
    return (file_path.name, file_path.open("rb"), content_type)


def _close_files(files: Iterable[tuple[str, Any, str]]) -> None:
    for _name, handle, _content_type in files:
        handle.close()


def execute(args: argparse.Namespace, client: ApiClient) -> Any:
    command = args.command
    if command == "health":
        return client.json("GET", "/api/health")
    if command == "projects":
        return client.json("GET", "/api/projects")
    if command == "jobs":
        return client.json("GET", "/api/jobs")
    if command == "models":
        return client.json("GET", "/api/settings")
    if command == "status":
        return client.json("GET", f"/api/jobs/{args.job_id}")
    if command == "download":
        args.output.parent.mkdir(parents=True, exist_ok=True)
        response = client.request("GET", f"/api/jobs/{args.job_id}/download")
        args.output.write_bytes(response.content)
        return {"ok": True, "job_id": args.job_id, "path": str(args.output.resolve()), "bytes": len(response.content)}
    if command == "create-script":
        data = {"prompt": args.prompt}
        if args.model:
            data["script_model"] = args.model
        return client.json("POST", "/api/projects/ai-script", data=data)
    if command == "save-rewritten":
        return client.json("POST", f"/api/projects/{args.job_id}/rewritten", data={"rewritten_text": args.text})
    if command == "generate-video":
        data = {"strategy": args.strategy}
        if args.lipsync_model:
            data["lipsync_model"] = args.lipsync_model
        return client.json("POST", f"/api/projects/{args.job_id}/generate-video", data=data)
    if command == "create-job":
        data = {
            "instruction": args.instruction,
            "create_voice": _form_bool(not args.no_create_voice and not args.voice_id),
            "consent": _form_bool(args.consent),
        }
        if args.voice_id:
            data["voice_id"] = args.voice_id
            data["create_voice"] = "false"
        files = [_multipart_file(args.video)]
        try:
            return client.json("POST", "/api/jobs", data=data, files={"video": files[0]})
        finally:
            _close_files(files)
    if command == "extract-reference":
        data = {
            "instruction": args.instruction,
            "reference_content_authorized": _form_bool(args.authorized),
        }
        if args.asr_model:
            data["asr_model"] = args.asr_model
        if args.rewrite_model:
            data["rewrite_model"] = args.rewrite_model
        files = [_multipart_file(args.video)]
        try:
            return client.json("POST", "/api/projects/extract-upload", data=data, files={"video": files[0]})
        finally:
            _close_files(files)
    if command == "upload-person-video":
        data = {"consent": _form_bool(args.consent)}
        files = [_multipart_file(args.video)]
        try:
            return client.json("POST", f"/api/projects/{args.job_id}/person-video", data=data, files={"video": files[0]})
        finally:
            _close_files(files)
    raise CliError(f"未知命令：{command}")


def main(argv: list[str] | None = None) -> int:
    parser = _command_parser()
    args = parser.parse_args(argv)
    try:
        result = execute(args, ApiClient(args.base_url, args.timeout))
    except CliError as error:
        print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through the CLI smoke test
    raise SystemExit(main())
