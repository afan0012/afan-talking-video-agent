"""Command-line client for the local afan Talking Video Agent service.

The CLI deliberately talks to the same FastAPI endpoints as the web UI.  It
does not execute arbitrary shell commands or import the application in-process;
this keeps it useful for other AI clients and makes failures observable at the
HTTP boundary.  All successful commands write JSON to stdout and diagnostics
go to stderr.

面向 AI 的增强：
- ``status`` 输出自带 ``next_actions``（客户端根据任务字段计算下一步命令）；
- ``wait`` 阻塞轮询直到任务结束，AI 不必自己 sleep；
- ``guide`` 返回机器可读的流程契约（GET /api/workflow，旧服务回退内置副本）；
- ``schema`` 从 argparse 自动生成命令目录，保证文档与实现不漂移。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.parse
import uuid
from pathlib import Path
from typing import Any, Iterable

try:
    import httpx
except ImportError:  # 技能包等分发场景允许零依赖运行，自动退回标准库 urllib。
    httpx = None


DEFAULT_BASE_URL = "http://127.0.0.1:8000"
DEFAULT_TIMEOUT = 30.0
# 这些状态意味着后台任务已结束，wait 可以停止轮询。
TERMINAL_STATUSES = {"ready", "succeeded", "failed"}


def _data_root_candidates() -> list[Path]:
    """与服务端约定一致的数据目录候选（见 app/main.py 的 USER_DATA_ROOT）。"""
    roots: list[Path] = []
    env_dir = os.environ.get("AFAN_DATA_DIR", "").strip()
    if env_dir:
        roots.append(Path(env_dir))
    local_app_data = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    frozen_root = local_app_data / "afan Talking Video Agent"
    location_file = frozen_root / "data_location.txt"
    if location_file.is_file():
        try:
            custom = Path(location_file.read_text(encoding="utf-8").strip().strip('"'))
            if custom.is_absolute():
                roots.append(custom)
        except OSError:
            pass
    roots.append(frozen_root)
    roots.append(Path(__file__).resolve().parents[1])
    return roots


def _read_hint_file(name: str) -> str:
    for root in _data_root_candidates():
        hint = root / name
        if hint.is_file():
            try:
                content = hint.read_text(encoding="utf-8").strip()
            except OSError:
                continue
            if content:
                return content
    return ""


def _hint_values(name: str) -> list[str]:
    """按候选顺序返回各数据目录中提示文件的内容（可能来自安装版残留，需调用方甄别）。"""
    values: list[str] = []
    for root in _data_root_candidates():
        hint = root / name
        if hint.is_file():
            try:
                content = hint.read_text(encoding="utf-8").strip()
            except OSError:
                continue
            if content and content not in values:
                values.append(content)
    return values


def _url_alive(url: str) -> bool:
    import urllib.request

    try:
        with urllib.request.urlopen(f"{url.rstrip('/')}/api/health", timeout=0.8) as response:
            return getattr(response, "status", 200) == 200
    except OSError:
        return False


def _discover_base_url() -> str:
    """AFAN_AGENT_URL 优先；候选（安装版/源码运行各自的 current_url.txt）不止一个时探活甄别。"""
    candidates: list[str] = []
    env_url = os.environ.get("AFAN_AGENT_URL", "").strip()
    if env_url:
        candidates.append(env_url)
    candidates.extend(_hint_values("current_url.txt"))
    candidates.append(DEFAULT_BASE_URL)
    unique = list(dict.fromkeys(candidates))
    if len(unique) == 1:
        return unique[0]
    for url in unique:
        if _url_alive(url):
            return url
    return unique[0]


def _token_candidates() -> list[str]:
    tokens: list[str] = []
    env_token = os.environ.get("AFAN_AGENT_TOKEN", "").strip()
    if env_token:
        tokens.append(env_token)
    tokens.extend(_hint_values("agent_token.txt"))
    return list(dict.fromkeys(tokens))


class CliError(RuntimeError):
    """An expected, user-actionable CLI failure."""


class _UrllibResponse:
    """httpx.Response 的最小子集，供未安装 httpx 的环境使用。"""

    def __init__(self, status_code: int, content: bytes) -> None:
        self.status_code = status_code
        self.content = content
        self.text = content.decode("utf-8", errors="replace")

    @property
    def is_error(self) -> bool:
        return self.status_code >= 400

    def json(self) -> Any:
        return json.loads(self.content.decode("utf-8"))


def _multipart_body(data: dict[str, str] | None, files: dict[str, tuple[str, Any, str]] | None) -> tuple[bytes, str]:
    boundary = f"----afanAgentCli{uuid.uuid4().hex}"
    chunks: list[bytes] = []
    for key, value in (data or {}).items():
        chunks.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n'.encode("utf-8")
            + str(value).encode("utf-8")
            + b"\r\n"
        )
    for field, (filename, handle, content_type) in (files or {}).items():
        chunks.append(
            (
                f'--{boundary}\r\nContent-Disposition: form-data; name="{field}"; filename="{filename}"\r\n'
                f"Content-Type: {content_type}\r\n\r\n"
            ).encode("utf-8")
            + handle.read()
            + b"\r\n"
        )
    chunks.append(f"--{boundary}--\r\n".encode("utf-8"))
    return b"".join(chunks), boundary


def _urllib_request(method: str, url: str, headers: dict[str, str], kwargs: dict[str, Any], timeout: float) -> _UrllibResponse:
    import urllib.error
    import urllib.request

    body: bytes | None
    if kwargs.get("files"):
        body, boundary = _multipart_body(kwargs.get("data"), kwargs["files"])
        headers = {**headers, "Content-Type": f"multipart/form-data; boundary={boundary}"}
    elif kwargs.get("data") is not None:
        body = urllib.parse.urlencode(kwargs["data"]).encode("utf-8")
        headers = {**headers, "Content-Type": "application/x-www-form-urlencoded"}
    else:
        body = None
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return _UrllibResponse(response.status, response.read())
    except urllib.error.HTTPError as error:
        return _UrllibResponse(error.code, error.read())
    except (urllib.error.URLError, OSError) as error:
        raise CliError(
            f"无法连接口播智能体服务：{url.split('/api/')[0]}。请先启动服务，或使用 --base-url 指定地址。\n{error}"
        ) from error


class ApiClient:
    def __init__(self, base_url: str, timeout: float = DEFAULT_TIMEOUT) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        # 同机可能同时存在安装版与源码运行的 token 残留；写操作遇到 403 时
        # 自动尝试下一个候选，保证无论哪个实例在运行都能对上握手 token。
        self.tokens = _token_candidates()
        self.token = self.tokens[0] if self.tokens else ""

    def _send(self, method: str, url: str, headers: dict[str, str], kwargs: dict[str, Any]) -> Any:
        if httpx is None:
            return _urllib_request(method, url, headers, kwargs, self.timeout)
        try:
            return httpx.request(method, url, timeout=self.timeout, headers=headers, **kwargs)
        except httpx.HTTPError as error:
            raise CliError(
                f"无法连接口播智能体服务：{self.base_url}。请先启动服务，或使用 --base-url 指定地址。\n{error}"
            ) from error

    def request(self, method: str, path: str, **kwargs: Any) -> Any:
        headers = {**(kwargs.pop("headers", None) or {})}
        # 服务端只对 /api/ 写操作校验握手 token（local_api_guard），GET 不需要。
        needs_token = path.startswith("/api/") and method.upper() != "GET"
        candidates = (self.tokens or [""]) if needs_token else [""]
        response: Any = None
        for index, token in enumerate(candidates):
            attempt = dict(headers)
            if needs_token and token:
                # 服务端要求所有 /api 写操作携带握手 token（app/main.py local_api_guard），
                # token 由服务启动时写入数据目录的 agent_token.txt。
                attempt.setdefault("x-afan-token", token)
            response = self._send(method, f"{self.base_url}{path}", attempt, kwargs)
            # 403 且还有候选 token 时，多半是撞上了安装版/源码运行的残留 token，换下一个重试。
            if not (needs_token and response.status_code == 403 and index + 1 < len(candidates)):
                break
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


def _key_value(value: str) -> tuple[str, str]:
    """解析 --set key=value；不做类型转换，校验交给服务端。"""
    key, separator, raw = value.partition("=")
    key = key.strip()
    if not separator or not key:
        raise argparse.ArgumentTypeError(f"--set 需要 key=value 形式，收到：{value}")
    return key, raw.strip()


def _apply_sets(data: dict[str, str], pairs: Iterable[tuple[str, str]] | None) -> dict[str, str]:
    for key, value in pairs or ():
        data[key] = value
    return data


def _add_project_id(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("job_id", help="项目或任务 ID")


def _form_bool(value: bool) -> str:
    return "true" if value else "false"


def _command_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="afan-agent",
        description="通过本机 HTTP 服务调用 afan 口播智能体。默认输出 JSON；AI 客户端建议先执行 guide 与 status <ID>（自带 next_actions）。",
    )
    parser.add_argument(
        "--base-url",
        default=_discover_base_url(),
        help="服务地址，默认依次读取 AFAN_AGENT_URL、启动器记录的 current_url.txt、http://127.0.0.1:8000",
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
    subparsers.add_parser("providers", help="列出改口型引擎及配置状态")
    subparsers.add_parser("guide", help="输出机器可读的工作流契约（步骤、门槛、约束）")
    subparsers.add_parser("schema", help="输出机器可读的命令目录（自动从本 CLI 生成）")

    status = subparsers.add_parser("status", help="查询任务/项目状态，输出含 next_actions")
    _add_project_id(status)
    status.add_argument("--brief", action="store_true", help="只输出关键字段（含 next_actions）")

    wait = subparsers.add_parser("wait", help="轮询任务直到结束；失败时退出码为 1")
    _add_project_id(wait)
    wait.add_argument("--interval", type=_positive_timeout, default=3.0, help="轮询间隔秒数（默认 3）")
    wait.add_argument("--timeout", type=_positive_timeout, default=900.0, help="总超时秒数（默认 900），超时报错退出")

    download = subparsers.add_parser("download", help="下载已生成的成片或中间产物")
    _add_project_id(download)
    download.add_argument("--output", type=Path, required=True, help="保存到本地的目标路径")
    download.add_argument(
        "--artifact",
        choices=("final", "voice-preview", "lipsync-video"),
        help="final=剪辑成片，lipsync-video=改口型原片，voice-preview=试听音频；缺省下载改口型原片（兼容旧行为）",
    )

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

    rewrite = subparsers.add_parser("rewrite", help="用 AI 改写项目现有文案（异步）")
    _add_project_id(rewrite)
    rewrite.add_argument("--instruction", required=True, help="改写要求")
    rewrite.add_argument("--model", help="可选的改写模型 ID")

    save_text = subparsers.add_parser("save-rewritten", help="保存用户确认后的口播文案")
    _add_project_id(save_text)
    save_text.add_argument("--text", required=True, help="改写后的文案")

    transcript = subparsers.add_parser("transcript", help="直接写入项目原文案")
    _add_project_id(transcript)
    transcript.add_argument("--text", required=True, help="完整文案（不可为空）")

    person = subparsers.add_parser("upload-person-video", help="给已有项目上传人物视频")
    _add_project_id(person)
    person.add_argument("--video", type=_existing_file, required=True)
    person.add_argument("--consent", action="store_true", help="确认拥有肖像使用授权")

    preview = subparsers.add_parser("voice-preview", help="生成配音试听（异步，用 status/wait 轮询）")
    _add_project_id(preview)
    preview.add_argument("--mode", choices=("upload", "saved", "direct"), default="upload", help="声音来源：上传样音/已存音色/云端标准音色")
    preview.add_argument("--sample", type=_existing_file, help="mode=upload 时的声音样音文件")
    preview.add_argument("--voice-id", help="mode=saved 时的音色 ID")
    preview.add_argument("--consent", action="store_true", help="确认拥有声音使用授权（direct 模式不需要）")
    preview.add_argument("--speed", choices=("slow", "standard", "fast"), help="语速")
    preview.add_argument("--emotion", choices=("natural", "warm", "steady"), help="情感")
    preview.add_argument("--reference-text", help="参考文本（≤12000 字）")
    preview.add_argument("--model", help="TTS 模型 ID（direct 模式映射 direct_tts_model，其余映射 voice_clone_model）")
    preview.add_argument(
        "--set",
        action="append",
        type=_key_value,
        default=None,
        metavar="KEY=VALUE",
        help="透传其余服务端表单参数（CosyVoice/Fish/MiniMax 等），可重复",
    )

    confirm = subparsers.add_parser("voice-confirm", help="确认试听，解锁改口型生成")
    _add_project_id(confirm)

    generate = subparsers.add_parser("generate-video", help="启动改口型视频生成")
    _add_project_id(generate)
    generate.add_argument("--strategy", choices=("keep_video", "trim_tail"), default="keep_video")
    generate.add_argument("--lipsync-model", help="可选的改口型模型 ID")

    cancel = subparsers.add_parser("cancel-video", help="请求取消正在生成的视频")
    _add_project_id(cancel)

    _add_edit_commands(subparsers)

    draft = subparsers.add_parser("draft", help="创建空项目（用于粘贴文案/素材库流程）")
    draft.add_argument("--name", help="项目名（≤80 字符）")

    rename = subparsers.add_parser("rename", help="重命名项目")
    _add_project_id(rename)
    rename.add_argument("--name", required=True, help="新项目名（≤80 字符）")

    for name, help_text in (
        ("save", "把项目持久化到磁盘（服务重启后不丢）"),
        ("forget", "从列表移除项目但保留文件"),
        ("delete", "删除项目及其工作目录（不可恢复）"),
    ):
        sub = subparsers.add_parser(name, help=help_text)
        _add_project_id(sub)

    library = subparsers.add_parser("library", help="列出素材库")
    library.add_argument("--kind", choices=("voice", "person", "script", "video"), help="按类型过滤")

    library_upload = subparsers.add_parser("library-upload", help="上传素材到素材库")
    library_upload.add_argument("--kind", choices=("voice", "person", "script", "video"), required=True)
    library_upload.add_argument("--file", type=_existing_file, help="素材文件（script 以外必填）")
    library_upload.add_argument("--text", help="script 类型必填的文案内容（≤20000 字）")
    library_upload.add_argument("--name", help="素材显示名")

    library_use = subparsers.add_parser("library-use", help="把素材库资产用于项目")
    _add_project_id(library_use)
    library_use.add_argument("asset_id", help="素材库资产 ID")
    library_use.add_argument("--authorized", action="store_true", help="确认拥有该素材的授权（voice/person/video 必填）")
    library_use.add_argument("--target", choices=("broll",), help="kind=video 时可指定用作 B-roll")

    library_remove = subparsers.add_parser("library-remove", help="从素材库删除资产")
    library_remove.add_argument("asset_id", help="素材库资产 ID")

    return parser


def _add_edit_commands(subparsers: argparse.ArgumentParser) -> None:
    """edit / auto-edit 共用同一组剪辑样式参数。"""
    for name, help_text in (
        ("edit", "保存剪辑样式并后台渲染成片"),
        ("auto-edit", "AI 补全未锁定的剪辑决定并导出成片"),
    ):
        sub = subparsers.add_parser(name, help=help_text)
        _add_project_id(sub)
        sub.add_argument("--title", help="标题文案")
        sub.add_argument("--sticker", help="贴纸文案")
        sub.add_argument("--subtitle-style", dest="subtitle_style", choices=("inline-cyan", "classic-yellow", "highlight-red", "soft-white"))
        sub.add_argument("--subtitle-enabled", action=argparse.BooleanOptionalAction, default=None, help="是否烧录字幕（--no-subtitle-enabled 关闭）")
        sub.add_argument("--subtitle-font-size", type=int, dest="subtitle_font_size")
        sub.add_argument("--subtitle-color", dest="subtitle_color")
        sub.add_argument("--subtitle-margin-v", type=int, dest="subtitle_margin_v")
        sub.add_argument("--subtitle-keywords", dest="subtitle_keywords", help="关键词高亮，逗号分隔")
        sub.add_argument("--subtitle-keyword-color", dest="subtitle_keyword_color")
        sub.add_argument("--title-font-size", dest="title_font_size", help='如 "h/18"')
        sub.add_argument("--title-color", dest="title_color")
        sub.add_argument("--title-position", choices=("top", "center", "bottom"), dest="title_position")
        sub.add_argument("--cover-text", dest="cover_text")
        sub.add_argument("--cover-style", choices=("diagonal-yellow", "giant-headline", "vertical-cutout", "center-sticker", "dark-bold", "yellow-block", "clean-white"), dest="cover_style")
        sub.add_argument("--music-volume", type=float, dest="music_volume", help="0-1")
        sub.add_argument("--layout-mode", choices=("fullscreen", "pip"), dest="layout_mode")
        sub.add_argument("--layout-background-type", choices=("broll", "image", "color", "blur"), dest="layout_background_type")
        sub.add_argument("--layout-background-name", dest="layout_background_name")
        sub.add_argument("--layout-background-color", dest="layout_background_color", help="6 位十六进制")
        sub.add_argument("--layout-foreground-scale", type=float, dest="layout_foreground_scale", help="0.15-0.95")
        sub.add_argument("--layout-foreground-position", choices=("top-left", "top-right", "bottom-left", "bottom-right", "center"), dest="layout_foreground_position")
        sub.add_argument("--layout-foreground-margin", type=int, dest="layout_foreground_margin", help="0-300")
        sub.add_argument("--layout-blur", type=int, dest="layout_blur", help="2-40")
        sub.add_argument(
            "--set",
            action="append",
            type=_key_value,
            default=None,
            metavar="KEY=VALUE",
            help="透传其余服务端剪辑字段，可重复",
        )
        if name == "auto-edit":
            sub.add_argument("--locked", default="", help="不交给 AI 的字段名（逗号分隔）")


EDIT_FORM_FIELDS = (
    "title", "sticker", "subtitle_style", "subtitle_enabled", "subtitle_font_size", "subtitle_color",
    "subtitle_margin_v", "subtitle_keywords", "subtitle_keyword_color", "title_font_size", "title_color",
    "title_position", "cover_text", "cover_style", "music_volume", "layout_mode", "layout_background_type",
    "layout_background_name", "layout_background_color", "layout_foreground_scale", "layout_foreground_position",
    "layout_foreground_margin", "layout_blur",
)


def _multipart_file(path: str) -> tuple[str, Any, str]:
    file_path = Path(path)
    content_type = "video/mp4" if file_path.suffix.lower() in {".mp4", ".mov", ".m4v", ".webm", ".avi", ".mkv"} else "application/octet-stream"
    return (file_path.name, file_path.open("rb"), content_type)


def _close_files(files: Iterable[tuple[str, Any, str]]) -> None:
    for _name, handle, _content_type in files:
        handle.close()


# ── next_actions：根据任务字段推断下一步（纯函数，与 app/workflow_contract.py 的门槛一致） ──

BRIEF_FIELDS = (
    "id", "source_name", "title", "status", "stage", "progress", "error", "current_step",
    "script_confirmed", "preview_confirmed", "person_status", "person_duration", "preview_duration",
    "duration_delta", "duration_status", "output_name", "edit_output_name", "music_name", "cover_name",
    "saved", "created_at", "updated_at",
)


def compute_next_actions(job: dict[str, Any]) -> list[dict[str, str]]:
    """返回建议的下一批 CLI 命令；只依赖 GET /api/jobs/{id} 返回的字段。"""
    job_id = str(job.get("id") or "<项目ID>")
    status = job.get("status")
    if status == "running":
        return [{"command": f"wait {job_id}", "reason": f"任务执行中：{job.get('stage') or ''}（{job.get('progress') or 0}%）。"}]
    if status == "failed":
        reason = str(job.get("error") or "未知错误")
        return [{"command": f"status {job_id}", "reason": f"上次任务失败：{reason}。排查后重试失败步骤。"}]

    has_text = bool(str(job.get("rewritten_text") or "").strip())
    has_source = bool(str(job.get("transcript") or "").strip())
    if not has_text and not has_source:
        return [
            {"command": "create-script --prompt <文案需求>", "reason": "项目还没有文案：可让 AI 写稿；"},
            {"command": "extract-reference --video <参考视频> --authorized", "reason": "或从参考视频提取；"},
            {"command": f"transcript {job_id} --text <文案>", "reason": "或直接粘贴已有文案。"},
        ]
    if not has_text:
        return [
            {"command": f"rewrite {job_id} --instruction <改写要求>", "reason": "已有原文案，可 AI 改写；"},
            {"command": f"save-rewritten {job_id} --text <最终文案>", "reason": "或人工确认后直接保存最终文案。"},
        ]
    if not job.get("person_duration"):
        return [{"command": f"upload-person-video {job_id} --video <人物视频> --consent", "reason": "文案已就绪，等待人物视频（需肖像授权）。"}]
    if not job.get("preview_confirmed"):
        if not job.get("preview_audio_name"):
            voice_id = str(job.get("voice_id") or "").strip()
            if voice_id:
                command = f"voice-preview {job_id} --mode saved --voice-id {voice_id}"
            else:
                command = f"voice-preview {job_id} --mode upload --sample <声音样音> --consent"
            return [{"command": command, "reason": "人物视频就绪，先生成配音试听（异步，提交后用 wait 轮询）。"}]
        return [{"command": f"voice-confirm {job_id}", "reason": "试听已生成，确认后才能启动改口型生成。"}]

    if not job.get("output_name"):
        person = float(job.get("person_duration") or 0)
        preview = float(job.get("preview_duration") or 0)
        if person and preview and preview > person + max(0.8, person * 0.05):
            return [{"command": f"voice-preview {job_id} --mode upload --sample <声音样音> --consent", "reason": "配音明显长于人物视频，超出容差会被拒绝；请缩短文案或调快语速后重新试听。"}]
        return [{"command": f"generate-video {job_id}", "reason": "文案/人物/配音确认均已就绪，可启动改口型生成（异步）。"}]

    actions: list[dict[str, str]] = []
    if not job.get("edit_output_name"):
        actions.append({"command": f"auto-edit {job_id}", "reason": "改口型完成，可一键智能剪辑；"})
        actions.append({"command": f"edit {job_id} --title <标题> --subtitle-style classic-yellow", "reason": "或用 edit 手工指定剪辑样式。"})
    artifact = "final" if job.get("edit_output_name") else "lipsync-video"
    actions.append({"command": f"download {job_id} --output <保存路径> --artifact {artifact}", "reason": "下载成片。"})
    return actions


def _with_next_actions(job: dict[str, Any]) -> dict[str, Any]:
    result = dict(job)
    result["next_actions"] = compute_next_actions(job)
    return result


def _brief_status(job: dict[str, Any]) -> dict[str, Any]:
    brief = {key: job.get(key) for key in BRIEF_FIELDS if key in job}
    brief["next_actions"] = compute_next_actions(job)
    return brief


def _builtin_workflow() -> dict[str, Any]:
    """旧版本服务没有 /api/workflow 时，读取仓库内同一份契约作为兜底。"""
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from app.workflow_contract import WORKFLOW

    return WORKFLOW


def _command_catalog(parser: argparse.ArgumentParser) -> list[dict[str, Any]]:
    """从 argparse 自动导出命令目录，保证 schema 与实现不漂移。"""
    choices = parser._subparsers._group_actions[0].choices  # noqa: SLF001 - argparse 无公开接口
    commands: list[dict[str, Any]] = []
    for name in choices:
        sub = choices[name]
        arguments = []
        for action in sub._actions:  # noqa: SLF001
            if action.dest in {"help"}:
                continue
            positional = not action.option_strings
            entry: dict[str, Any] = {
                "name": action.dest if positional else action.option_strings[0],
                "required": bool(action.required),
                "positional": positional,
            }
            if action.help:
                entry["help"] = action.help
            if action.choices:
                entry["choices"] = list(action.choices)
            if action.nargs == 0 and not positional:
                entry["flag"] = True
            if isinstance(action, argparse._AppendAction):
                entry["repeatable"] = True
            arguments.append(entry)
        commands.append({"name": name, "help": sub.description or "", "args": arguments})
    return commands


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
    if command == "providers":
        return client.json("GET", "/api/digital-human/providers")
    if command == "guide":
        try:
            payload = client.json("GET", "/api/workflow")
        except CliError:
            return {"ok": True, "source": "builtin", "workflow": _builtin_workflow()}
        return {"ok": True, "source": "server", "workflow": payload.get("workflow")}
    if command == "schema":
        return {"ok": True, "cli": "afan-agent", "commands": _command_catalog(_command_parser()), "doc": "docs/agent-api.md"}
    if command == "status":
        job = client.json("GET", f"/api/jobs/{args.job_id}")
        return _brief_status(job) if args.brief else _with_next_actions(job)
    if command == "wait":
        return _wait(args, client)
    if command == "download":
        args.output.parent.mkdir(parents=True, exist_ok=True)
        if args.artifact == "final":
            path = f"/api/projects/{args.job_id}/download"
        elif args.artifact:
            path = f"/api/projects/{args.job_id}/download/{args.artifact}"
        else:
            path = f"/api/jobs/{args.job_id}/download"
        response = client.request("GET", path)
        args.output.write_bytes(response.content)
        return {"ok": True, "job_id": args.job_id, "artifact": args.artifact, "path": str(args.output.resolve()), "bytes": len(response.content)}
    if command == "create-script":
        data = {"prompt": args.prompt}
        if args.model:
            data["script_model"] = args.model
        return client.json("POST", "/api/projects/ai-script", data=data)
    if command == "save-rewritten":
        return client.json("POST", f"/api/projects/{args.job_id}/rewritten", data={"rewritten_text": args.text})
    if command == "rewrite":
        data = {"instruction": args.instruction}
        if args.model:
            data["rewrite_model"] = args.model
        return client.json("POST", f"/api/projects/{args.job_id}/rewrite", data=data)
    if command == "transcript":
        return client.json("POST", f"/api/projects/{args.job_id}/transcript", data={"transcript": args.text})
    if command == "generate-video":
        data = {"strategy": args.strategy}
        if args.lipsync_model:
            data["lipsync_model"] = args.lipsync_model
        return client.json("POST", f"/api/projects/{args.job_id}/generate-video", data=data)
    if command == "voice-confirm":
        return client.json("POST", f"/api/projects/{args.job_id}/voice-confirm")
    if command == "cancel-video":
        return client.json("POST", f"/api/projects/{args.job_id}/cancel-video")
    if command == "draft":
        data = {"source_name": args.name} if args.name else {}
        return client.json("POST", "/api/projects/draft", data=data)
    if command == "rename":
        return client.json("POST", f"/api/projects/{args.job_id}/rename", data={"name": args.name})
    if command in {"save", "forget", "delete"}:
        return client.json("POST", f"/api/projects/{args.job_id}/{command}")
    if command == "library":
        query = f"?kind={args.kind}" if args.kind else ""
        return client.json("GET", f"/api/library{query}")
    if command == "library-upload":
        data: dict[str, str] = {"kind": args.kind}
        if args.name:
            data["name"] = args.name
        files = None
        if args.kind == "script":
            if not args.text:
                raise CliError("kind=script 时必须提供 --text。")
            data["text"] = args.text
        else:
            if not args.file:
                raise CliError(f"kind={args.kind} 时必须提供 --file。")
            files = [_multipart_file(args.file)]
        try:
            return client.json("POST", "/api/library/upload", data=data, files={"file": files[0]} if files else None)
        finally:
            if files:
                _close_files(files)
    if command == "library-use":
        data = {"authorized": _form_bool(args.authorized)}
        if args.target:
            data["target"] = args.target
        return client.json("POST", f"/api/projects/{args.job_id}/library/{args.asset_id}/use", data=data)
    if command == "library-remove":
        return client.json("DELETE", f"/api/library/{args.asset_id}")
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
    if command == "voice-preview":
        return _voice_preview(args, client)
    if command in {"edit", "auto-edit"}:
        return _edit(args, client)
    raise CliError(f"未知命令：{command}")


def _voice_preview(args: argparse.Namespace, client: ApiClient) -> Any:
    data: dict[str, str] = {
        "mode": args.mode,
        "voice_consent": _form_bool(args.consent),
    }
    if args.voice_id:
        data["voice_id"] = args.voice_id
    if args.speed:
        data["speed"] = args.speed
    if args.emotion:
        data["emotion"] = args.emotion
    if args.reference_text:
        data["reference_text"] = args.reference_text
    if args.model:
        data["direct_tts_model" if args.mode == "direct" else "voice_clone_model"] = args.model
    _apply_sets(data, args.set)
    files = [_multipart_file(args.sample)] if args.sample else None
    try:
        return client.json("POST", f"/api/projects/{args.job_id}/voice-preview", data=data, files={"sample": files[0]} if files else None)
    finally:
        if files:
            _close_files(files)


def _edit(args: argparse.Namespace, client: ApiClient) -> Any:
    data: dict[str, str] = {}
    for field in EDIT_FORM_FIELDS:
        value = getattr(args, field)
        if value is None:
            continue
        data[field] = str(value).lower() if isinstance(value, bool) else str(value)
    _apply_sets(data, args.set)
    if args.command == "auto-edit":
        endpoint = f"/api/projects/{args.job_id}/auto-edit"
        return client.json("POST", endpoint, data={**data, "locked": args.locked or ""})
    return client.json("POST", f"/api/projects/{args.job_id}/edit", data=data)


def _wait(args: argparse.Namespace, client: ApiClient) -> dict[str, Any]:
    deadline = time.monotonic() + args.timeout
    while True:
        job = client.json("GET", f"/api/jobs/{args.job_id}")
        if job.get("status") in TERMINAL_STATUSES:
            return _with_next_actions(job)
        if time.monotonic() >= deadline:
            raise CliError(
                f"等待超时（{args.timeout:g} 秒）：任务仍在 {job.get('status')}/{job.get('stage')}。可加大 --timeout 后继续 wait。"
            )
        time.sleep(args.interval)


def main(argv: list[str] | None = None) -> int:
    parser = _command_parser()
    args = parser.parse_args(argv)
    try:
        result = execute(args, ApiClient(args.base_url, args.timeout))
    except CliError as error:
        print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    if args.command == "wait" and isinstance(result, dict) and result.get("status") == "failed":
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through the CLI smoke test
    raise SystemExit(main())
