"""Local digital-human provider adapters.

The application deliberately keeps the model runtimes outside the repository.
MuseTalk and HeyGem are therefore integrated as small adapters: MuseTalk is
started with a user-supplied command template, while HeyGem is called through
its local HTTP API.  This keeps the workflow stable without bundling model
weights or assuming one particular installation layout.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

import httpx


CancelCheck = Callable[[], bool] | None
ProgressCallback = Callable[[int], None] | None


class DigitalHumanError(RuntimeError):
    """A user-facing error raised by a digital-human provider."""


@dataclass(frozen=True)
class ProviderInfo:
    id: str
    label: str
    kind: str
    configured: bool
    note: str


class DigitalHumanProvider(Protocol):
    """Stable contract implemented by every lip-sync backend."""

    id: str
    label: str

    def info(self) -> ProviderInfo: ...

    def generate(
        self,
        video: Path,
        audio: Path,
        output: Path,
        *,
        cancelled: CancelCheck = None,
        progress: ProgressCallback = None,
    ) -> Path: ...


ProviderFactory = Callable[[Mapping[str, str] | None, str], DigitalHumanProvider]


class DigitalHumanRegistry:
    """Registry for lip-sync providers.

    The workflow only depends on this registry contract; provider-specific
    startup and HTTP/command details stay inside each adapter class.  A new
    backend can therefore be registered without editing the workflow itself.
    """

    def __init__(self) -> None:
        self._factories: dict[str, ProviderFactory] = {}

    def register(self, provider_id: str, factory: ProviderFactory) -> None:
        key = str(provider_id).strip()
        if not key:
            raise ValueError("数字人模型标识不能为空。")
        if key in self._factories:
            raise ValueError(f"数字人模型已注册：{key}")
        self._factories[key] = factory

    def create(self, model: str, settings: Mapping[str, str] | None = None, *, ffmpeg: str = "ffmpeg") -> DigitalHumanProvider:
        try:
            return self._factories[model](settings, ffmpeg)
        except KeyError as exc:
            raise DigitalHumanError(f"未知的本地数字人模型：{model}") from exc

    def infos(self, settings: Mapping[str, str] | None = None) -> list[ProviderInfo]:
        return [factory(settings, "ffmpeg").info() for factory in self._factories.values()]


def _setting(settings: Mapping[str, str] | None, name: str, default: str = "") -> str:
    if settings is not None:
        value = settings.get(name)
        if value is not None and str(value).strip():
            return str(value).strip()
    return os.getenv(name, default).strip()


def _check_cancel(cancelled: CancelCheck) -> None:
    if cancelled and cancelled():
        raise DigitalHumanError("改口型任务已取消。")


def _report(progress: ProgressCallback, value: int) -> None:
    if progress:
        progress(max(0, min(99, int(value))))


def _replace_tokens(value: str, *, video: Path, audio: Path, output: Path, config: Path, result_dir: Path) -> str:
    return value.format(
        video=str(video),
        audio=str(audio),
        output=str(output),
        output_dir=str(output.parent),
        output_name=output.name,
        config=str(config),
        result_dir=str(result_dir),
    )


def _command_tokens(command: str) -> list[str]:
    """Parse a command template without invoking a shell.

    JSON arrays are recommended on Windows because they preserve paths with
    spaces.  A normal command line remains supported for convenience.
    """
    raw = command.strip()
    if not raw:
        return []
    if raw.startswith("["):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise DigitalHumanError("MuseTalk 命令不是有效的 JSON 数组。") from exc
        if not isinstance(parsed, list) or not all(isinstance(item, str) for item in parsed):
            raise DigitalHumanError("MuseTalk JSON 命令必须是字符串数组。")
        return [item for item in parsed if item.strip()]
    try:
        return shlex.split(raw, posix=False)
    except ValueError as exc:
        raise DigitalHumanError(f"MuseTalk 命令解析失败：{exc}") from exc


def _find_output(directory: Path, output: Path) -> Path | None:
    candidates = sorted(
        (item for item in directory.rglob("*") if item.is_file() and item.suffix.lower() in {".mp4", ".mov", ".webm"}),
        key=lambda item: item.stat().st_mtime,
        reverse=True,
    )
    for candidate in candidates:
        if candidate.resolve() != output.resolve():
            return candidate
    return None


def _timeout_seconds(raw: str | None) -> float | None:
    """解析超时秒数；0、留空或非法值返回 None（不限制）。

    本地模型推理耗时波动很大（首次加载权重、长视频逐帧处理），
    保守的超时会误杀正常任务，所以这里默认不限制；用户仍可
    显式填一个正数秒数来启用看门狗。
    """
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        value = float(text)
    except ValueError:
        return None
    return value if value > 0 else None


class MuseTalkProvider:
    id = "musetalk-1.5"
    label = "MuseTalk 1.5（本地）"

    def __init__(self, settings: Mapping[str, str] | None = None, *, ffmpeg: str = "ffmpeg") -> None:
        self.settings = settings
        self.ffmpeg = ffmpeg

    @property
    def remote_url(self) -> str:
        return _setting(self.settings, "MUSETALK_BASE_URL").rstrip("/")

    def info(self) -> ProviderInfo:
        command = _setting(self.settings, "MUSETALK_COMMAND")
        if self.remote_url:
            return ProviderInfo(self.id, self.label, "http", True, f"调用远程 MuseTalk 1.5 服务（{self.remote_url}）")
        return ProviderInfo(self.id, self.label, "command", bool(command), "通过命令模板调用本机 MuseTalk 1.5 推理环境")

    @staticmethod
    def _json(response: httpx.Response, prefix: str) -> dict[str, Any]:
        try:
            body = response.json()
        except ValueError as exc:
            raise DigitalHumanError(f"{prefix}：服务返回了无法解析的响应（HTTP {response.status_code}）。") from exc
        if not isinstance(body, dict):
            raise DigitalHumanError(f"{prefix}：服务返回格式异常。")
        return body

    def _generate_remote(
        self,
        video: Path,
        audio: Path,
        output: Path,
        *,
        cancelled: CancelCheck = None,
        progress: ProgressCallback = None,
    ) -> Path:
        """Submit files to the optional FastAPI adapter on a GPU host."""
        timeout = _timeout_seconds(_setting(self.settings, "MUSETALK_TIMEOUT"))
        _report(progress, 20)
        try:
            # 本机/局域网模型服务不走系统代理：回环流量被代理进程转发时，
            # 连接复用会破坏上传后的后续请求目标，导致 404。
            with httpx.Client(timeout=300, follow_redirects=True, trust_env=False) as client:
                with video.open("rb") as video_file, audio.open("rb") as audio_file:
                    response = client.post(
                        f"{self.remote_url}/v1/lipsync",
                        files={
                            "video": (video.name, video_file, "video/mp4"),
                            "audio": (audio.name, audio_file, "audio/wav"),
                        },
                    )
                body = self._json(response, "MuseTalk 远程提交失败")
                if not response.is_success:
                    message = body.get("error") or body.get("detail") or f"HTTP {response.status_code}"
                    raise DigitalHumanError(f"MuseTalk 远程提交失败：{message}")
                task_id = body.get("task_id")
                if not isinstance(task_id, str) or not task_id:
                    raise DigitalHumanError("MuseTalk 远程服务没有返回 task_id。")
                deadline = time.monotonic() + timeout if timeout is not None else None
                while True:
                    _check_cancel(cancelled)
                    if deadline is not None and time.monotonic() >= deadline:
                        break
                    query = client.get(f"{self.remote_url}/v1/lipsync/{task_id}")
                    query_body = self._json(query, "MuseTalk 远程状态查询失败")
                    if not query.is_success:
                        message = query_body.get("error") or query_body.get("detail") or f"HTTP {query.status_code}"
                        raise DigitalHumanError(f"MuseTalk 远程状态查询失败：{message}")
                    status = str(query_body.get("status") or "").lower()
                    _report(progress, int(query_body.get("progress") or 35))
                    if status == "succeeded":
                        result_response = client.get(f"{self.remote_url}/v1/lipsync/{task_id}/result")
                        result_response.raise_for_status()
                        output.parent.mkdir(parents=True, exist_ok=True)
                        output.write_bytes(result_response.content)
                        if not output.exists() or output.stat().st_size == 0:
                            raise DigitalHumanError("MuseTalk 远程服务返回了空视频。")
                        _report(progress, 99)
                        return output
                    if status in {"failed", "cancelled", "canceled"}:
                        raise DigitalHumanError(f"MuseTalk 远程推理失败：{query_body.get('error') or '服务未提供详细原因'}")
                    time.sleep(2)
        except DigitalHumanError:
            raise
        except (httpx.HTTPError, OSError) as exc:
            raise DigitalHumanError(f"MuseTalk 远程服务连接失败：{exc}") from exc
        raise DigitalHumanError(f"MuseTalk 远程推理超时（{timeout:g} 秒）。" if timeout is not None else "MuseTalk 远程推理未完成。")

    def generate(
        self,
        video: Path,
        audio: Path,
        output: Path,
        *,
        cancelled: CancelCheck = None,
        progress: ProgressCallback = None,
    ) -> Path:
        if self.remote_url:
            if not video.exists() or not audio.exists():
                raise DigitalHumanError("MuseTalk 输入视频或音频不存在。")
            return self._generate_remote(video, audio, output, cancelled=cancelled, progress=progress)
        command_template = _setting(self.settings, "MUSETALK_COMMAND")
        if not command_template:
            raise DigitalHumanError(
                "未配置 MuseTalk 1.5。请在设置中填写 MUSETALK_COMMAND（支持 {video}、{audio}、{output}、{output_name}、{config}、{result_dir}）。"
            )
        if not video.exists() or not audio.exists():
            raise DigitalHumanError("MuseTalk 输入视频或音频不存在。")
        workdir_value = _setting(self.settings, "MUSETALK_WORKDIR")
        workdir = Path(workdir_value) if workdir_value else output.parent
        if not workdir.exists():
            raise DigitalHumanError(f"MuseTalk 工作目录不存在：{workdir}")
        timeout = _timeout_seconds(_setting(self.settings, "MUSETALK_TIMEOUT"))
        output.parent.mkdir(parents=True, exist_ok=True)
        result_dir = output.parent / "musetalk-results"
        result_dir.mkdir(parents=True, exist_ok=True)
        # MuseTalk's official entry point consumes a mapping of task IDs to
        # task settings. JSON is valid YAML and avoids adding a PyYAML
        # dependency to this controller.  Do not wrap this in a synthetic
        # ``task`` object: the official runner iterates the top-level keys.
        config_path = output.parent / "musetalk-inference.json"
        config_path.write_text(
            json.dumps({"task-1": {"video_path": str(video), "audio_path": str(audio), "result_name": str(output)}}, ensure_ascii=False),
            encoding="utf-8",
        )
        tokens = [
            _replace_tokens(token, video=video, audio=audio, output=output, config=config_path, result_dir=result_dir)
            for token in _command_tokens(command_template)
        ]
        if not tokens:
            raise DigitalHumanError("MuseTalk 命令不能为空。")
        _report(progress, 24)
        _check_cancel(cancelled)
        try:
            # Do not pipe stdout: inference logs can be very large and would
            # block a background task when the pipe buffer fills.  The adapter
            # reports the exit code and points users to the model's own logs.
            child_env = os.environ.copy()
            ffmpeg_parent = Path(self.ffmpeg).expanduser().parent
            if ffmpeg_parent.exists():
                child_env["PATH"] = str(ffmpeg_parent) + os.pathsep + child_env.get("PATH", "")
            process = subprocess.Popen(
                tokens, cwd=str(workdir), env=child_env,
                stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT,
            )
        except OSError as exc:
            raise DigitalHumanError(f"MuseTalk 启动失败：{exc}") from exc
        started = time.monotonic()
        try:
            while True:
                _check_cancel(cancelled)
                if process.poll() is not None:
                    break
                if timeout is not None and time.monotonic() - started > timeout:
                    process.kill()
                    raise DigitalHumanError(f"MuseTalk 超时（{timeout:g} 秒）。")
                elapsed = time.monotonic() - started
                _report(progress, min(95, 35 + int(elapsed / max(timeout or 600, 1) * 55)))
                time.sleep(0.2)
        except DigitalHumanError:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)
            raise
        if process.returncode != 0:
            raise DigitalHumanError(f"MuseTalk 推理失败（退出码 {process.returncode}）；请查看 MuseTalk 运行日志。")
        if not output.exists() or output.stat().st_size == 0:
            discovered = _find_output(output.parent, output)
            if discovered:
                shutil.copy2(discovered, output)
        if not output.exists() or output.stat().st_size == 0:
            raise DigitalHumanError("MuseTalk 已结束但没有找到输出视频；请检查命令中的 {output} 参数。")
        _report(progress, 99)
        return output


class HeyGemProvider:
    id = "heygem"
    label = "HeyGem（本地服务）"

    def __init__(self, settings: Mapping[str, str] | None = None) -> None:
        self.settings = settings

    @property
    def base_url(self) -> str:
        return _setting(self.settings, "HEYGEM_BASE_URL", "http://127.0.0.1:8383").rstrip("/")

    def info(self) -> ProviderInfo:
        configured = bool(_setting(self.settings, "HEYGEM_BASE_URL"))
        return ProviderInfo(self.id, self.label, "http", configured, "调用本机 HeyGem /easy/submit 与 /easy/query 接口")

    def _media_ref(self, path: Path) -> str:
        # A shared path is needed when HeyGem runs in Docker.  Without it we
        # send a file URI, which is useful for a native HeyGem installation.
        media_root = _setting(self.settings, "HEYGEM_MEDIA_ROOT")
        if media_root:
            # HeyGem's container path is POSIX even when the controller runs
            # on Windows; normalize separators before sending the JSON body.
            return str(Path(media_root) / path.name).replace("\\", "/")
        return path.resolve().as_uri()

    def _stage_media(self, path: Path) -> Path:
        """Copy inputs into an optional host directory shared with Docker."""
        host_root = _setting(self.settings, "HEYGEM_HOST_MEDIA_ROOT")
        if not host_root:
            return path
        target_root = Path(host_root)
        try:
            target_root.mkdir(parents=True, exist_ok=True)
            target = target_root / path.name
            shutil.copy2(path, target)
            return target
        except OSError as exc:
            raise DigitalHumanError(f"无法准备 HeyGem 共享素材：{exc}") from exc

    def _submission_payload(self, *, video: Path, audio: Path, task_code: str) -> dict[str, Any]:
        """Build the documented HeyGem ``/easy/submit`` payload.

        The caller chooses ``code`` and must use that same value for the
        documented ``/easy/query?code=...`` polling call.  Some community
        builds return a separate status code in their JSON response, so using
        a loosely parsed response field here can accidentally poll ``0`` or
        ``null`` instead of the submitted task ID.
        """
        return {
            "audio_url": self._media_ref(audio),
            "video_url": self._media_ref(video),
            "code": task_code,
            "chaofen": 0,
            "watermark_switch": 0,
            "pn": 1,
        }

    @staticmethod
    def _json(response: httpx.Response, prefix: str) -> dict[str, Any]:
        try:
            body = response.json()
        except ValueError as exc:
            raise DigitalHumanError(f"{prefix}：服务返回了无法解析的响应（HTTP {response.status_code}）。") from exc
        if not isinstance(body, dict):
            raise DigitalHumanError(f"{prefix}：服务返回格式异常。")
        return body

    @staticmethod
    def _pick(body: Any, keys: tuple[str, ...]) -> Any:
        if isinstance(body, dict):
            for key in keys:
                if body.get(key) not in (None, ""):
                    return body[key]
            for value in body.values():
                found = HeyGemProvider._pick(value, keys)
                if found not in (None, ""):
                    return found
        elif isinstance(body, list):
            for value in body:
                found = HeyGemProvider._pick(value, keys)
                if found not in (None, ""):
                    return found
        return None

    def _download_or_copy(self, value: Any, output: Path) -> None:
        if not isinstance(value, str) or not value.strip():
            raise DigitalHumanError("HeyGem 已完成但未返回输出视频。")
        candidate = Path(value)
        if candidate.exists():
            shutil.copy2(candidate, output)
            return
        url = value if value.startswith(("http://", "https://")) else None
        if not url:
            raise DigitalHumanError("HeyGem 返回的输出路径在本机不存在。")
        try:
            with httpx.stream("GET", url, timeout=300, follow_redirects=True) as response:
                response.raise_for_status()
                with output.open("wb") as target:
                    for chunk in response.iter_bytes():
                        target.write(chunk)
        except (httpx.HTTPError, OSError) as exc:
            raise DigitalHumanError(f"HeyGem 输出视频下载失败：{exc}") from exc

    def generate(
        self,
        video: Path,
        audio: Path,
        output: Path,
        *,
        cancelled: CancelCheck = None,
        progress: ProgressCallback = None,
    ) -> Path:
        if not video.exists() or not audio.exists():
            raise DigitalHumanError("HeyGem 输入视频或音频不存在。")
        staged_video = self._stage_media(video)
        staged_audio = self._stage_media(audio)
        timeout = _timeout_seconds(_setting(self.settings, "HEYGEM_TIMEOUT"))
        task_code = output.stem
        payload = self._submission_payload(video=staged_video, audio=staged_audio, task_code=task_code)
        _report(progress, 24)
        try:
            with httpx.Client(timeout=60, follow_redirects=True, trust_env=False) as client:
                response = client.post(f"{self.base_url}/easy/submit", json=payload)
                body = self._json(response, "HeyGem 提交失败")
                if not response.is_success:
                    message = self._pick(body, ("message", "msg", "error")) or f"HTTP {response.status_code}"
                    raise DigitalHumanError(f"HeyGem 提交失败：{message}")
                deadline = time.monotonic() + timeout if timeout is not None else None
                started = time.monotonic()
                while True:
                    _check_cancel(cancelled)
                    if deadline is not None and time.monotonic() >= deadline:
                        break
                    query = client.get(f"{self.base_url}/easy/query", params={"code": task_code})
                    query_body = self._json(query, "HeyGem 状态查询失败")
                    if not query.is_success:
                        message = self._pick(query_body, ("message", "msg", "error")) or f"HTTP {query.status_code}"
                        raise DigitalHumanError(f"HeyGem 状态查询失败：{message}")
                    status = str(self._pick(query_body, ("status", "task_status", "state")) or "").upper()
                    result = self._pick(query_body, ("video_url", "output", "result", "result_url", "url"))
                    if status in {"SUCCEEDED", "SUCCESS", "DONE", "COMPLETED", "2"} or result:
                        self._download_or_copy(result, output)
                        if not output.exists() or output.stat().st_size == 0:
                            raise DigitalHumanError("HeyGem 返回了空的视频文件。")
                        _report(progress, 99)
                        return output
                    if status in {"FAILED", "ERROR", "CANCELED", "CANCELLED", "3", "-1"}:
                        message = self._pick(query_body, ("message", "msg", "error")) or "服务未提供详细原因"
                        raise DigitalHumanError(f"HeyGem 生成失败：{message}")
                    elapsed = time.monotonic() - started
                    _report(progress, min(95, 35 + int(elapsed / max(timeout or 600, 1) * 60)))
                    time.sleep(2)
        except DigitalHumanError:
            raise
        except (httpx.HTTPError, OSError) as exc:
            raise DigitalHumanError(f"HeyGem 服务连接失败：{exc}") from exc
        raise DigitalHumanError(f"HeyGem 超时（{timeout:g} 秒）。" if timeout is not None else "HeyGem 任务未完成。")


DIGITAL_HUMAN_REGISTRY = DigitalHumanRegistry()
DIGITAL_HUMAN_REGISTRY.register(
    MuseTalkProvider.id,
    lambda settings, ffmpeg: MuseTalkProvider(settings, ffmpeg=ffmpeg),
)
DIGITAL_HUMAN_REGISTRY.register(
    HeyGemProvider.id,
    lambda settings, _ffmpeg: HeyGemProvider(settings),
)


def provider_infos(settings: Mapping[str, str] | None = None) -> list[ProviderInfo]:
    return DIGITAL_HUMAN_REGISTRY.infos(settings)


def provider_for(model: str, settings: Mapping[str, str] | None = None, *, ffmpeg: str = "ffmpeg") -> DigitalHumanProvider:
    return DIGITAL_HUMAN_REGISTRY.create(model, settings, ffmpeg=ffmpeg)
