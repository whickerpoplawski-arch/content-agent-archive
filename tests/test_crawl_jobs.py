"""Crawl job manager tests (optimization plan 12.6).

Verifies: create job -> poll status running->done -> result readable from the
persisted file -> SSE/progress events delivered.
"""

from __future__ import annotations

import asyncio
import json
import time

from fastapi.testclient import TestClient

from app.api.routes_local_site_crawler import _run_crawl_job
from app.main import app
from app.models.schemas import SearchCrawlRequest, SearchCrawlResponse
from app.services.crawl_job_manager import CrawlJobManager


def run(coro):
    return asyncio.run(coro)


def test_crawl_job_background_flow(tmp_path, monkeypatch):
    """create -> running/done -> persisted result file -> progress+done events."""
    from app.api import routes_local_site_crawler as routes

    manager = CrawlJobManager(tmp_path / "crawl_jobs")
    monkeypatch.setattr(routes, "crawl_job_manager", manager)

    async def fake_crawl(request, on_progress=None):
        if on_progress:
            await on_progress(
                {"collected": 2, "target": request.target_count, "keyword": request.keywords[0]}
            )
        return SearchCrawlResponse(
            target_count=request.target_count,
            count=2,
            used_keywords=list(request.keywords),
            items=[],
        )

    monkeypatch.setattr(routes, "crawl_local_site_notes", fake_crawl)

    async def scenario():
        request = SearchCrawlRequest(keywords=["测试"], target_count=2)
        job = manager.create(request)
        queue = manager.subscribe(job)  # subscribe before the background task runs
        await _run_crawl_job(job.job_id, request)

        finished = manager.get(job.job_id)
        assert finished.status == "done"
        assert finished.result["count"] == 2
        assert finished.progress["collected"] == 2

        # result persisted to disk and re-loadable
        path = tmp_path / "crawl_jobs" / f"{job.job_id}.json"
        assert path.exists()
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["status"] == "done"
        assert data["result"]["count"] == 2

        # SSE-style events: at least one progress, then done
        kinds = []
        while True:
            event = await asyncio.wait_for(queue.get(), timeout=2.0)
            kinds.append(event["kind"])
            if event["kind"] in ("done", "failed"):
                break
        assert "progress" in kinds
        assert kinds[-1] == "done"

    run(scenario())


def test_crawl_job_http_endpoints(tmp_path, monkeypatch):
    """POST /jobs -> poll GET /jobs/{id} -> status done with persisted result."""
    from app.api import routes_local_site_crawler as routes

    manager = CrawlJobManager(tmp_path / "crawl_jobs")
    monkeypatch.setattr(routes, "crawl_job_manager", manager)

    async def fake_crawl(request, on_progress=None):
        if on_progress:
            await on_progress(
                {"collected": 1, "target": request.target_count, "keyword": request.keywords[0]}
            )
        return SearchCrawlResponse(
            target_count=request.target_count,
            count=1,
            used_keywords=list(request.keywords),
            items=[],
        )

    monkeypatch.setattr(routes, "crawl_local_site_notes", fake_crawl)

    client = TestClient(app)
    resp = client.post(
        "/local-crawl/jobs",
        json={"keywords": ["测试"], "target_count": 1},
    )
    assert resp.status_code == 200
    job_id = resp.json()["job_id"]

    status = None
    for _ in range(50):
        data = client.get(f"/local-crawl/jobs/{job_id}").json()
        status = data["status"]
        if status in ("done", "failed"):
            break
        time.sleep(0.05)
    assert status == "done"
    assert data["result"]["count"] == 1

    # persisted file readable
    path = tmp_path / "crawl_jobs" / f"{job_id}.json"
    assert path.exists()

    # 404 for unknown job
    assert client.get("/local-crawl/jobs/nope").status_code == 404
