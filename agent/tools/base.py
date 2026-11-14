"""Tool registry and `run_tool`, the single path every tool call takes:
validate arguments -> permission check -> confirmation for state changes -> run with timeout
-> capture errors -> one structured log line (secrets redacted).

Confirmation is passed in by the caller from the *user's* request, never from model output,
so the LLM cannot approve its own state-changing actions.
"""

import json
import logging
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass

from pydantic import BaseModel, ValidationError

from agent import config
from agent.schemas import ToolResult

log = logging.getLogger("agent.tools")
# shortcut: a timed-out call keeps running in its thread until it returns; fine for read-only
# calls with their own HTTP timeouts, revisit if tools ever do long writes.
_pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="tool")
_SECRET_WORDS = ("password", "token", "secret", "key", "authorization")


class ToolError(Exception):
    """Expected failure with a message safe to show the user (service down, not found...)."""


class PermissionDenied(ToolError):
    """The request is outside what the agent is allowed to do."""


class NoArgs(BaseModel):
    pass


@dataclass(frozen=True)
class Tool:
    name: str
    module: str
    description: str
    input_model: type[BaseModel]
    fn: Callable[[BaseModel], dict]
    mutating: bool
    precheck: Callable[[BaseModel], None] | None
    timeout: float | None = None  # overrides config.TOOL_TIMEOUT_S, e.g. for waiting tools


REGISTRY: dict[str, Tool] = {}


def tool(module: str, input_model: type[BaseModel] = NoArgs, *, mutating: bool = False,
         precheck: Callable[[BaseModel], None] | None = None, timeout: float | None = None):
    """Register a function as an agent tool. Its docstring is the description the LLM sees."""

    def wrap(fn):
        REGISTRY[fn.__name__] = Tool(fn.__name__, module, " ".join((fn.__doc__ or "").split()),
                                     input_model, fn, mutating, precheck, timeout)
        return fn

    return wrap


def redact(obj):
    if isinstance(obj, dict):
        return {k: "***" if any(w in k.lower() for w in _SECRET_WORDS) else redact(v)
                for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact(v) for v in obj]
    return obj


def run_tool(name: str, args: dict | None, request_id: str, *, confirmed: bool = False,
             timeout: float | None = None) -> ToolResult:
    start = time.perf_counter()
    status, data, error = "error", None, None
    t = REGISTRY.get(name)
    timeout = timeout or (t.timeout if t else None) or config.TOOL_TIMEOUT_S
    try:
        if t is None:
            raise ToolError(f"unknown tool '{name}'")
        params = t.input_model.model_validate(args or {})
        if t.precheck:
            t.precheck(params)
        if t.mutating and not confirmed:
            status = "needs_confirmation"
            error = f"{name} changes system state; it runs only after the user explicitly confirms."
        else:
            data = _pool.submit(t.fn, params).result(timeout=timeout)
            status = "ok"
    except ValidationError as e:
        error = "invalid arguments: " + "; ".join(
            f"{'.'.join(map(str, x['loc'])) or 'args'}: {x['msg']}" for x in e.errors())
    except FutureTimeout:
        status, error = "timeout", f"no response within {timeout:g}s"
    except PermissionDenied as e:
        status, error = "denied", str(e)
    except ToolError as e:
        error = str(e)
    except Exception as e:  # unexpected: report the type, never crash the agent
        error = f"{type(e).__name__}: {e}"[:500]

    result = ToolResult(tool=name, status=status, data=data, error=error,
                        duration_ms=round((time.perf_counter() - start) * 1000, 1))
    log.info(json.dumps({"event": "tool_call", "request_id": request_id, "tool": name,
                         "status": status, "duration_ms": result.duration_ms,
                         "args": redact(args or {}), "error": error}))
    return result
