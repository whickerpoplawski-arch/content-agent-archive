#!/usr/bin/env python3
"""Smoke the content-agent OpenRouter client.

Reads OPENROUTER_API_KEY / OPENROUTER_BASE_URL / OPENROUTER_MODEL from the
process environment. Exits 0 without a network call when the key is missing.
Does not invent a key and does not read a committed secret.

    export OPENROUTER_API_KEY=...
    export OPENROUTER_BASE_URL=https://openrouter.ai/api/v1   # optional
    export OPENROUTER_MODEL=openrouter/free                   # optional free router
    python scripts/openrouter_smoke.py
"""

from __future__ import annotations

import asyncio
import os
import sys


def main() -> int:
    if not os.environ.get("OPENROUTER_API_KEY", "").strip():
        print("SKIP: OPENROUTER_API_KEY not set; no live OpenRouter call")
        return 0

    from pydantic import BaseModel, Field

    from app.agent.llm import get_backend, openrouter_config

    class Ping(BaseModel):
        ok: bool = Field(...)
        echo: str = ""

    cfg = openrouter_config()
    print(f"calling model={cfg['model']} base={cfg['base_url']}")

    async def _run() -> None:
        backend = get_backend("openai")
        try:
            if getattr(backend, "name", "") != "openrouter":
                raise SystemExit("expected OpenRouter backend when OPENROUTER_API_KEY is set")
            result = await backend.generate(
                'Reply with JSON {"ok": true, "echo": "pong"} and nothing else.',
                Ping,
                temperature=0,
            )
            print(f"ok={result.ok} echo={result.echo}")
            if not result.ok:
                raise SystemExit("model returned ok=false")
        finally:
            await backend.aclose()

    asyncio.run(_run())
    return 0


if __name__ == "__main__":
    sys.exit(main())
