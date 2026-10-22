"""M0 bare-topic baseline.

Same topic prompt as ``topic_service``, no ``registry.call``. Mock mode
registers ``TopicsDraft`` on ``MockLLMBackend`` and never opens a live client.
"""

from __future__ import annotations

import hashlib
from contextlib import contextmanager
from typing import Any, Iterator

from pydantic import BaseModel, ConfigDict

from app.agent.llm import MockLLMBackend
from app.agent.tool_eval.case import ToolEvalCase, file_sha256
from app.agent.tool_eval.rubric import behaves, in_main_mean, score_output
from app.core.config import settings
from app.models.schemas import TopicGenerateRequest
from app.services.topic_helper import DEFAULT_AUDIENCE, DEFAULT_COUNT
from app.services.topic_service import load_prompt_template, render_topic_prompt


class BarePathViolation(AssertionError):
    """Raised when the bare path touches the tool registry."""


class TopicDraftItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str
    reason: str


class TopicsDraft(BaseModel):
    """Structured topic list registered on the mock backend (spec C.3)."""

    model_config = ConfigDict(extra="forbid")

    topics: list[TopicDraftItem]


_BARE_PATTERNS = (
    ("{label}清单：怎么做", "把{label}写成可勾选清单，每步写清怎么做，控制在几分钟和几十元。"),
    ("{label}避雷步骤", "用避雷步骤说明{label}哪里容易做错，以及怎么改。"),
    ("{label}公式与分钟", "给出{label}的公式：按分钟拆开，写清每一步。"),
    ("{label}如何分步", "说明如何把{label}拆成步骤，方便照着做。"),
    ("{label}一天安排", "用一天的安排把{label}落到具体场景，写清每步怎么做。"),
)


def prompt_sha256() -> str:
    """Digest of the shared ``topic_generation_prompt.txt`` text."""
    return hashlib.sha256(load_prompt_template().encode("utf-8")).hexdigest()


def assert_bare_trace(trace: dict[str, Any], tool_calls_total: int) -> None:
    steps = trace.get("steps") if isinstance(trace, dict) else None
    if steps != [] or int(tool_calls_total) != 0:
        raise BarePathViolation(
            f"M0 must not call tools (steps={steps!r}, tool_calls_total={tool_calls_total})"
        )


@contextmanager
def guard_registry() -> Iterator[dict[str, int]]:
    """Count and reject ``ToolRegistry.call`` for the duration of an M0 run."""
    from app.agent.tools import ToolRegistry

    counter = {"n": 0}
    original = ToolRegistry.call

    async def trapped(self, name: str, params: dict[str, Any]) -> Any:
        counter["n"] += 1
        raise BarePathViolation(f"M0 must not call registry.call ({name})")

    ToolRegistry.call = trapped  # type: ignore[method-assign]
    try:
        yield counter
    finally:
        ToolRegistry.call = original  # type: ignore[method-assign]


def case_to_request(case: ToolEvalCase) -> TopicGenerateRequest:
    theme = case.theme.strip()
    audience = (case.audience or "").strip() or DEFAULT_AUDIENCE
    count = case.count if case.count is not None else DEFAULT_COUNT
    summary = theme or "（空主题）"
    return TopicGenerateRequest(
        summary=summary,
        top_keywords=[],
        top_tags=[],
        title_patterns=[],
        insight_points=[],
        audience=audience,
        count=count,
        focus_keywords=[theme] if theme else [],
        knowledge_hints=[],
    )


def angle_from_title(title: str) -> str:
    """Map a topic title to an angle: the first half of the title (spec D.5)."""
    text = (title or "").strip()
    if not text:
        return ""
    return text[: max(1, len(text) // 2)]


def mock_topics_draft(label: str, count: int) -> TopicsDraft:
    """Deterministic topics that stay on the theme and avoid fake metrics."""
    safe_count = count if count > 0 else DEFAULT_COUNT
    topics: list[TopicDraftItem] = []
    for index in range(safe_count):
        title_t, reason_t = _BARE_PATTERNS[index % len(_BARE_PATTERNS)]
        title = title_t.format(label=label)
        if safe_count > len(_BARE_PATTERNS):
            title = f"{title}#{index + 1}"
        topics.append(TopicDraftItem(title=title, reason=reason_t.format(label=label)))
    return TopicsDraft(topics=topics)


def _angles_from_draft(draft: TopicsDraft, audience: str) -> list[dict[str, Any]]:
    angles: list[dict[str, Any]] = []
    for topic in draft.topics:
        title = topic.title.strip()
        angles.append(
            {
                "angle": angle_from_title(title),
                "title": title,
                "why": topic.reason.strip(),
                "audience": audience,
                "grounded_from": [],
            }
        )
    return angles


def build_mock_backend(request: TopicGenerateRequest) -> MockLLMBackend:
    label = request.summary.strip() or "（空主题）"
    draft = mock_topics_draft(label, request.count)

    def factory() -> TopicsDraft:
        return draft

    return MockLLMBackend().register(TopicsDraft, factory)


async def run_m0_case(
    case: ToolEvalCase,
    *,
    model_id: str,
    temperature: float,
    max_tokens: int | None,
    prompt_digest: str,
    backend: MockLLMBackend | None = None,
) -> dict[str, Any]:
    """Generate angles for one case without tools. Raises if a tool is called."""
    request = case_to_request(case)
    prompt = render_topic_prompt(request)
    if "只输出 JSON" not in prompt or request.summary not in prompt:
        raise RuntimeError("M0 prompt is not the shared topic_generation_prompt")
    llm = backend or build_mock_backend(request)
    try:
        draft = await llm.generate(prompt, TopicsDraft, temperature=temperature)
    except BarePathViolation:
        raise
    except Exception as exc:  # noqa: BLE001 — parse/backend failure becomes a row
        row = _row(
            case,
            request,
            model_id=model_id,
            temperature=temperature,
            max_tokens=max_tokens,
            prompt_digest=prompt_digest,
            status="degraded",
            parsed=False,
            angles=[],
            raw={"error": str(exc)},
            message=str(exc),
        )
        return _finish_row(case, row)

    if not isinstance(draft, TopicsDraft):
        row = _row(
            case,
            request,
            model_id=model_id,
            temperature=temperature,
            max_tokens=max_tokens,
            prompt_digest=prompt_digest,
            status="degraded",
            parsed=False,
            angles=[],
            raw={"error": "backend did not return TopicsDraft"},
            message="backend did not return TopicsDraft",
        )
        return _finish_row(case, row)

    row = _row(
        case,
        request,
        model_id=model_id,
        temperature=temperature,
        max_tokens=max_tokens,
        prompt_digest=prompt_digest,
        status="ok",
        parsed=True,
        angles=_angles_from_draft(draft, request.audience),
        raw=draft.model_dump(),
        message="",
    )
    return _finish_row(case, row)


def _row(
    case: ToolEvalCase,
    request: TopicGenerateRequest,
    *,
    model_id: str,
    temperature: float,
    max_tokens: int | None,
    prompt_digest: str,
    status: str,
    parsed: bool,
    angles: list[dict[str, Any]],
    raw: dict[str, Any],
    message: str,
) -> dict[str, Any]:
    return {
        "id": case.id,
        "bucket": case.bucket.value,
        "theme": case.theme,
        "audience": request.audience,
        "count": request.count,
        "expect_status": case.expect_status.value,
        "model_id": model_id,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "prompt_sha256": prompt_digest,
        "status": status,
        "parsed": parsed,
        "message": message,
        "angles": angles,
        "raw": raw,
        "trace": {"steps": []},
        "tool_calls_total": 0,
        "in_main_mean": in_main_mean(case.bucket.value),
    }


def _finish_row(case: ToolEvalCase, row: dict[str, Any]) -> dict[str, Any]:
    assert_bare_trace(row["trace"], int(row["tool_calls_total"]))
    row["rubric"] = score_output(case, row)
    if case.bucket.value in {"refuse", "clarify"}:
        row["compliant"] = behaves(case, row)
    else:
        row["compliant"] = None
    return row


async def run_m0(
    cases: list[ToolEvalCase],
    *,
    model_id: str,
    backend_name: str = "mock",
    eval_path: str = "",
    eval_sha256: str = "",
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Run every case on the bare path. ``backend_name`` must be ``mock``."""
    if backend_name != "mock":
        raise ValueError("this round only runs backend=mock; live M0 is out of scope")
    temperature = float(settings.openai_temperature)
    max_tokens = None
    digest = prompt_sha256()
    counts = sorted({(case.count if case.count is not None else DEFAULT_COUNT) for case in cases})
    rows: list[dict[str, Any]] = []
    with guard_registry() as counter:
        for case in cases:
            before = counter["n"]
            row = await run_m0_case(
                case,
                model_id=model_id,
                temperature=temperature,
                max_tokens=max_tokens,
                prompt_digest=digest,
            )
            delta = counter["n"] - before
            row["tool_calls_total"] = delta
            assert_bare_trace(row["trace"], row["tool_calls_total"])
            rows.append(row)
    meta = {
        "backend": backend_name,
        "model_id": model_id,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "count": counts[0] if len(counts) == 1 else counts,
        "prompt_sha256": digest,
        "eval_path": eval_path,
        "eval_sha256": eval_sha256,
    }
    return rows, meta


def eval_digest(path: str) -> str:
    return file_sha256(path)
