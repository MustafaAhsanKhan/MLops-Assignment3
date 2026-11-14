"""HTTP interface to the MLOps agent.

    uvicorn api.main:app --port 8000

POST /chat {message, session_id?, confirm_action_ids?} -> answer, tool trace, pending actions.
State-changing actions are only executed when their id is sent back in confirm_action_ids.
"""

import json
import logging

from fastapi import FastAPI, HTTPException

from agent.orchestrator import Agent
from agent.schemas import ChatRequest, ChatResponse
from agent.tools import REGISTRY

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("api")
app = FastAPI(title="MLOps Agent", version="1.0.0")
agent = Agent()


@app.get("/health")
def health():
    return {"status": "ok", "model": agent.model, "tools": len(REGISTRY)}


@app.get("/tools")
def tools():
    return [{"name": t.name, "module": t.module, "mutating": t.mutating, "description": t.description}
            for t in REGISTRY.values()]


@app.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest) -> ChatResponse:
    try:
        return agent.chat(req.message, req.session_id, req.confirm_action_ids)
    except Exception as e:  # LLM/API failure: report it, never improvise an answer
        log.error(json.dumps({"event": "chat_failed", "error_type": type(e).__name__,
                              "code": getattr(e, "code", None)}))
        raise HTTPException(502, f"The language model call failed ({type(e).__name__}); "
                                 "no answer was generated.") from e
