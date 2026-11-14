"""Structured inputs and outputs shared by the tools, the orchestrator and the API."""

from typing import Any, Literal

from pydantic import BaseModel

ToolStatus = Literal["ok", "error", "timeout", "denied", "needs_confirmation"]


class ToolResult(BaseModel):
    """What every tool call returns. `data` is only set when status == "ok"."""

    tool: str
    status: ToolStatus
    data: Any = None
    error: str | None = None
    duration_ms: float
