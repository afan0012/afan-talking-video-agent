"""Small HTTP adapter for a local MuseTalk 1.5 checkout.

Run this file from the MuseTalk repository on a GPU machine.  It deliberately
keeps the model runtime separate from the controller application: the
controller uploads one video/audio pair, polls a task, and downloads the MP4.
The server binds to localhost by default so it is suitable for an SSH tunnel.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse


ROOT = Path(os.getenv("MUSETALK_ROOT", Path.cwd())).resolve()
JOBS_ROOT = Path(os.getenv("MUSETALK_JOBS_ROOT", str(ROOT / "runtime-jobs"))).resolve()
_bundled_ffmpeg = ROOT / ("ffmpeg.exe" if os.name == "nt" else "ffmpeg")
FFMPEG_PATH = os.getenv("FFMPEG_PATH", shutil.which("ffmpeg") or str(_bundled_ffmpeg))

_raw_timeout = os.getenv("MUSETALK_TIMEOUT", "").strip()
try:
    MAX_SECONDS = float(_raw_timeout) if _raw_timeout else 0.0
except ValueError:
    MAX_SECONDS = 0.0  # 0 或留空 = 不限制；本地推理耗时会随视频长度和首载波动
JOBS_ROOT.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="MuseTalk 1.5 adapter", version="1.0")
executor = ThreadPoolExecutor(max_workers=1)
jobs: dict[str, dict[str, Any]] = {}


def _safe_suffix(filename: str | None, fallback: str) -> str:
    suffix = Path(filename or "").suffix.lower()
    return suffix if suffix in {".mp4", ".mov", ".mkv", ".avi", ".wav", ".mp3", ".m4a"} else fallback


def _find_result(result_dir: Path) -> Path | None:
    files = [p for p in result_dir.rglob("*") if p.is_file() and p.suffix.lower() in {".mp4", ".mov", ".webm"}]
    return max(files, key=lambda p: p.stat().st_mtime, default=None)


def _run_job(job_id: str, video: Path, audio: Path, result_dir: Path) -> None:
    job = jobs[job_id]
    job["status"] = "running"
    job["started_at"] = time.time()
    config = result_dir / "inference.json"
    config.write_text(
        json.dumps({"task-1": {"video_path": str(video), "audio_path": str(audio), "bbox_shift": 0}}, ensure_ascii=False),
        encoding="utf-8",
    )
    # Avoid shell parsing.  Explicit paths are important on Windows clients
    # that upload files containing spaces or non-ASCII characters.
    command = [
        sys.executable, "-m", "scripts.inference",
        "--inference_config", str(config),
        "--result_dir", str(result_dir),
        "--version", "v15",
        "--unet_config", str(ROOT / "models/musetalkV15/musetalk.json"),
        "--unet_model_path", str(ROOT / "models/musetalkV15/unet.pth"),
        "--vae_type", "sd-vae",
        "--whisper_dir", str(ROOT / "models/whisper"),
        "--ffmpeg_path", FFMPEG_PATH,
        "--use_float16",
    ]
    env = os.environ.copy()
    env["FFMPEG_PATH"] = FFMPEG_PATH
    env["PYTHONUNBUFFERED"] = "1"
    # Windows 中文环境下子进程/原生库可能输出 GBK 字节；errors="replace"
    # 保证日志读取不会因解码失败中断推理监听。
    env.setdefault("PYTHONIOENCODING", "utf-8:replace")
    process = None
    try:
        process = subprocess.Popen(
            command, cwd=str(ROOT), env=env, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, bufsize=1,
            encoding="utf-8", errors="replace",
        )
        job["pid"] = process.pid
        deadline = time.monotonic() + MAX_SECONDS if MAX_SECONDS > 0 else None
        if process.stdout:
            for line in process.stdout:
                job["log"] = line.strip()[-500:]
                match = re.search(r"(\d+)%", line)
                if match:
                    job["progress"] = min(98, max(1, int(match.group(1))))
                if deadline is not None and time.monotonic() > deadline:
                    process.kill()
                    raise TimeoutError(f"推理超过 {MAX_SECONDS} 秒")
        return_code = process.wait(timeout=10)
        if return_code != 0:
            raise RuntimeError(f"MuseTalk 退出码 {return_code}：{job.get('log', '')}")
        result = _find_result(result_dir)
        if result is None or result.stat().st_size == 0:
            raise RuntimeError("推理完成但没有找到输出 MP4")
        job.update(status="succeeded", progress=100, result=str(result), finished_at=time.time())
    except Exception as exc:
        if process is not None and process.poll() is None:
            try:
                process.kill()
            except OSError:
                pass
        job.update(status="failed", progress=0, error=str(exc), finished_at=time.time())


@app.get("/health")
def health() -> dict[str, Any]:
    return {
        "ok": True,
        "model": "MuseTalk 1.5",
        "root": str(ROOT),
        "weights": (ROOT / "models/musetalkV15/unet.pth").is_file(),
        "cuda_visible": bool(os.getenv("CUDA_VISIBLE_DEVICES", "0")),
    }


@app.post("/v1/lipsync")
async def submit_lipsync(video: UploadFile = File(...), audio: UploadFile = File(...)) -> dict[str, Any]:
    job_id = uuid.uuid4().hex
    job_dir = JOBS_ROOT / job_id
    result_dir = job_dir / "result"
    result_dir.mkdir(parents=True, exist_ok=True)
    video_path = job_dir / f"input{_safe_suffix(video.filename, '.mp4')}"
    audio_path = job_dir / f"input{_safe_suffix(audio.filename, '.wav')}"
    try:
        video_path.write_bytes(await video.read())
        audio_path.write_bytes(await audio.read())
    except OSError as exc:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise HTTPException(400, f"保存上传素材失败：{exc}") from exc
    jobs[job_id] = {"id": job_id, "status": "queued", "progress": 0, "created_at": time.time()}
    executor.submit(_run_job, job_id, video_path, audio_path, result_dir)
    return {"task_id": job_id, "status": "queued"}


@app.get("/v1/lipsync/{job_id}")
def query_lipsync(job_id: str) -> dict[str, Any]:
    job = jobs.get(job_id)
    if job is None:
        raise HTTPException(404, "任务不存在或服务已重启")
    response = {k: v for k, v in job.items() if k != "result"}
    if job.get("status") == "succeeded":
        response["result_url"] = f"/v1/lipsync/{job_id}/result"
    return response


@app.get("/v1/lipsync/{job_id}/result")
def get_result(job_id: str) -> FileResponse:
    job = jobs.get(job_id)
    result = Path(job["result"]) if job and job.get("result") else None
    if not result or not result.is_file():
        raise HTTPException(404, "任务尚未完成或结果不存在")
    return FileResponse(result, media_type="video/mp4", filename="musetalk-result.mp4")


@app.delete("/v1/lipsync/{job_id}")
def cancel_lipsync(job_id: str) -> dict[str, Any]:
    job = jobs.get(job_id)
    if job is None:
        raise HTTPException(404, "任务不存在")
    job["status"] = "cancelled"
    return {"task_id": job_id, "status": "cancelled"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=os.getenv("MUSETALK_HOST", "127.0.0.1"), port=int(os.getenv("MUSETALK_PORT", "8001")))
