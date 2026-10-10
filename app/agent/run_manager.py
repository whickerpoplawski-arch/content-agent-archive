from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from app.agent.audit import audit
from app.agent.checkpoint import StepCheckpointer
from app.core.config import settings
from app.agent.mock_factories import build_mock_backend, build_mock_registry
from app.agent.orchestrator import XHSAgent
from app.agent.tools import build_default_registry


TERMINAL_STATES = {"completed", "failed", "stopped"}
DEFAULT_TOOLS = [
    "load_notes",
    "analyze_notes",
    "generate_topics",
    "generate_contents",
    "generate_images",
    "crawl_notes",
    "knowledge_search",  # 调研规划 M2: 检索工具化, 只读安全, 默认启用
]
ALL_TOOLS = DEFAULT_TOOLS + ["publish_to_xhs", "sync_to_feishu"]

logger = logging.getLogger("agent.run_manager")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _resolve_terminal(stopped: bool, is_complete: bool) -> tuple[str, str]:
    """缺陷7: 终态判定。用户停止 -> stopped;计划全部成功/跳过 -> completed;
    任何 FAILED 步骤或预算中断(plan 不完整) -> failed,绝不虚报 completed。"""
    if stopped:
        return "run_stopped", "stopped"
    if is_complete:
        return "run_completed", "completed"
    return "run_failed", "failed"


class AgentRunManager:
    def __init__(self, runs_dir: Path = Path("data/agent/console_runs")) -> None:
        self.runs_dir = runs_dir
        self._runs: dict[str, dict[str, Any]] = {}
        self._lock = threading.RLock()
        self._approval_conditions: dict[str, threading.Condition] = {}
        self._pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="agent-run")
        self._load_runs()

    def create(
        self,
        *,
        goal: str,
        audience: str,
        tone: str,
        backend: str,
        enabled_tools: Optional[list[str]],
    ) -> dict[str, Any]:
        tools = list(DEFAULT_TOOLS) if enabled_tools is None else enabled_tools
        unknown = sorted(set(tools) - set(ALL_TOOLS))
        if unknown:
            raise ValueError(f"Unknown tools: {', '.join(unknown)}")

        run_id = uuid.uuid4().hex[:12]
        created_at = _now()
        run = {
            "run_id": run_id,
            "goal": goal,
            "audience": audience,
            "tone": tone,
            "backend": backend,
            # 缺陷6: 记录实际模型名,控制台不再写死 "OpenAI"。
            "model": "mock" if backend == "mock" else (settings.openai_model or backend),
            "enabled_tools": tools,
            "status": "queued",
            "created_at": created_at,
            "updated_at": created_at,
            "events": [],
            "plan": [],
            "outputs": {},
            "usage": {},
            "tool_calls": {},
            "error": None,
            "stop_requested": False,
            "pending_approval": None,
        }
        with self._lock:
            self._runs[run_id] = run
            self._approval_conditions[run_id] = threading.Condition(self._lock)
            self._persist(run)
        self._pool.submit(self._execute, run_id)
        return self.summary(run_id)

    def get(self, run_id: str) -> dict[str, Any]:
        with self._lock:
            run = self._require(run_id)
            return json.loads(json.dumps(run, ensure_ascii=False))

    def summary(self, run_id: str) -> dict[str, Any]:
        run = self.get(run_id)
        return {
            "run_id": run["run_id"],
            "goal": run["goal"],
            "status": run["status"],
            "created_at": run["created_at"],
            "updated_at": run["updated_at"],
            "backend": run["backend"],
            "model": run.get("model"),
            "artifact_count": len(run.get("outputs", {}).get("results", [])),
        }

    def list(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            ids = sorted(
                self._runs,
                key=lambda item: self._runs[item]["created_at"],
                reverse=True,
            )[: min(max(limit, 1), 50)]
        return [self.summary(run_id) for run_id in ids]

    def events_after(self, run_id: str, last_event_id: int = 0) -> list[dict[str, Any]]:
        run = self.get(run_id)
        return [event for event in run["events"] if event["id"] > last_event_id]

    def stop(self, run_id: str) -> dict[str, Any]:
        with self._lock:
            run = self._require(run_id)
            if run["status"] in TERMINAL_STATES:
                return self.summary(run_id)
            run["stop_requested"] = True
            run["status"] = "stopping"
            run["updated_at"] = _now()
            self._persist(run)
            condition = self._approval_conditions.get(run_id)
            if condition:
                condition.notify_all()
        audit("stop", run_id)
        self._emit(run_id, "run_stopping", {})
        return self.summary(run_id)

    def confirm(self, run_id: str, approval_id: str, approved: bool) -> dict[str, Any]:
        with self._lock:
            run = self._require(run_id)
            pending = run.get("pending_approval")
            if not pending or pending.get("approval_id") != approval_id:
                raise RuntimeError("No matching approval is pending")
            pending["decision"] = approved
            condition = self._approval_conditions[run_id]
            condition.notify_all()
        return self.summary(run_id)

    def resume(self, run_id: str) -> dict[str, Any]:
        """Requeue a terminal run and continue it from its last checkpoint."""
        from app.agent.checkpoint import StepCheckpointer

        with self._lock:
            run = self._require(run_id)
            if run["status"] not in TERMINAL_STATES:
                raise RuntimeError("Run is still active")
            if not StepCheckpointer().load(run_id):
                raise RuntimeError("No checkpoint available to resume from")
            run["status"] = "queued"
            run["stop_requested"] = False
            run["error"] = None
            run["updated_at"] = _now()
            self._persist(run)
        self._pool.submit(self._execute, run_id)
        audit("resume", run_id)
        return self.summary(run_id)

    def _maybe_housekeep(self) -> None:
        """阶段5: 每次 run 后必触发 TTL 清理(不再概率跳过)。"""
        if not settings.agent_housekeeping_auto:
            return
        from app.agent.housekeeping import purge_old_runs

        purge_old_runs(
            retention_days=settings.agent_housekeeping_retention_days
        )

    def _execute(self, run_id: str) -> None:
        try:
            self._set_status(run_id, "running")
            self._emit(run_id, "run_started", {})
            run = self.get(run_id)
            if run["backend"] == "mock":
                registry = build_mock_registry().filtered(run["enabled_tools"])
                agent = XHSAgent(backend=build_mock_backend(), registry=registry)
            else:
                registry = build_default_registry().filtered(run["enabled_tools"])
                agent = XHSAgent(registry=registry)

            async def _run_agent():
                try:
                    return await agent.run(
                        run["goal"],
                        audience=run["audience"],
                        tone=run["tone"],
                        run_id=run_id,
                        event_sink=lambda kind, fields: self._from_agent_event(
                            run_id, kind, fields
                        ),
                        stop_check=lambda: self._stop_requested(run_id),
                        approval_handler=lambda action, params: self._wait_for_approval(
                            run_id, action, params
                        ),
                        checkpointer=StepCheckpointer(),
                        consolidation_pool=self._pool,
                    )
                finally:
                    # 必须在同一个 loop 内关掉 LLM 客户端: 否则 Windows proactor
                    # 的 loop.close() 会等 keep-alive 连接的挂起 I/O, run 永久
                    # 卡死在 asyncio.run 收尾(py-spy 实锤 2026-09-16)。
                    closer = getattr(agent.backend, "aclose", None)
                    if closer is not None:
                        try:
                            await closer()
                        except Exception as exc:  # noqa: BLE001
                            logger.warning("backend close failed (non-fatal): %s", exc)

            result = asyncio.run(_run_agent())
            with self._lock:
                current = self._require(run_id)
                current["plan"] = [step.model_dump(mode="json") for step in result.plan.steps]
                current["outputs"] = result.outputs
                current["usage"] = result.usage
                current["tool_calls"] = result.tool_calls
                stopped = current["stop_requested"]
                current["updated_at"] = _now()
                self._persist(current)

            # 缺陷7/11: 终态以 plan.is_complete 为准,绝不无条件标 completed.
            # 有 FAILED 步骤或预算中断(stopped_reason 非空)时标 failed 并透出原因.
            event_type, status = _resolve_terminal(stopped, result.plan.is_complete)
            if status == "failed":
                failed = [s for s in result.plan.steps if s.status == "failed"]
                reason = result.stopped_reason
                if not reason and failed:
                    reason = "{} step(s) failed: {}".format(
                        len(failed),
                        "; ".join(
                            f"{s.action}: {s.error}" for s in failed[:3]
                        ),
                    )
                with self._lock:
                    current = self._require(run_id)
                    current["error"] = reason or "Plan incomplete"
                    current["updated_at"] = _now()
                    self._persist(current)
            self._emit(run_id, event_type, {"outputs": result.outputs})
            self._set_status(run_id, status)
        except Exception as exc:
            with self._lock:
                run = self._require(run_id)
                run["error"] = str(exc)
                run["updated_at"] = _now()
                self._persist(run)
            self._emit(run_id, "run_failed", {"error": str(exc)})
            self._set_status(run_id, "failed")
        finally:
            # 阶段5: run 结束后必触发 TTL 清理(不阻塞 create 响应)。
            self._maybe_housekeep()

    def _from_agent_event(self, run_id: str, kind: str, fields: dict[str, Any]) -> None:
        if kind == "plan":
            self._emit(run_id, "plan_created", fields)
        elif kind == "tool_start":
            self._emit(run_id, "step_started", fields)
            self._emit(run_id, "tool_started", fields)
        elif kind == "tool_call":
            event_type = "tool_completed" if fields.get("ok") else "step_failed"
            self._emit(run_id, event_type, fields)
        elif kind == "resume_from_checkpoint":
            self._emit(run_id, "resume_from_checkpoint", fields)
        elif kind == "crawl_progress":
            self._emit(run_id, "crawl_progress", fields)
        elif kind == "reflection":
            self._emit(run_id, "reflection", fields)
            if fields.get("next_action") == "replan":
                self._emit(run_id, "replan", fields)
        elif kind == "quality":
            self._emit(run_id, "quality_evaluation", fields)
        elif kind == "approval_rejected":
            self._emit(run_id, "approval_resolved", {**fields, "approved": False})

    def _stop_requested(self, run_id: str) -> bool:
        with self._lock:
            return bool(self._require(run_id).get("stop_requested"))

    async def _wait_for_approval(
        self, run_id: str, action: str, params: dict[str, Any]
    ) -> bool:
        approval_id = uuid.uuid4().hex[:12]
        with self._lock:
            run = self._require(run_id)
            run["status"] = "waiting_for_approval"
            run["pending_approval"] = {
                "approval_id": approval_id,
                "action": action,
                "params": params,
                "decision": None,
            }
            self._persist(run)
        self._emit(
            run_id,
            "approval_required",
            {"approval_id": approval_id, "action": action, "params": params},
        )

        decision = await asyncio.to_thread(self._wait_for_approval_sync, run_id)
        with self._lock:
            run = self._require(run_id)
            run["pending_approval"] = None
            if not run["stop_requested"]:
                run["status"] = "running"
            self._persist(run)
        self._emit(
            run_id,
            "approval_resolved",
            {"approval_id": approval_id, "action": action, "approved": decision},
        )
        return decision

    def _wait_for_approval_sync(self, run_id: str) -> bool:
        with self._lock:
            condition = self._approval_conditions[run_id]
            while True:
                run = self._require(run_id)
                if run["stop_requested"]:
                    return False
                pending = run.get("pending_approval")
                if pending and pending.get("decision") is not None:
                    return bool(pending["decision"])
                condition.wait(timeout=0.5)

    def _emit(self, run_id: str, event_type: str, data: dict[str, Any]) -> None:
        with self._lock:
            run = self._require(run_id)
            event = {
                "id": len(run["events"]) + 1,
                "run_id": run_id,
                "type": event_type,
                "timestamp": _now(),
                "data": data,
            }
            run["events"].append(event)
            run["updated_at"] = event["timestamp"]
            self._persist(run)

    def _set_status(self, run_id: str, status: str) -> None:
        with self._lock:
            run = self._require(run_id)
            run["status"] = status
            run["updated_at"] = _now()
            self._persist(run)

    def _require(self, run_id: str) -> dict[str, Any]:
        if run_id not in self._runs:
            raise KeyError(run_id)
        return self._runs[run_id]

    def _persist(self, run: dict[str, Any]) -> None:
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        path = self.runs_dir / f"{run['run_id']}.json"
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(run, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(temporary, path)

    def _load_runs(self) -> None:
        if not self.runs_dir.exists():
            return
        for path in self.runs_dir.glob("*.json"):
            try:
                run = json.loads(path.read_text(encoding="utf-8"))
                if run.get("status") not in TERMINAL_STATES:
                    run["status"] = "failed"
                    run["error"] = "Application restarted before the run completed"
                    run["updated_at"] = _now()
                self._runs[run["run_id"]] = run
                self._approval_conditions[run["run_id"]] = threading.Condition(
                    self._lock
                )
            except (OSError, ValueError, KeyError):
                continue


run_manager = AgentRunManager()
