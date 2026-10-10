"""Offline demo: run the Agent end to end with the deterministic Mock backend.

No API key and no external services needed. Demonstrates the genuine Agent loop:
plan -> execute -> reflect -> persist trace.

Usage:
    python run_agent_demo.py [--backend openai|mock] ["goal text"]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

sys.path.insert(0, ".")

from app.agent.orchestrator import XHSAgent
from app.agent.mock_factories import build_mock_backend, build_mock_registry


def _print_result(result) -> None:
    print("\n===== AGENT RESULT =====")
    print(f"run_id    : {result.run_id}")
    print(f"goal      : {result.goal}")
    print(f"trace     : {result.trace_path}")
    print("tool calls:")
    for name, count in result.tool_calls.items():
        if count:
            print(f"  - {name}: {count}")
    print("plan:")
    for s in result.plan.steps:
        print(f"  - [{s.status.value}] {s.action}: {s.summary}")
    print("outputs:")
    print(json.dumps(result.outputs, ensure_ascii=False, indent=2)[:1500])


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("goal", nargs="?", default="为大学生女生生成3篇小红书种草文案并给出配图建议")
    parser.add_argument("--backend", default="mock", choices=["mock", "openai"])
    args = parser.parse_args()

    if args.backend == "mock":
        agent = XHSAgent(backend=build_mock_backend(), registry=build_mock_registry())
    else:
        # Real backend: requires OPENAI_API_KEY (and optionally OPENAI_BASE_URL).
        agent = XHSAgent()

    result = await agent.run(args.goal, audience="大学生女生", tone="真实分享")
    _print_result(result)


if __name__ == "__main__":
    asyncio.run(main())
