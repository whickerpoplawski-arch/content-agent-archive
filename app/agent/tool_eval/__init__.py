"""Tool-calling eval cases, the M0 bare baseline, and the D.6 rubric.

Gate assertion runners (``assertions.py`` / ``eval_tool_calling.py``) are a
later change. This package loads the frozen cases and can score a no-tool baseline.
"""

from app.agent.tool_eval.case import (
    ToolEvalCase,
    file_sha256,
    load_cases,
    verify_sha256,
)

__all__ = ["ToolEvalCase", "file_sha256", "load_cases", "verify_sha256"]
