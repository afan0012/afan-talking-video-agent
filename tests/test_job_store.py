import json

from app.domain import Job
from app.job_store import JobStore


def _job(job_id: str = "job-1", **changes) -> Job:
    values = {
        "id": job_id,
        "source_name": "source.mp4",
        "instruction": "demo",
        "create_voice": False,
        "voice_id": None,
    }
    values.update(changes)
    return Job(**values)


def test_legacy_broll_is_migrated_and_running_job_is_interrupted(tmp_path):
    database = tmp_path / "jobs.json"
    database.write_text(
        json.dumps([{
            "id": "old",
            "source_name": "source.mp4",
            "instruction": "demo",
            "create_voice": False,
            "voice_id": None,
            "status": "running",
            "broll_name": "insert.mp4",
            "broll_start": 2,
            "broll_duration": 3,
        }]),
        encoding="utf-8",
    )

    store = JobStore(database)
    job = store.get("old")

    assert job.status == "failed"
    assert job.stage == "服务重启，任务中断"
    assert len(job.broll_clips) == 1
    assert job.broll_clips[0].name == "insert.mp4"
    assert json.loads(database.read_text(encoding="utf-8"))[0]["status"] == "failed"


def test_malformed_records_do_not_prevent_store_startup(tmp_path):
    database = tmp_path / "jobs.json"
    database.write_text(json.dumps([{"id": "broken", "source_name": None}, {"id": "ok", "source_name": "x", "instruction": "y", "create_voice": False, "voice_id": None}]), encoding="utf-8")

    store = JobStore(database)

    assert store.get("ok").source_name == "x"
    assert "broken" not in store.jobs


def test_updates_are_written_only_for_saved_jobs(tmp_path):
    saved_store = JobStore(tmp_path / "saved.json")
    saved = _job()
    saved_store.add(saved)
    saved_store.update(saved, stage="完成", progress=100)
    assert json.loads((tmp_path / "saved.json").read_text(encoding="utf-8"))[0]["stage"] == "完成"

    transient_store = JobStore(tmp_path / "transient.json")
    transient = _job("transient")
    transient_store.jobs[transient.id] = transient
    transient_store.update(transient, stage="不落盘")
    assert not (tmp_path / "transient.json").exists()
