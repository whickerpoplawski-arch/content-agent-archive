import json
import re
from pathlib import Path
from typing import Any

from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import PydanticOutputParser
from langchain_openai import ChatOpenAI

from app.core.config import settings
from app.models.schemas import (
    ContentGenerateRequest,
    ContentGenerateResponse,
)


def load_prompt_template() -> str:
    prompt_path = Path("app/prompts/content_generation_prompt.txt")
    with open(prompt_path, "r", encoding="utf-8") as f:
        return f.read()


def build_llm() -> ChatOpenAI:
    llm_kwargs = {
        "model": settings.openai_model,
        "temperature": settings.openai_temperature,
        "api_key": settings.openai_api_key,
    }

    if settings.openai_base_url:
        llm_kwargs["base_url"] = settings.openai_base_url

    return ChatOpenAI(**llm_kwargs)


def generate_contents(request: ContentGenerateRequest) -> ContentGenerateResponse:
    template = load_prompt_template()

    parser = PydanticOutputParser(pydantic_object=ContentGenerateResponse)

    prompt = ChatPromptTemplate.from_template(template)
    llm = build_llm()

    # Real LLMs occasionally wrap multiple drafts inside a single "title" field
    # ({"contents": [{"title": [DRAFT1, DRAFT2]}]}) or emit {} / null / stray
    # prose. Instead of letting PydanticOutputParser reject the whole call, we
    # grab the raw text and normalize before validation (real-run 2026-08-12).
    chain = prompt | llm
    text = chain.invoke(
        {
            "count": request.count,
            "topic": request.topic,
            "reason": request.reason,
            "audience": request.audience,
            "tone": request.tone,
            "focus_keywords": ", ".join(request.focus_keywords) or "（无）",
            "knowledge_hints": chr(10).join(f"- {h}" for h in request.knowledge_hints) or "（无）",  # chr(10)=换行, 规避转义歧义
            "format_instructions": parser.get_format_instructions(),
        }
    ).content

    return _parse_contents(text)


def _parse_contents(text: str) -> ContentGenerateResponse:
    """Validate an LLM completion as ContentGenerateResponse, normalizing the
    common "drafts nested under title" shape before failing."""
    data = _extract_json(text)
    if data is None:
        raise ValueError(f"Failed to parse JSON from completion {text[:400]!r}")

    # {"contents": [{"title": [DRAFT1, DRAFT2]}]} -> flatten nested drafts.
    # Each DRAFT is itself a full ContentItem (title/body/hashtags/...); the
    # wrapper only carries the stray "title" key, so spread the draft itself.
    contents = data.get("contents") if isinstance(data, dict) else None
    if isinstance(contents, list):
        flat: list[dict] = []
        for item in contents:
            if isinstance(item, dict) and isinstance(item.get("title"), list):
                for draft in item["title"]:
                    if isinstance(draft, dict):
                        flat.append({**draft, "title": draft.get("title", "")})
            else:
                flat.append(item)
        data["contents"] = flat

    return ContentGenerateResponse.model_validate(data)


def _extract_json(text: str) -> Any:
    """Best-effort JSON extraction from an LLM completion: a raw object, an
    object wrapped in fenced code blocks, or an object inside surrounding prose."""
    if not text:
        return None
    text = text.strip()
    # Strip markdown code fences.
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```\s*$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Fall back to the first {...} block.
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            return None
    return None