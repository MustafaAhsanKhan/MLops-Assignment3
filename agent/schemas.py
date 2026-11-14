"""Structured inputs and outputs shared by the tools, the orchestrator and the API."""

from typing import Any, Literal

from pydantic import BaseModel, Field

ToolStatus = Literal["ok", "error", "timeout", "denied", "needs_confirmation"]


class ToolResult(BaseModel):
    """What every tool call returns. `data` is only set when status == "ok"."""

    tool: str
    status: ToolStatus
    data: Any = None
    error: str | None = None
    duration_ms: float


class PendingAction(BaseModel):
    """A state-changing call the agent proposed; it runs only if the user confirms this id."""

    action_id: str
    tool: str
    args: dict


class ToolTrace(BaseModel):
    tool: str
    args: dict
    status: ToolStatus
    duration_ms: float
    error: str | None = None


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    session_id: str | None = Field(None, description="Continue a conversation")
    confirm_action_ids: list[str] = Field(default_factory=list,
                                          description="Pending actions the user explicitly approves")


class ChatResponse(BaseModel):
    request_id: str
    session_id: str
    answer: str
    tool_calls: list[ToolTrace]
    pending_actions: list[PendingAction]
