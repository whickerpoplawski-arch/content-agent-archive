import asyncio
import json
from typing import Optional

from fastapi import APIRouter, Header, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.core.config import settings
from app.models.schemas import AgentRunRequest, AgentRunResponse, AutonomousAgentRequest
from app.services.agent_service import run_agent_pipeline

router = APIRouter(prefix="/agent", tags=["Agent"])


def _require_admin(x_admin_token: Optional[str]) -> None:
    """Enforce an optional X-Admin-Token on management endpoints (plan 10.3)."""
    token = settings.admin_token
    if token and x_admin_token != token:
        raise HTTPException(status_code=403, detail="Forbidden: invalid admin token")


class ConsoleRunRequest(BaseModel):
    goal: str = Field(..., min_length=1)
    audience: str = ""
    tone: str = ""
    backend: str = Field("openai", pattern="^(openai|mock)$")
    enabled_tools: list[str] | None = None


class ApprovalRequest(BaseModel):
    approval_id: str
    approved: bool


class FeedbackRequest(BaseModel):
    content_id: str = Field(..., min_length=1, description="产物标识(标题)")
    liked: bool = False
    score: int | None = Field(None, ge=0, le=10, description="0-10 采纳评分")
    note: str = ""


class KnowledgeIngestRequest(BaseModel):
    text: str = Field(..., min_length=1, description="文档正文")
    title: str = ""
    source: str = Field("manual", pattern="^(manual|crawl|analysis|output)$")
    metadata: dict = Field(default_factory=dict)


class KnowledgeQueryRequest(BaseModel):
    q: str = Field(..., min_length=1)
    top_k: int = Field(5, ge=1, le=20)
    source: str | None = None


@router.post("/run", response_model=AgentRunResponse)
async def run_agent_endpoint(request: AgentRunRequest):
    """Legacy deterministic pipeline (kept for backward compatibility)."""
    try:
        return await run_agent_pipeline(request)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/autonomous")
async def run_autonomous_endpoint(request: AutonomousAgentRequest):
    """Run the real Agent: goal -> plan -> execute -> reflect -> trace.

    backend: "openai" uses the configured model; "mock" runs offline (demo/tests).
    """
    from app.agent.orchestrator import XHSAgent
    from app.agent.llm import get_backend
    from app.agent.mock_factories import build_mock_backend, build_mock_registry

    try:
        if request.backend == "mock":
            agent = XHSAgent(backend=build_mock_backend(), registry=build_mock_registry())
        else:
            agent = XHSAgent(backend=get_backend("openai"))
        result = await agent.run(
            request.goal,
            audience=request.audience,
            tone=request.tone,
        )
        return {
            "run_id": result.run_id,
            "goal": result.goal,
            "plan": [s.model_dump() for s in result.plan.steps],
            "outputs": result.outputs,
            "tool_calls": result.tool_calls,
            "trace_path": result.trace_path,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/runs", status_code=status.HTTP_202_ACCEPTED)
async def create_console_run(request: ConsoleRunRequest):
    from app.agent.run_manager import run_manager

    try:
        return run_manager.create(
            goal=request.goal.strip(),
            audience=request.audience,
            tone=request.tone,
            backend=request.backend,
            enabled_tools=request.enabled_tools,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/runs")
async def list_console_runs(limit: int = Query(50, ge=1, le=50)):
    from app.agent.run_manager import run_manager

    return {"items": run_manager.list(limit)}


@router.get("/runs/{run_id}")
async def get_console_run(run_id: str):
    from app.agent.run_manager import run_manager

    try:
        return run_manager.get(run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Run not found") from exc


@router.get("/runs/{run_id}/events")
async def stream_console_run_events(
    run_id: str,
    last_event_id: str | None = Header(None, alias="Last-Event-ID"),
):
    from app.agent.run_manager import TERMINAL_STATES, run_manager

    try:
        cursor = int(last_event_id or 0)
        run_manager.get(run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Run not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid Last-Event-ID") from exc

    async def generate():
        nonlocal cursor
        while True:
            events = run_manager.events_after(run_id, cursor)
            for event in events:
                cursor = event["id"]
                payload = json.dumps(event, ensure_ascii=False)
                yield f"id: {event['id']}\nevent: {event['type']}\ndata: {payload}\n\n"
            run = run_manager.get(run_id)
            if run["status"] in TERMINAL_STATES and not run_manager.events_after(run_id, cursor):
                break
            yield ": heartbeat\n\n"
            await asyncio.sleep(0.2)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/runs/{run_id}/stop")
async def stop_console_run(run_id: str, x_admin_token: Optional[str] = Header(None)):
    _require_admin(x_admin_token)
    from app.agent.run_manager import run_manager

    try:
        return run_manager.stop(run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Run not found") from exc


@router.post("/runs/{run_id}/confirm")
async def confirm_console_run(run_id: str, request: ApprovalRequest):
    from app.agent.run_manager import run_manager

    try:
        return run_manager.confirm(run_id, request.approval_id, request.approved)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Run not found") from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/runs/{run_id}/feedback")
async def feedback_console_run(run_id: str, request: FeedbackRequest):
    """阶段1 反馈回路: 用户对产物的采纳/评分写入长期记忆(profile.user_preferences)。

    写入后下一次生成时 _profile_text 会把偏好摘要注入 planner,让生成偏向用户
    高评分/已采纳的风格与主题。
    """
    from datetime import datetime, timezone

    from app.agent.audit import audit
    from app.agent.memory import LongTermMemory
    from app.agent.orchestrator import MAX_PREFERENCES, _goal_signature
    from app.agent.run_manager import run_manager

    try:
        run = run_manager.get(run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Run not found") from exc
    memory = LongTermMemory()
    prefs = list(memory.get("user_preferences") or [])
    prefs.append(
        {
            "content_id": request.content_id,
            "goal_signature": _goal_signature(run["goal"]),
            "liked": request.liked,
            "score": request.score,
            "note": request.note,
            "ts": datetime.now(timezone.utc).isoformat(),
        }
    )
    memory.set("user_preferences", prefs[-MAX_PREFERENCES:])
    audit(
        "feedback",
        run_id=run_id,
        detail=(
            f"content_id={request.content_id} liked={request.liked} "
            f"score={request.score}"
        ),
    )
    return {"ok": True, "stored": len(prefs[-MAX_PREFERENCES:])}


@router.post("/runs/{run_id}/resume")
async def resume_console_run(run_id: str, x_admin_token: Optional[str] = Header(None)):
    _require_admin(x_admin_token)
    """Resume a stopped/failed run from its last checkpoint (optimization P0)."""
    from app.agent.run_manager import run_manager

    try:
        return run_manager.resume(run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Run not found") from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

class HousekeepingRequest(BaseModel):
    retention_days: int = Field(30, ge=1, le=365)
    keep_summary: bool = True


@router.post("/housekeeping")
async def housekeeping_endpoint(request: HousekeepingRequest, x_admin_token: Optional[str] = Header(None)):
    _require_admin(x_admin_token)
    """Purge run/checkpoint artifacts older than the retention window (plan 10.1)."""
    from app.agent.audit import audit
    from app.agent.housekeeping import purge_old_runs

    audit("housekeeping", detail=f"retention={request.retention_days}")
    return purge_old_runs(
        retention_days=request.retention_days,
        keep_summary=request.keep_summary,
    )

class ScheduleRequest(BaseModel):
    name: str = ""
    goal: str = Field(..., min_length=1)
    audience: str = ""
    tone: str = ""
    backend: str = Field("openai", pattern="^(openai|mock)$")
    enabled_tools: list[str] | None = None
    interval_seconds: int | None = Field(None, ge=1, le=86400)
    run_at: str | None = None


@router.post("/schedule")
async def create_schedule(request: ScheduleRequest, x_admin_token: Optional[str] = Header(None)):
    """Create a recurring Agent run schedule (optimization plan P2-a)."""
    from app.agent.scheduler import scheduler

    _require_admin(x_admin_token)
    try:
        return scheduler.create(
            name=request.name,
            goal=request.goal,
            audience=request.audience,
            tone=request.tone,
            backend=request.backend,
            enabled_tools=request.enabled_tools,
            interval_seconds=request.interval_seconds,
            run_at=request.run_at,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/schedule")
async def list_schedules(x_admin_token: Optional[str] = Header(None)):
    from app.agent.scheduler import scheduler

    _require_admin(x_admin_token)
    return {"items": scheduler.list()}


@router.delete("/schedule/{schedule_id}")
async def delete_schedule(schedule_id: str, x_admin_token: Optional[str] = Header(None)):
    from app.agent.scheduler import scheduler

    _require_admin(x_admin_token)
    try:
        return scheduler.delete(schedule_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Schedule not found") from exc


@router.post("/knowledge")
async def knowledge_ingest(request: KnowledgeIngestRequest):
    """手动喂文档入 RAG 知识库 (docs/memory-rag-selection.md R3)。"""
    from app.agent.knowledge import get_knowledge_store

    try:
        doc_id = get_knowledge_store().add(
            request.text,
            title=request.title,
            source=request.source,
            metadata=request.metadata,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"ok": True, "doc_id": doc_id, "total": get_knowledge_store().count()}


@router.get("/knowledge/search")
async def knowledge_search(q: str, top_k: int = 5, source: Optional[str] = None):
    """知识库混合检索 (向量 KNN + BM25 -> RRF), 供问答/人工查询。"""
    from app.agent.knowledge import get_knowledge_store

    hits = get_knowledge_store().search(q, top_k=max(1, min(top_k, 20)), source=source)
    return {"hits": hits, "count": len(hits)}


@router.get("/knowledge")
async def knowledge_stats():
    from app.agent.knowledge import get_knowledge_store

    store = get_knowledge_store()
    return {"documents": store.count()}