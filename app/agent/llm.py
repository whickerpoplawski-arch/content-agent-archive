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
import os
import re
import time
from abc import ABC, abstractmethod
from typing import Any, Optional, Type, TypeVar

from pydantic import BaseModel

from app.core.config import settings

logger = logging.getLogger("agent.llm")

T = TypeVar("T", bound=BaseModel)

# Default free router: https://openrouter.ai/openrouter/free
# It selects a zero-price model and is the documented free model id.
_OPENROUTER_DEFAULT_BASE = "https://openrouter.ai/api/v1"
_OPENROUTER_DEFAULT_MODEL = "openrouter/free"


def openrouter_config() -> dict[str, str]:
    """Read OpenRouter settings from the process environment only.

    Empty ``api_key`` means the legacy Codex/OpenAI Responses backend stays in
    use. This helper never invents a key and never reads a committed secret.
    """
    api_key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    base_url = os.environ.get("OPENROUTER_BASE_URL", "").strip() or _OPENROUTER_DEFAULT_BASE
    model = os.environ.get("OPENROUTER_MODEL", "").strip() or _OPENROUTER_DEFAULT_MODEL
    return {"api_key": api_key, "base_url": base_url, "model": model}


def openrouter_model_name() -> str:
    cfg = openrouter_config()
    return cfg["model"] if cfg["api_key"] else ""


def _loads_json_object(text: str) -> dict:
    """Parse a JSON object, tolerating markdown fences and leading prose."""
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        start = raw.find("{")
        end = raw.rfind("}")
        if start < 0 or end <= start:
            raise
        data = json.loads(raw[start : end + 1])
    if not isinstance(data, dict):
        raise ValueError("LLM JSON was not an object")
    return data


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
    """Real backend: OpenRouter chat completions when OPENROUTER_API_KEY is set,
    otherwise the OpenAI Responses API (Codex wire_api)."""

    name = "openai"

    def __init__(self) -> None:
        from openai import AsyncOpenAI

        # 网关冷启动可达 230s(README/optimization-plan), openai 默认 connect=5s
        # 会在冷启动期直接 APITimeoutError; 放宽 connect, 读仍 600s。
        import httpx

        timeout = httpx.Timeout(connect=60.0, read=600.0, write=600.0, pool=600.0)
        router = openrouter_config()
        if router["api_key"]:
            # Content-agent runtime only. The key comes from the environment.
            self.name = "openrouter"
            self._wire = "chat"
            self._client = AsyncOpenAI(
                api_key=router["api_key"],
                base_url=router["base_url"],
                timeout=timeout,
                default_headers={
                    "HTTP-Referer": "http://localhost",
                    "X-Title": "XHS Content Agent",
                },
            )
            self._model = router["model"]
        else:
            self.name = "openai"
            self._wire = "responses"
            self._client = AsyncOpenAI(
                api_key=settings.openai_api_key or None,
                base_url=settings.openai_base_url or None,
                timeout=timeout,
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

    def _account(
        self,
        *,
        prompt: str,
        text: str,
        started: float,
        input_tokens: int,
        output_tokens: int,
        cache_read: int,
        estimated: bool,
    ) -> None:
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
        if self._wire == "chat":
            text, input_tokens, output_tokens, cache_read, estimated = await self._chat_generate(
                prompt, instructions, temperature
            )
        else:
            text, input_tokens, output_tokens, cache_read, estimated = await self._responses_generate(
                prompt, instructions, temperature
            )
        if not text:
            raise RuntimeError("LLM returned empty output")
        if estimated:
            input_tokens = _estimate_tokens(prompt)
            output_tokens = _estimate_tokens(text)
        self._account(
            prompt=prompt,
            text=text,
            started=started,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read=cache_read,
            estimated=estimated,
        )
        data = _loads_json_object(text)  # may raise -> handled by retry in generate_structured
        return schema(**data)

    async def _responses_generate(
        self, prompt: str, instructions: str, temperature: Optional[float]
    ) -> tuple[str, int, int, int, bool]:
        response = await self._client.responses.create(
            model=self._model,
            instructions=instructions,
            input=prompt,
            temperature=(
                temperature if temperature is not None else settings.openai_temperature
            ),
        )
        text = getattr(response, "output_text", "") or ""
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
        return text, input_tokens, output_tokens, cache_read, estimated

    async def _chat_generate(
        self, prompt: str, instructions: str, temperature: Optional[float]
    ) -> tuple[str, int, int, int, bool]:
        """OpenRouter-compatible Chat Completions call (async client)."""
        temp = temperature if temperature is not None else settings.openai_temperature
        kwargs: dict[str, Any] = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": instructions},
                {"role": "user", "content": prompt},
            ],
            "temperature": temp,
        }
        try:
            response = await self._client.chat.completions.create(**kwargs)
        except Exception as exc:  # noqa: BLE001
            # Some free models reject a non-default temperature. Retry once without it.
            if "temperature" in kwargs and "temperature" in str(exc).lower():
                kwargs.pop("temperature", None)
                response = await self._client.chat.completions.create(**kwargs)
            else:
                raise
        choice = response.choices[0].message
        content = getattr(choice, "content", "") or ""
        if isinstance(content, list):
            chunks = []
            for part in content:
                if isinstance(part, dict):
                    chunks.append(str(part.get("text") or ""))
                else:
                    chunks.append(str(getattr(part, "text", part)))
            text = "".join(chunks)
        else:
            text = str(content)
        usage = getattr(response, "usage", None)
        input_tokens = output_tokens = cache_read = 0
        estimated = True
        if usage is not None:
            input_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
            output_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
            details = getattr(usage, "prompt_tokens_details", None)
            if details is not None:
                cache_read = int(getattr(details, "cached_tokens", 0) or 0)
            if input_tokens or output_tokens:
                estimated = False
        return text, input_tokens, output_tokens, cache_read, estimated

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
