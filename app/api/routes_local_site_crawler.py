import asyncio
import json
import traceback

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from app.models.schemas import SearchCrawlRequest, SearchCrawlResponse
from app.services.crawl_job_manager import crawl_job_manager
from app.services.local_site_crawler_service import crawl_local_site_notes

router = APIRouter(prefix="/local-crawl", tags=["Local Crawl"])


async def _run_crawl_job(job_id: str, request: SearchCrawlRequest) -> None:
    """Background task: run the crawl, push progress, persist the result."""
    job = crawl_job_manager.get(job_id)
    if job is None:
        return
    job.status = "running"
    crawl_job_manager.update_progress(
        job,
        {
            "collected": 0,
            "target": request.target_count,
            "current_keyword": request.keywords[0] if request.keywords else "",
        },
    )

    async def on_progress(progress: dict) -> None:
        crawl_job_manager.update_progress(
            job,
            {
                "collected": progress.get("collected", 0),
                "target": progress.get("target", request.target_count),
                "current_keyword": progress.get("keyword", ""),
            },
        )

    try:
        result = await crawl_local_site_notes(request, on_progress=on_progress)
        crawl_job_manager.save_result(job, result)
    except Exception as e:  # noqa: BLE001
        print("\n===== LOCAL CRAWL JOB ERROR =====")
        traceback.print_exc()
        print("=============================\n")
        crawl_job_manager.mark_failed(job, str(e) or "Local crawl failed")


@router.post("/jobs")
async def create_crawl_job(request: SearchCrawlRequest) -> dict:
    """Create a background crawl job; poll GET /jobs/{id} or stream /jobs/{id}/events."""
    job = crawl_job_manager.create(request)
    asyncio.create_task(_run_crawl_job(job.job_id, request))
    return {"job_id": job.job_id, "status": job.status}


@router.get("/jobs/{job_id}")
async def get_crawl_job(job_id: str) -> dict:
    job = crawl_job_manager.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="crawl job not found")
    return job.to_dict()


@router.get("/jobs/{job_id}/events")
async def crawl_job_events(job_id: str):
    job = crawl_job_manager.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="crawl job not found")

    if job.status in ("done", "failed"):
        async def replayed():
            event = {
                "kind": "done" if job.status == "done" else "failed",
                "result": job.result,
                "error": job.error,
            }
            yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

        return StreamingResponse(replayed(), media_type="text/event-stream")

    queue = crawl_job_manager.subscribe(job)

    async def event_stream():
        try:
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15.0)
                    yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                    if event.get("kind") in ("done", "failed"):
                        break
                except asyncio.TimeoutError:
                    yield ": heartbeat\n\n"
        finally:
            crawl_job_manager.unsubscribe(job, queue)

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@router.post("/search", response_model=SearchCrawlResponse)
async def search_and_crawl_notes(request: SearchCrawlRequest):
    """Synchronous compatibility entry (deprecated in favor of POST /local-crawl/jobs)."""
    try:
        return await crawl_local_site_notes(request)
    except Exception as e:  # noqa: BLE001
        print("\n===== LOCAL CRAWL ERROR =====")
        traceback.print_exc()
        print("=============================\n")
        raise HTTPException(status_code=500, detail=str(e) or "Local crawl failed")
