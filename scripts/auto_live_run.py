"""Auto live-run sentinel: wait for the LLM gateway to come back, then fire the
browser console run + systematic evaluation unattended (2026-09-15).

Loop: ping gateway every 60s (max --tries). On first success:
  1. snapshot knowledge count (kb_before)
  2. run scripts/console_live_run.py (browser full flow, real backend)
  3. locate the new run_id from page_export.json
  4. run scripts/evaluate_run.py --kb-before, save verdict next to screenshots

Usage: python scripts/auto_live_run.py [--tries 30]
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
import tomllib
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

GOAL_SNIPPET = "西电毕业去向"


def gateway_alive() -> tuple[bool, str]:
    import httpx
    from openai import AsyncOpenAI

    cfg_path = os.getenv("CODEX_CONFIG", str(Path.home() / ".codex" / "config.toml"))
    cfg = tomllib.load(open(cfg_path, "rb"))
    prov = cfg["model_providers"]["custom"]
    client = AsyncOpenAI(
        api_key=prov["experimental_bearer_token"],
        base_url=prov.get("base_url"),
        timeout=httpx.Timeout(connect=30.0, read=90.0, write=60.0, pool=60.0),
    )

    async def _ping() -> str:
        t0 = time.time()
        r = await client.responses.create(
            model=cfg.get("model", "deepseek-v4-flash"),
            input="ping, reply with the single word: pong",
            max_output_tokens=16,
        )
        return f"{time.time() - t0:.1f}s -> {(getattr(r, 'output_text', '') or '').strip()[:20]}"

    try:
        detail = asyncio.run(_ping())
        return True, detail
    except Exception as exc:  # noqa: BLE001
        return False, type(exc).__name__


def main() -> int:
    tries = int(sys.argv[sys.argv.index("--tries") + 1]) if "--tries" in sys.argv else 30
    for i in range(1, tries + 1):
        ok, detail = gateway_alive()
        print(f"[sentinel {i}/{tries}] {'UP ' if ok else 'down'} {detail}", flush=True)
        if ok:
            break
        time.sleep(60)
    else:
        print("[sentinel] gateway never came back — giving up")
        return 1

    from app.agent.knowledge import get_knowledge_store

    kb_before = get_knowledge_store().count()
    print(f"[sentinel] firing browser run (kb_before={kb_before})", flush=True)

    r = subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "scripts" / "console_live_run.py")],
        cwd=PROJECT_ROOT,
    )
    if r.returncode != 0:
        print("[sentinel] console run failed — see console_shots")
        return 2

    # 最新一次导出目录即本次 run
    shots = sorted((PROJECT_ROOT / "data" / "agent" / "console_shots").glob("*"))[-1]
    run_id = json.loads((shots / "page_export.json").read_text(encoding="utf-8"))["run_id"]
    run_id = run_id.split("/")[-1].strip()
    print(f"[sentinel] run_id={run_id} — evaluating", flush=True)

    e = subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "scripts" / "evaluate_run.py"), run_id,
         "--kb-before", str(kb_before)],
        cwd=PROJECT_ROOT,
        capture_output=True, text=True, encoding="utf-8",
    )
    report = (e.stdout or "") + (e.stderr or "")
    (shots / "evaluation_report.txt").write_text(report, encoding="utf-8")
    print(report, flush=True)
    print(f"[sentinel] done. artifacts: {shots}", flush=True)
    return e.returncode


if __name__ == "__main__":
    raise SystemExit(main())
