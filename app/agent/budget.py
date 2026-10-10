"""Budget: caps an Agent run so it cannot runaway on cost or steps.

A self-regulating agent stops itself once its LLM-call count, estimated token
usage, or total executed steps exceed configured limits (0 = unlimited).
"""

from __future__ import annotations

from typing import Any, Optional


class Budget:
    def __init__(
        self,
        max_llm_calls: Optional[int] = None,
        max_est_tokens: Optional[int] = None,
        max_steps: Optional[int] = None,
        max_replans: int = 0,
    ) -> None:
        self.max_llm_calls = max_llm_calls or 0
        self.max_est_tokens = max_est_tokens or 0
        self.max_steps = max_steps or 0
        self.max_replans = max_replans or 0

    @classmethod
    def from_settings(cls, settings) -> "Budget":
        return cls(
            max_llm_calls=settings.agent_max_llm_calls or 0,
            max_est_tokens=settings.agent_max_est_tokens or 0,
            max_steps=settings.agent_max_steps or 0,
            max_replans=settings.agent_max_replans or 0,
        )

    def exceeded(self, usage: dict[str, Any], steps: int, replans: int = 0) -> bool:
        if self.max_llm_calls and usage.get("calls", 0) > self.max_llm_calls:
            return True
        if self.max_est_tokens and usage.get("est_tokens", 0) > self.max_est_tokens:
            return True
        if self.max_steps and steps > self.max_steps:
            return True
        if self.max_replans and replans > self.max_replans:
            return True
        return False

    def reason(self, usage: dict[str, Any], steps: int, replans: int = 0) -> str:
        if self.max_llm_calls and usage.get("calls", 0) > self.max_llm_calls:
            return f"llm_calls {usage.get('calls')} > {self.max_llm_calls}"
        if self.max_est_tokens and usage.get("est_tokens", 0) > self.max_est_tokens:
            return f"est_tokens {usage.get('est_tokens')} > {self.max_est_tokens}"
        if self.max_steps and steps > self.max_steps:
            return f"steps {steps} > {self.max_steps}"
        if self.max_replans and replans > self.max_replans:
            return f"replan_oscillation replans {replans} > {self.max_replans}"
        return ""
