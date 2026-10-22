import sys
import asyncio
import threading
from contextlib import asynccontextmanager

if sys.platform.startswith("win"):
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

from pathlib import Path
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from app.core.config import settings
from app.api.routes_health import router as health_router
from app.api.routes_analysis import router as analysis_router
from app.api.routes_topics import router as topics_router
from app.api.routes_content import router as content_router
from app.api.routes_agent import router as agent_router
from app.api.routes_local_site_crawler import router as local_crawl_router
from app.api.routes_publish import router as publish_router
from app.api.routes_feishu import router as feishu_router
from app.api.routes_xhs_service import router as xhs_service_router

@asynccontextmanager
async def lifespan(_app):
    """Start the recurring scheduler loop for the app's lifetime (plan P2-a)."""
    from app.agent.scheduler import scheduler
    from app.agent.run_manager import run_manager
    from app.api.routes_health import code_fingerprint

    stop_event = threading.Event()

    def _submit(schedule):
        run_manager.create(
            goal=schedule["goal"],
            audience=schedule["audience"],
            tone=schedule["tone"],
            backend=schedule["backend"],
            enabled_tools=schedule["enabled_tools"],
        )

    task = asyncio.create_task(scheduler.run_loop(_submit, stop_event))
    # 13.3: log boot version + code fingerprint so a reload=False long-running
    # process can be cross-checked against /health after edits.
    print(f"[boot] version={settings.app_version} code_fingerprint={code_fingerprint()}")
    print("[boot] reload=False 长跑:改代码后须重启;用 GET /health/version 核对指纹是否过期")
    try:
        yield
    finally:
        stop_event.set()
        task.cancel()


app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    debug=settings.debug,
    description="AI-powered content mining and generation system for social media.",
    lifespan=lifespan,
)

app.include_router(health_router)
app.include_router(analysis_router)
app.include_router(topics_router)
app.include_router(content_router)
app.include_router(agent_router)
app.include_router(local_crawl_router)
app.include_router(publish_router)
app.include_router(feishu_router)
app.include_router(xhs_service_router)

Path("data/output/images").mkdir(parents=True, exist_ok=True)
app.mount("/images", StaticFiles(directory="data/output/images"), name="images")
app.mount("/static", StaticFiles(directory="static"), name="static")

from fastapi.responses import FileResponse, RedirectResponse

_TOPIC_HELPER_PAGE = Path(__file__).resolve().parent.parent / "static" / "topic_helper.html"


@app.get("/", tags=["Root"])
def root():
    return RedirectResponse(url="/static/index.html")


@app.get("/demo/topic-helper", tags=["Demo"])
def topic_helper_demo_page():
    """Offline topic-helper page. Start with `python run.py`, then open this path."""
    return FileResponse(_TOPIC_HELPER_PAGE)