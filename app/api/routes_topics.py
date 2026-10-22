from typing import Literal, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.models.schemas import TopicGenerateRequest, TopicGenerateResponse
from app.services.topic_helper import run_topic_helper
from app.services.topic_service import generate_topics

router = APIRouter(prefix="/topics", tags=["Topics"])


class TopicHelperRequest(BaseModel):
    theme: str = ""
    audience: Optional[str] = None
    count: int = Field(4, description="角度数，服务内夹紧到 3–5")
    mode: Literal["mock", "live"] = "mock"
    allow_tools: Optional[list[str]] = None


@router.post("/generate", response_model=TopicGenerateResponse)
def generate_topic_endpoint(request: TopicGenerateRequest):
    try:
        return generate_topics(request)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/helper")
async def topic_helper_endpoint(body: TopicHelperRequest):
    """Theme → angles for the demo page. Default mode is mock.

    Refuse / clarify stay in the JSON body (HTTP 200) so the page can render
    them. Crawl and publish are not part of this route.
    """
    return await run_topic_helper(
        body.theme,
        audience=body.audience,
        count=body.count,
        allow_tools=body.allow_tools,
        mode=body.mode,
    )