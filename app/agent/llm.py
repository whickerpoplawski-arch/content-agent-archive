"""Unified LLM layer.

Provides a pluggable backend (real OpenAI/Responses API or a deterministic Mock
for tests), structured JSON output, exponential-backoff retries, and cost
tracking so an Agent run can be observed and billed without hard-wiring any
single vendor.

The real backend talks the OpenAI *Responses* wire API and reads model / base_url
/ api_key from the same place Codex does (config.toml + auth.json), so this
project stays consistent with the user's Codex setup.

Reference patterns applied (mainstream agent stacks):
  - Backend abstraction / dependency injection.
  - Structured output via Pydantic instead of brittle free-text JSON parsing.
  - Retry with exponential backoff.
  - Cost accounting at the call site.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from abc import ABC, abstractmethod
from typing import Any, Optional, Type, TypeVar

from pydantic import BaseModel

from app.core.config import settings

logger = logging.getLogger("agent.llm")

T = TypeVar("T", bound=BaseModel)


class LLMBackend(ABC):
    """A model backend that turns a prompt into a structured Pydantic object."""

    name: str = "abstract"

    @abstractmethod
    async def generate(
        self,
        prompt: str,
        schema: Type[T],
        *,
        temperature: Optional[float] = None,
    ) -> T:
        """Run the model and return an instance of ``schema``."""

    def usage(self) -> dict[str, Any]:
        """Aggregate usage/cost stats for this backend across a run."""
        return {"calls": 0, "est_tokens": 0}

    async def aclose(self) -> None:
        """Release underlying resources (no-op by default)."""


class OpenAILLMBackend(LLMBackend):
    """Real backend using the OpenAI Responses API (matches Codex wire_api)."""

    name = "openai"

    def __init__(self) -> None:
        from openai import AsyncOpenAI

        # 网关冷启动可达 230s(README/optimization-plan), openai 默认 connect=5s
        # 会在冷启动期直接 APITimeoutError; 放宽 connect, 读仍 600s。
        import httpx

        self._client = AsyncOpenAI(
            api_key=settings.openai_api_key or None,
            base_url=settings.openai_base_url or None,
            timeout=httpx.Timeout(connect=60.0, read=600.0, write=600.0, pool=600.0),
        )
        self._model = settings.openai_model
        self._last: dict[str, Any] = {}
        self._usage = {
            "calls": 0,
            "est_tokens": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_tokens": 0,
            "estimated": False,
        }

    async def generate(
        self,
        prompt: str,
        schema: Type[T],
        *,
        temperature: Optional[float] = None,
    ) -> T:
        schema_json = json.dumps(schema.model_json_schema(), ensure_ascii=False)
        instructions = (
            "You are a precise assistant. Reply with ONLY valid JSON that conforms "
            "exactly to this JSON Schema (no markdown, no extra text):\n" + schema_json
        )
        started = time.perf_counter()
        response = await self._client.responses.create(
            model=self._model,
            instructions=instructions,
            input=prompt,
            temperature=(
                temperature if temperature is not None else settings.openai_temperature
            ),
        )
        text = getattr(response, "output_text", "")
        if not text:
            raise RuntimeError("LLM returned empty output_text")

        # P0-1: 用量真实化。优先取 Responses API 响应 usage(input/output/cache),
        # 后端不返回时 fallback 为双端估算(输入+输出)并标记 estimated。
        usage = getattr(response, "usage", None)
        input_tokens = output_tokens = cache_read = 0
        estimated = True
        if usage is not None:
            input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
            output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
            details = getattr(usage, "input_tokens_details", None)
            if details is not None:
                cache_read = int(getattr(details, "cached_tokens", 0) or 0)
            if input_tokens or output_tokens:
                estimated = False
        if estimated:
            input_tokens = _estimate_tokens(prompt)
            output_tokens = _estimate_tokens(text)

        total = input_tokens + output_tokens
        self._last = {
            "model": self._model,
            "elapsed_ms": int((time.perf_counter() - started) * 1000),
            "output_chars": len(text),
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_read_tokens": cache_read,
            "estimated": estimated,
            "total_tokens": total,
        }
        self._usage["calls"] += 1
        self._usage["est_tokens"] += total
        self._usage["input_tokens"] += input_tokens
        self._usage["output_tokens"] += output_tokens
        self._usage["cache_read_tokens"] += cache_read
        self._usage["estimated"] = self._usage["estimated"] or estimated
        data = json.loads(text)  # may raise -> handled by retry in generate_structured
        return schema(**data)

    def usage(self) -> dict[str, Any]:
        return dict(self._usage)

    async def aclose(self) -> None:
        # 不关客户端的后果: Windows proactor 上 asyncio.run 的 loop.close() 会
        # 等 keep-alive 连接的挂起 overlapped I/O, 跑完的 run 卡死在收尾
        # (py-spy 实锤 2026-09-16)。每个 run 的 finally 必须走到这里。
        try:
            await self._client.close()
        except Exception as exc:  # noqa: BLE001
            logger.warning("LLM client close failed (non-fatal): %s", exc)


def _estimate_tokens(text: str) -> int:
    try:
        import tiktoken

        enc = tiktoken.get_encoding("cl100k_base")
        return len(enc.encode(text))
    except Exception:
        return max(1, len(text) // 4)


class MockLLMBackend(LLMBackend):
    """Deterministic backend for tests / offline demos (no API key required).

    Each supported schema has a factory that returns canned-but-valid data so the
    whole Agent loop (plan -> execute -> reflect) can be exercised end to end.
    """

    name = "mock"

    def __init__(self, factories: Optional[dict[Type[BaseModel], Any]] = None) -> None:
        self._factories: dict[Type[BaseModel], Any] = factories or {}
        self._usage = {"calls": 0, "est_tokens": 0}

    def register(self, schema: Type[BaseModel], factory: Any) -> "MockLLMBackend":
        self._factories[schema] = factory
        return self

    async def generate(
        self,
        prompt: str,
        schema: Type[T],
        *,
        temperature: Optional[float] = None,
    ) -> T:
        factory = self._factories.get(schema)
        if factory is None:
            raise ValueError(f"MockLLMBackend has no factory for {schema.__name__}")
        value = factory() if callable(factory) else factory
        self._usage["calls"] += 1
        if isinstance(value, schema):
            return value
        if isinstance(value, dict):
            return schema(**value)
        raise TypeError(f"factory for {schema.__name__} returned unexpected {value!r}")

    def usage(self) -> dict[str, Any]:
        return dict(self._usage)


def get_backend(name: Optional[str] = None) -> LLMBackend:
    """Return the configured LLM backend (dependency-injected and swappable)."""
    configured = name or getattr(settings, "agent_llm_backend", "openai") or "openai"
    if configured == "mock":
        from app.agent.mock_factories import build_mock_backend

        return build_mock_backend()
    return OpenAILLMBackend()


async def generate_structured(
    prompt: str,
    schema: Type[T],
    *,
    backend: Optional[LLMBackend] = None,
    max_retries: int = 3,
    temperature: Optional[float] = None,
) -> T:
    """Generate a structured object with retries and exponential backoff."""
    backend = backend or get_backend()
    last_error: Optional[Exception] = None
    for attempt in range(1, max_retries + 1):
        try:
            return await backend.generate(prompt, schema, temperature=temperature)
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            logger.warning("structured generate failed attempt=%d: %s", attempt, exc)
            if attempt < max_retries:
                await asyncio.sleep(2 ** attempt)
    raise RuntimeError(f"LLM generation failed after {max_retries} attempts: {last_error}")


def dump(model: BaseModel) -> dict:
    """Serialize a Pydantic model to a plain JSON-safe dict (Pydantic v2)."""
    return json.loads(model.model_dump_json())
