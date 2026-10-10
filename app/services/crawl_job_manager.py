"""Crawl job manager (optimization plan 12.6).

Turns the long synchronous crawl into a background job with pollable status,
progress (SSE), and results persisted to disk so a client timeout never loses
the result.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from app.models.schemas import SearchCrawlRequest, SearchCrawlResponse

logger = logging.getLogger("crawl_jobs")

JOBS_DIR = Path("data") / "raw" / "crawl_jobs"


@dataclass
class CrawlJob:
    job_id: str
    status: str = "pending"  # pending | running | done | failed
    progress: dict = field(default_factory=dict)  # {collected, target, current_keyword}
    result: Optional[dict] = None  # SearchCrawlResponse.model_dump()
    error: Optional[str] = None
    created_at: float = field(default_factory=time.time)
    finished_at: Optional[float] = None
    _subscribers: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "job_id": self.job_id,
            "status": self.status,
            "progress": self.progress,
            "result": self.result,
            "error": self.error,
            "created_at": self.created_at,
            "finished_at": self.finished_at,
        }


class CrawlJobManager:
    """In-memory job table + file persistence under data/raw/crawl_jobs/."""

    def __init__(self, jobs_dir: Path = JOBS_DIR) -> None:
        self.jobs_dir = jobs_dir
        self._jobs: dict[str, CrawlJob] = {}

    def create(self, request: SearchCrawlRequest) -> CrawlJob:
        job = CrawlJob(job_id=uuid.uuid4().hex[:12])
        job.status = "pending"
        job.progress = {
            "collected": 0,
            "target": request.target_count,
            "current_keyword": request.keywords[0] if request.keywords else "",
        }
        self._jobs[job.job_id] = job
        self._persist(job)
        return job

    def get(self, job_id: str) -> Optional[CrawlJob]:
        job = self._jobs.get(job_id)
        if job is None:
            self._load(job_id)
            job = self._jobs.get(job_id)
        return job

    def update_progress(self, job: CrawlJob, progress: dict) -> None:
        job.progress = progress
        self._publish(job, {"kind": "progress", "progress": progress})
        self._persist(job)

    def save_result(self, job: CrawlJob, result: SearchCrawlResponse) -> None:
        job.status = "done"
        job.result = result.model_dump()
        job.finished_at = time.time()
        self._publish(job, {"kind": "done", "result": job.result})
        self._persist(job)

    def mark_failed(self, job: CrawlJob, error: str) -> None:
        job.status = "failed"
        job.error = error
        job.finished_at = time.time()
        self._publish(job, {"kind": "failed", "error": error})
        self._persist(job)

    def subscribe(self, job: CrawlJob) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue()
        job._subscribers.append(queue)
        return queue

    def unsubscribe(self, job: CrawlJob, queue: asyncio.Queue) -> None:
        try:
            job._subscribers.remove(queue)
        except ValueError:
            pass

    def _publish(self, job: CrawlJob, event: dict) -> None:
        for queue in list(job._subscribers):
            queue.put_nowait(event)

    def _path(self, job_id: str) -> Path:
        return self.jobs_dir / f"{job_id}.json"

    def _persist(self, job: CrawlJob) -> None:
        try:
            self.jobs_dir.mkdir(parents=True, exist_ok=True)
            self._path(job.job_id).write_text(
                json.dumps(job.to_dict(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("failed to persist crawl job %s: %s", job.job_id, exc)

    def _load(self, job_id: str) -> None:
        try:
            path = self._path(job_id)
            if not path.exists():
                return
            data = json.loads(path.read_text(encoding="utf-8"))
            job = CrawlJob(job_id=data["job_id"])
            job.status = data.get("status", "pending")
            job.progress = data.get("progress", {})
            job.result = data.get("result")
            job.error = data.get("error")
            job.created_at = data.get("created_at", time.time())
            job.finished_at = data.get("finished_at")
            self._jobs[job_id] = job
        except Exception as exc:  # noqa: BLE001
            logger.warning("failed to load crawl job %s: %s", job_id, exc)


crawl_job_manager = CrawlJobManager()
