"""回归: run 结束必须 aclose LLM 客户端。

背景(2026-09-16, run a6b5e5c10b33 卡死实锤): OpenAILLMBackend 的 httpx
客户端从不关闭, Windows proactor 上 asyncio.run 的 loop.close() 会等
keep-alive 连接的挂起 overlapped I/O, agent 本体早已跑完却永久卡在收尾。
修复: run_manager._execute 在 finally 里于同一 loop 内调 backend.aclose();
巩固任务改用独立客户端, 用完自关。
"""

from __future__ import annotations

import time

TERMINAL = {"completed", "failed", "stopped"}


def test_run_manager_closes_backend_after_run(tmp_path, monkeypatch):
    from app.agent import run_manager as rm_mod
    from app.agent.mock_factories import build_mock_backend
    from app.agent.run_manager import AgentRunManager

    closed = {"count": 0}
    backend = build_mock_backend()

    async def spy_aclose() -> None:
        closed["count"] += 1

    backend.aclose = spy_aclose  # 实例属性遮蔽基类 no-op
    monkeypatch.setattr(rm_mod, "build_mock_backend", lambda: backend)

    mgr = AgentRunManager(runs_dir=tmp_path / "console_runs")
    summary = mgr.create(
        goal="生成1篇测试文案",
        audience="学生",
        tone="真实",
        backend="mock",
        enabled_tools=None,
    )
    run_id = summary["run_id"]

    deadline = time.time() + 20
    while time.time() < deadline:
        if mgr.summary(run_id)["status"] in TERMINAL:
            break
        time.sleep(0.1)

    assert mgr.summary(run_id)["status"] == "completed"
    # 主 run 的 finally 必然关一次; 巩固任务(共享 mock backend 时)可能再关一次。
    assert closed["count"] >= 1
