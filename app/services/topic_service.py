import json
import re
from pathlib import Path

from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import StrOutputParser
from langchain_openai import ChatOpenAI

from app.core.config import settings
from app.models.schemas import (
    TopicGenerateRequest,
    TopicGenerateResponse,
    TopicItem,
)


def load_prompt_template() -> str:
    prompt_path = Path("app/prompts/topic_generation_prompt.txt")
    with open(prompt_path, "r", encoding="utf-8") as f:
        return f.read()


def topic_prompt_inputs(request: TopicGenerateRequest) -> dict:
    """Variables shared by the live topic chain and the bare M0 path."""
    return {
        "count": request.count,
        "audience": request.audience,
        "summary": request.summary,
        "top_keywords": ", ".join(request.top_keywords),
        "top_tags": ", ".join(request.top_tags),
        "title_patterns": ", ".join(request.title_patterns),
        "insight_points": "\n".join(f"- {point}" for point in request.insight_points),
        "focus_keywords": ", ".join(request.focus_keywords) or "（无）",
        "knowledge_hints": "\n".join(f"- {h}" for h in request.knowledge_hints) or "（无）",
    }


def render_topic_prompt(request: TopicGenerateRequest) -> str:
    """Fill ``topic_generation_prompt.txt``. Does not call a model."""
    prompt = ChatPromptTemplate.from_template(load_prompt_template())
    messages = prompt.format_messages(**topic_prompt_inputs(request))
    parts: list[str] = []
    for message in messages:
        content = message.content
        if isinstance(content, str):
            parts.append(content)
        else:
            parts.append(str(content))
    return "\n".join(parts)


def build_llm() -> ChatOpenAI:
    llm_kwargs = {
        "model": settings.openai_model,
        "temperature": settings.openai_temperature,
        "api_key": settings.openai_api_key,
    }

    if settings.openai_base_url:
        llm_kwargs["base_url"] = settings.openai_base_url

    return ChatOpenAI(**llm_kwargs)


def parse_topics_payload(result_text: str) -> TopicGenerateResponse:
    """Parse the shared topic prompt's JSON, including fenced model output."""
    raw = (result_text or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        start = raw.find("{")
        end = raw.rfind("}")
        if start < 0 or end <= start:
            raise
        parsed = json.loads(raw[start : end + 1])
    if not isinstance(parsed, dict):
        raise ValueError("LLM JSON was not an object")
    topics = [TopicItem(**item) for item in parsed.get("topics") or []]
    return TopicGenerateResponse(topics=topics)


def generate_topics(request: TopicGenerateRequest) -> TopicGenerateResponse:
    template = load_prompt_template()

    prompt = ChatPromptTemplate.from_template(template)
    llm = build_llm()
    parser = StrOutputParser()

    chain = prompt | llm | parser

    result_text = chain.invoke(topic_prompt_inputs(request))

    try:
        return parse_topics_payload(result_text)
    except Exception as e:
        raise ValueError(f"Failed to parse LLM output as JSON: {e}\nRaw output:\n{result_text}")