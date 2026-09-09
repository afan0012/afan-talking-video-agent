"""Durable in-process job store.

The store owns persistence and the small amount of backwards migration needed
for old jobs.  HTTP handlers can keep working with ``Job`` instances without
knowing where the JSON file lives or how legacy B-roll fields are upgraded.
"""

from __future__ import annotations

import threading
from dataclasses import asdict
from pathlib import Path
from typing import Any

from fastapi import HTTPException

from app.domain import BrollClip, Job, now
from app.storage import read_json, write_json_atomic


class JobStore:
    """Thread-safe job collection backed by one JSON file."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self.lock = threading.RLock()
        self.jobs: dict[str, Job] = {}
        self._saved: set[str] = set()
        self._load()

    def _load(self) -> None:
        interrupted = False
        stored_jobs = read_json(self.db_path, [])
        for raw in stored_jobs if isinstance(stored_jobs, list) else []:
            if not isinstance(raw, dict) or not raw.get("id"):
                continue
            try:
                raw = dict(raw)
                clips = raw.pop("broll_clips", None)
                if clips is None:
                    # 旧数据迁移：单值字段 → 一条 clip（仅当确实上传过素材时）。
                    if raw.get("broll_name"):
                        clips = [{
                            "name": raw.get("broll_name"),
                            "start": float(raw.get("broll_start") or 0),
                            "duration": float(raw.get("broll_duration") or 4),
                            "enabled": bool(raw.get("broll_enabled")),
                        }]
                    else:
                        clips = []
                raw["broll_clips"] = [
                    BrollClip(
                        name=str(clip.get("name") or ""),
                        start=max(0.0, float(clip.get("start") or 0)),
                        duration=max(0.2, float(clip.get("duration") or 4)),
                        enabled=bool(clip.get("enabled", True)),
                        title=str(clip.get("title") or ""),
                        mode=str(clip.get("mode") or "replace"),
                        pip_position=str(clip.get("pip_position") or "bottom-right"),
                        pip_scale=max(0.1, min(0.8, float(clip.get("pip_scale") or 0.32))),
                        pip_margin=max(0, min(300, int(clip.get("pip_margin") or 24))),
                    )
                    for clip in clips
                    if isinstance(clip, dict)
                ]
                job = Job(**raw)
            except (TypeError, ValueError, OverflowError):
                # A malformed historical record must not prevent the app from
                # starting.  It remains in the JSON file for manual recovery.
                continue
            if job.status == "running":
                # 任务创建即落盘后，重启会让「处理中」状态残留；
                # 加载时统一标记为失败，提示用户重试该步骤。
                job.status = "failed"
                job.stage = "服务重启，任务中断"
                job.error = "服务重启导致任务中断，请重试该步骤。"
                interrupted = True
            self.jobs[job.id] = job
            self._saved.add(job.id)
        if interrupted:
            self.save()

    def save(self) -> None:
        """Persist all currently saved jobs to disk."""
        with self.lock:
            saved_jobs = [asdict(self.jobs[job_id]) for job_id in sorted(self._saved) if job_id in self.jobs]
            write_json_atomic(self.db_path, saved_jobs)

    def add(self, job: Job) -> None:
        """Create a new job and persist it immediately."""
        with self.lock:
            self.jobs[job.id] = job
            self._saved.add(job.id)
            self.save()

    def get(self, job_id: str) -> Job:
        try:
            return self.jobs[job_id]
        except KeyError as error:
            raise HTTPException(404, "任务不存在") from error

    def update(self, job: Job, **changes: Any) -> None:
        """Update a job and keep already-saved projects durable."""
        with self.lock:
            for key, value in changes.items():
                setattr(job, key, value)
            job.updated_at = now()
            if job.id in self._saved:
                self.save()

    def is_saved(self, job_id: str) -> bool:
        return job_id in self._saved

    def persist(self, job_id: str) -> None:
        """Mark a job as saved and write it to disk."""
        self.get(job_id)  # validate exists
        with self.lock:
            self._saved.add(job_id)
            self.save()

    def forget(self, job_id: str) -> None:
        """Remove a job from memory entirely."""
        with self.lock:
            self.jobs.pop(job_id, None)
            self._saved.discard(job_id)
            self.save()
