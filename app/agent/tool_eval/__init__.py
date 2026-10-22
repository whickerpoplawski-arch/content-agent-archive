"""Frozen tool-calling eval cases.

Assertion runners and the Gate script are a later change. This package only
loads and validates the case file.
"""

from app.agent.tool_eval.case import (
    ToolEvalCase,
    file_sha256,
    load_cases,
    verify_sha256,
)

__all__ = ["ToolEvalCase", "file_sha256", "load_cases", "verify_sha256"]
