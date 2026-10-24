"""Tool registry and tool wrappers.

The Agent does not call services directly; it selects from a registry of typed
tools (name + description + Pydantic params schema + async handler). Each handler
receives the validated Pydantic params object (never raw dicts), so nested models
such as NoteItem keep their type and can be passed straight into services.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional, Type

from pydantic import BaseModel, Field

from app.agent.schemas import ToolSpec
from app.models.schemas import (
    NoteItem,
    TopicGenerateRequest,
    ContentGenerateRequest,
    SearchCrawlRequest,
)

logger = logging.getLogger("agent.tools")


class LoadNotesParams(BaseModel):
    items: Optional[list[NoteItem]] = Field(
        None, description="Injected notes; if omitted, load sample notes."
    )


class AnalyzeParams(BaseModel):
    items: list[NoteItem]


class TopicsParams(BaseModel):
    summary: str
    top_keywords: list[str] = Field(default_factory=list)
    top_tags: list[str] = Field(default_factory=list)
    title_patterns: list[str] = Field(default_factory=list)
    insight_points: list[str] = Field(default_factory=list)
    audience: str = "大学生女生"
    count: int = 5
    focus_keywords: list[str] = Field(
        default_factory=list,
        description="目标域关键词，选题必须命中至少一个（由 goal 抽取/注入）",
    )
    knowledge_hints: list[str] = Field(
        default_factory=list,
        description="知识库 K 线索（由编排器自动注入），选题尽量扎根其中",
    )


class ContentsParams(BaseModel):
    topic: str
    reason: str = ""
    audience: str = "大学生女生"
    tone: str = "真实分享"
    count: int = 2
    focus_keywords: list[str] = Field(
        default_factory=list,
        description="目标域关键词，正文/标题必须体现",
    )
    knowledge_hints: list[str] = Field(
        default_factory=list,
        description="知识库 K 线索（由编排器自动注入），正文尽量化用其中经验",
    )


class ImagesParams(BaseModel):
    topic_title: str
    topic_reason: str = ""
    content_title: str
    content_body: str = ""
    content_hashtags: list[str] = Field(default_factory=list)
    content_cta: str = ""
    content_image_suggestion: str = ""
    content_type: str = "分享"
    image_count: int = Field(1, ge=1, le=4)


class PublishParams(BaseModel):
    topic_title: str
    topic_reason: str = ""
    content_title: str
    content_body: str
    content_hashtags: list[str] = Field(default_factory=list)
    content_cta: str = ""
    content_image_suggestion: str = ""
    content_type: str = "分享"
    image_count: int = Field(1, ge=1, le=4)
    is_original: bool = True
    visibility: str = "公开可见"
    mode: str = "mcp"


class FeishuParams(BaseModel):
    title: str
    content: str
    images: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    is_original: bool = True
    visibility: str = "公开可见"
    content_type: str = ""


class CrawlParams(BaseModel):
    keywords: list[str]
    topic_words: list[str] = Field(default_factory=list)
    min_comments: int = Field(0, ge=0)
    min_likes: int = Field(0, ge=0)
    min_favorites: int = Field(0, ge=0)
    target_count: int = Field(20, ge=1)
    content_type: str = "图文"


class ReadArtifactParams(BaseModel):
    path: str = Field(..., description="Artifact path returned by an offloaded tool result.")
    fields: list[str] = Field(
        default_factory=list,
        description="Optional object keys to return. Omit to return a truncated view of the whole artifact.",
    )
    limit: int = Field(20, ge=1, le=100, description="Max list items (or preview size) to return.")


class LoadSkillParams(BaseModel):
    name: str = Field(..., description="Skill name from the skill index (directory / frontmatter name).")


class KnowledgeSearchParams(BaseModel):
    query: str = Field(..., description="Natural-language search query over past knowledge.")
    source: Optional[str] = Field(
        None,
        description=(
            "Optional intent-routed filter: 'crawl'=raw competitor notes, "
            "'analysis'=viral-pattern analysis reports, 'output'=previously "
            "generated copy, 'manual'=manually fed docs."
        ),
    )
    top_k: int = Field(5, ge=1, le=8)


@dataclass
class Tool:
    name: str
    description: str
    params_schema: Type[BaseModel]
    handler: Callable[..., Awaitable[Any]]
    calls: int = field(default=0, init=False)

    def spec(self) -> ToolSpec:
        return ToolSpec(
            name=self.name,
            description=self.description,
            params_schema_name=self.params_schema.__name__,
            params_schema_json=json.dumps(self.params_schema.model_json_schema(), ensure_ascii=False),
        )


Handler = Callable[..., Awaitable[Any]]


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> "ToolRegistry":
        self._tools[tool.name] = tool
        return self

    def get(self, name: str) -> Tool:
        try:
            return self._tools[name]
        except KeyError:
            raise KeyError(f"Unknown tool: {name}. Available: {self.names()}")

    def names(self) -> list[str]:
        return sorted(self._tools)

    def specs(self) -> list[ToolSpec]:
        return [self._tools[n].spec() for n in self.names()]

    def total_calls(self) -> dict[str, int]:
        return {n: t.calls for n, t in self._tools.items()}

    def filtered(self, names: list[str]) -> "ToolRegistry":
        selected = ToolRegistry()
        for name in names:
            source = self.get(name)
            selected.register(
                Tool(source.name, source.description, source.params_schema, source.handler)
            )
        return selected

    async def call(self, name: str, params: dict[str, Any]) -> Any:
        tool = self.get(name)
        params = _coerce_params(tool.params_schema, params)
        validated = tool.params_schema(**params)  # validate + coerce to typed params
        tool.calls += 1
        return await tool.handler(validated)


# --------------------------------------------------------------------------
# Default tool handlers (each receives the validated params object)
# --------------------------------------------------------------------------

async def _load_notes(params: LoadNotesParams) -> list[dict]:
    from app.services.agent_service import load_sample_notes

    notes = params.items if params.items else load_sample_notes()
    return [n.model_dump() for n in notes]


async def _analyze(params: AnalyzeParams) -> dict:
    from app.services.analysis_service import analyze_notes

    return analyze_notes(params.items).model_dump()


async def _topics(params: TopicsParams) -> dict:
    from app.services.topic_service import generate_topics

    req = TopicGenerateRequest(
        summary=params.summary,
        top_keywords=params.top_keywords,
        top_tags=params.top_tags,
        title_patterns=params.title_patterns,
        insight_points=params.insight_points,
        audience=params.audience,
        count=params.count,
        focus_keywords=params.focus_keywords,
        knowledge_hints=params.knowledge_hints,
    )
    return generate_topics(req).model_dump()


async def _contents(params: ContentsParams) -> dict:
    from app.services.content_service import generate_contents

    req = ContentGenerateRequest(
        topic=params.topic,
        reason=params.reason,
        audience=params.audience,
        tone=params.tone,
        count=params.count,
        focus_keywords=params.focus_keywords,
        knowledge_hints=params.knowledge_hints,
    )
    return generate_contents(req).model_dump()


async def _images(params: ImagesParams) -> list[str]:
    from app.models.schemas import TopicItem, ContentItem
    from app.services.image_service import generate_images

    topic = TopicItem(title=params.topic_title, reason=params.topic_reason)
    content = ContentItem(
        title=params.content_title,
        body=params.content_body,
        hashtags=params.content_hashtags,
        cta=params.content_cta,
        image_suggestion=params.content_image_suggestion,
        content_type=params.content_type,
    )
    return await generate_images(topic=topic, content=content, image_count=params.image_count)


async def _publish(params: PublishParams) -> dict:
    from app.models.schemas import TopicItem, ContentItem
    from app.services.publish_service import send_to_xhs

    topic = TopicItem(title=params.topic_title, reason=params.topic_reason)
    content = ContentItem(
        title=params.content_title,
        body=params.content_body,
        hashtags=params.content_hashtags,
        cta=params.content_cta,
        image_suggestion=params.content_image_suggestion,
        content_type=params.content_type,
    )
    images = await _images(
        ImagesParams(
            topic_title=params.topic_title,
            topic_reason=params.topic_reason,
            content_title=params.content_title,
            content_body=params.content_body,
            content_hashtags=params.content_hashtags,
            content_cta=params.content_cta,
            content_image_suggestion=params.content_image_suggestion,
            content_type=params.content_type,
            image_count=params.image_count,
        )
    )
    result = await send_to_xhs(
        content=content,
        image_paths=images,
        is_original=params.is_original,
        visibility=params.visibility,
        mode=params.mode,
    )
    return result.model_dump()


async def _feishu(params: FeishuParams) -> dict:
    from app.models.schemas import XHSMCPToolArgs
    from app.services.feishu_service import sync_to_feishu

    args = XHSMCPToolArgs(
        title=params.title,
        content=params.content,
        images=params.images,
        tags=params.tags,
        is_original=params.is_original,
        visibility=params.visibility,
    )
    return await sync_to_feishu(args, content_type=params.content_type)


async def _crawl(params: CrawlParams) -> dict:
    from app.agent.executor import _crawl_ctx  # lazy: avoid import cycle
    from app.services.local_site_crawler_service import crawl_local_site_notes

    # 13.2: pull the executor-provided stop_check + progress sink (set around the
    # registry.call of this tool) so an in-flight crawl can be cancelled and can
    # stream per-note progress to the console / SSE.
    ctx = _crawl_ctx.get()
    stop_check = ctx["stop_check"] if ctx else None
    sink = ctx["sink"] if ctx else None

    def should_cancel() -> bool:
        return bool(stop_check and stop_check())

    async def on_progress(progress: dict) -> None:
        if sink:
            sink(
                "crawl_progress",
                collected=progress.get("collected", 0),
                target=progress.get("target", params.target_count),
                keyword=progress.get("keyword", ""),
            )

    req = SearchCrawlRequest(
        keywords=params.keywords,
        topic_words=params.topic_words,
        min_comments=params.min_comments,
        min_likes=params.min_likes,
        min_favorites=params.min_favorites,
        target_count=params.target_count,
        content_type=params.content_type,
    )
    return (await crawl_local_site_notes(
        req, on_progress=on_progress, should_cancel=should_cancel
    )).model_dump()


async def _read_artifact(params: ReadArtifactParams) -> dict:
    """Just-in-time read of an offloaded tool result. Paths must stay under the artifacts dir."""
    from app.agent.artifacts import read_artifact_view

    return read_artifact_view(params.path, fields=list(params.fields or []), limit=params.limit)


async def _load_skill(params: LoadSkillParams) -> dict:
    """Return one SKILL.md body. The planner index only carries name + description."""
    from app.agent.skills_loader import load_skill

    return load_skill(params.name)


async def _knowledge_search(params: KnowledgeSearchParams) -> dict:
    """Agentic retrieval tool (调研规划 M2): 检索交给 Agent 决策, 而非固定注入。

    永不抛错——空库/异常降级为可读的 count=0 结果并提示兜底动作, 让 planner
    能在反思中改走 crawl_notes / load_notes (CRAG 式兜底)。
    """
    from app.agent.knowledge import get_knowledge_store

    try:
        store = get_knowledge_store()
        hits = (
            store.search(params.query, top_k=params.top_k, source=params.source)
            if store.count()
            else []
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("knowledge_search failed (non-fatal): %s", exc)
        return {"count": 0, "hits": [], "note": f"knowledge base unavailable: {exc}"}
    if not hits:
        return {
            "count": 0,
            "hits": [],
            "note": "no matching knowledge; consider crawl_notes for fresh data "
            "or load_notes for sample data",
        }
    return {
        "count": len(hits),
        "hits": [
            {
                "doc_id": h.get("doc_id", ""),
                "title": h.get("title", ""),
                "source": h.get("source", ""),
                "score": h.get("score", 0.0),
                "text": str(h.get("text", ""))[:300],
            }
            for h in hits
        ],
    }


_SCALAR_KEYS = ("title", "reason", "text", "name", "content", "summary", "value")
# Keys whose value is a list of records (tool output wrappers like
# ``{"topics": [...]}`` / ``{"contents": [...]}``), used to unwrap planner
# references that point at a whole tool result instead of a single record.
_LIST_KEYS = ("topics", "contents", "items", "notes", "results", "top_notes", "data", "articles")


def _unwrap_to_record(value: Any, depth: int = 0) -> Any:
    """Reduce a planner-provided value down to the first record dict, if possible.

    Real LLM planners often reference a whole tool result (``$latest.generate_topics``)
    instead of a scalar field (``$latest.generate_topics.topics.0.title``); after
    ``_best_effort_dig`` that lands as one of these shapes:

      - a list of records: ``[{...}, ...]``                 -> first record
      - a dict wrapping a list: ``{"topics": [{...}]}``      -> first record
      - a bare record dict: ``{"title": ...}``               -> itself (unchanged)

    Returns the record dict when reachable, else None.
    """
    seen = depth
    while seen < 4:  # bound recursion to avoid pathological loops
        seen += 1
        if isinstance(value, list):
            if not value:
                return None
            value = value[0]
            continue
        if isinstance(value, dict):
            for key in _LIST_KEYS:
                inner = value.get(key)
                if isinstance(inner, list) and inner:
                    value = inner[0]
                    break
            else:
                # dict without a known list wrapper -> this is the record itself
                return value
            continue
        return value if isinstance(value, (str, int, float, bool)) else None
    return value if isinstance(value, dict) else None


def _candidate_keys(field_name: str) -> tuple[str, ...]:
    """Source-key candidates for a params field, in priority order.

    Maps prefixed param names (``content_title`` / ``content_hashtags`` /
    ``topic_title``) back to the record's own keys (``title`` / ``hashtags`` /
    ``title``) while keeping the exact name and generic scalar keys as fallbacks.
    """
    stripped = field_name
    if stripped.startswith("content_"):
        stripped = stripped[len("content_"):]
    elif stripped.startswith("topic_"):
        stripped = stripped[len("topic_"):]
    candidates = [field_name, stripped, *_SCALAR_KEYS]
    seen: set[str] = set()
    result: list[str] = []
    for key in candidates:
        if key not in seen:
            seen.add(key)
            result.append(key)
    return tuple(result)


def _coerce_params(schema: Type[BaseModel], params: dict[str, Any]) -> dict[str, Any]:
    """Loosely coerce planner-provided values into expected field types so
    imprecise references (e.g. passing a whole tool result object where a single
    scalar is expected) degrade gracefully instead of failing the step.

    Only intervenes when a value's type does not already match the field; correct
    values pass through untouched, keeping the coercion backward-compatible.
    """
    from typing import get_args, get_origin

    out = dict(params)
    for field_name, field in schema.model_fields.items():
        ann = field.annotation
        value = out.get(field_name)
        if value is None:
            continue

        # str field: accept as-is if already str, otherwise unwrap + extract.
        if ann is str:
            if isinstance(value, str):
                continue
            record = _unwrap_to_record(value)
            if isinstance(record, dict):
                for key in _candidate_keys(field_name):
                    candidate = record.get(key)
                    if isinstance(candidate, str):
                        out[field_name] = candidate
                        break
            continue

        # list[str] field: accept as-is if already a list, otherwise unwrap +
        # extract the scalar list. Record-list fields (list[NoteItem]) are left
        # to Pydantic so the full list of notes is preserved.
        origin = get_origin(ann)
        if origin is list or ann is list:
            if isinstance(value, list):
                continue
            (inner,) = get_args(ann) or (None,)
            scalar_inner = isinstance(inner, type) and issubclass(
                inner, (str, int, float, bool)
            )
            if scalar_inner:
                record = _unwrap_to_record(value)
                if isinstance(record, dict):
                    for key in _candidate_keys(field_name):
                        candidate = record.get(key)
                        if isinstance(candidate, list):
                            out[field_name] = candidate
                            break
                        if isinstance(candidate, str):
                            out[field_name] = [candidate]
                            break
            elif isinstance(value, dict):
                # 缺陷9: record-list 字段(如 list[NoteItem])遇整个工具结果 dict
                # (如 crawl_notes 返回 {"count":5,"items":[...]})时,取 dict 内
                # 首个列表值作为 items,而不是把整包 dict 透传给 Pydantic.
                for key in (*_candidate_keys(field_name), *_LIST_KEYS):
                    candidate = value.get(key)
                    if isinstance(candidate, list) and candidate:
                        out[field_name] = candidate
                        break

    return out


def build_default_registry(
    tool_overrides: Optional[dict[str, Handler]] = None,
) -> ToolRegistry:
    """Build the standard tool registry; ``tool_overrides`` can swap handlers
    (e.g. stubs for image/publish) to keep offline demos and tests hermetic."""
    overrides = tool_overrides or {}
    registry = ToolRegistry()
    registry.register(Tool(
        "load_notes",
        "Offline/sample notes; use crawl for fresh data.",
        LoadNotesParams,
        overrides.get("load_notes", _load_notes),
    ))
    registry.register(Tool(
        "analyze_notes",
        "Stats and insights from a note list only.",
        AnalyzeParams,
        overrides.get("analyze_notes", _analyze),
    ))
    registry.register(Tool(
        "generate_topics",
        "From an analysis; put the domain in focus_keywords.",
        TopicsParams,
        overrides.get("generate_topics", _topics),
    ))
    registry.register(Tool(
        "generate_contents",
        "Drafts for a topic; use count for N copies, don't split the same tool into N steps.",
        ContentsParams,
        overrides.get("generate_contents", _contents),
    ))
    registry.register(Tool(
        "generate_images",
        "Images for an existing draft.",
        ImagesParams,
        overrides.get("generate_images", _images),
    ))
    registry.register(Tool(
        "publish_to_xhs",
        'Side effect, approval required; never "just try".',
        PublishParams,
        overrides.get("publish_to_xhs", _publish),
    ))
    registry.register(Tool(
        "sync_to_feishu",
        "Side effect: sync a generated note to Feishu Bitable.",
        FeishuParams,
        overrides.get("sync_to_feishu", _feishu),
    ))
    registry.register(Tool(
        "crawl_notes",
        "Keyword crawl; needs a logged-in browser; only if the goal asks for fresh content.",
        CrawlParams,
        overrides.get("crawl_notes", _crawl),
    ))
    registry.register(Tool(
        "knowledge_search",
        "Use first when topic/audience may be in KB; route via source; do not write copy with it.",
        KnowledgeSearchParams,
        overrides.get("knowledge_search", _knowledge_search),
    ))
    registry.register(Tool(
        "read_artifact",
        "Read offloaded $latest fields by path.",
        ReadArtifactParams,
        overrides.get("read_artifact", _read_artifact),
    ))
    registry.register(Tool(
        "load_skill",
        "Load the skill body after an index match, then plan by it.",
        LoadSkillParams,
        overrides.get("load_skill", _load_skill),
    ))
    from app.agent.memory_tools import iter_memory_tools

    for name, description, schema, handler in iter_memory_tools():
        registry.register(Tool(
            name,
            description,
            schema,
            overrides.get(name, handler),
        ))
    return registry
