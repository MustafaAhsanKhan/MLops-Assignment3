"""The agent loop: Gemini picks tools, run_tool executes them, results go back until it answers.

- Stateless Interactions API (store=False): history lives here, tool output is not stored by Google.
- State-changing tools never run on the model's say-so. An unconfirmed call becomes a
  PendingAction; only when the user confirms its id does it run, with exactly the proposed args.
"""

import json
import logging
import time
import uuid
from dataclasses import dataclass, field

from google import genai
from pydantic import BaseModel

from agent import config
from agent.schemas import ChatResponse, PendingAction, ToolTrace
from agent.tools import REGISTRY, run_tool

log = logging.getLogger("agent.orchestrator")
MAX_RESULT_CHARS = 12000

SYSTEM_PROMPT = f"""You are an MLOps operations agent for a breast-cancer classifier. The system uses
DVC (dataset versions), MLflow (runs, metrics, model registry), Apache Airflow 3 (training DAG
`training_pipeline`: validate -> preprocess -> train -> evaluate/MLflow -> quality gate) and
Kubernetes (FastAPI prediction service `prediction-api` in namespace `{config.K8S_NAMESPACE}`).

Rules:
1. Every operational fact you state (versions, metrics, run states, health, logs, commits) must come
   from a tool result in this conversation. Never fill gaps from general knowledge or guess values.
2. If a tool fails, times out or is denied, say so plainly and state what could not be verified.
   Do not present unverified information as fact.
3. Call as many tools as the question needs; prefer several focused calls over assumptions. For
   questions about overall state, deployment readiness or retraining, gather evidence from all four
   systems: MLflow (runs, registry), DVC (dataset versions), Kubernetes (deployed version, health)
   and Airflow (recent training runs and failed tasks). Report any caveats a tool returns.
   To find out whether a DAG run succeeded, call wait_for_dag_run once; do not poll.
4. Only call trigger_dag, restart_deployment or scale_deployment when the user explicitly asks for
   that action. Never start retraining because of a status or diagnosis question. These tools
   return needs_confirmation with a pending action id; then tell the user exactly what would run
   and that they must confirm it.
5. Recommend retraining only with evidence: a failed quality gate, the deployed model trained on an
   older dataset version than the newest one, a registered model older than the freshness policy
   ({config.MODEL_MAX_AGE_DAYS} days), or degradation results the user supplied. Otherwise say the
   evidence does not indicate retraining.
6. The best metric alone does not make a model production-ready: also weigh the quality gate,
   dataset version, registry status and what is deployed.
7. Warning events for pods that no longer exist describe the past, not the current state.

Answer in Markdown with three sections: **Findings** (facts, each naming the tool that showed
it), **Uncertainties**, **Recommended actions**. Be concise."""


def _inline_schema(model: type[BaseModel]) -> dict:
    """Pydantic JSON schema -> plain schema for Gemini: inline $refs, drop titles, unwrap Optional."""
    schema = model.model_json_schema()
    defs = schema.pop("$defs", {})

    def fix(node):
        if isinstance(node, list):
            return [fix(x) for x in node]
        if not isinstance(node, dict):
            return node
        if "$ref" in node:
            node = {**defs[node["$ref"].split("/")[-1]], **{k: v for k, v in node.items() if k != "$ref"}}
        node = {k: fix(v) for k, v in node.items() if k != "title"}
        if "anyOf" in node:
            options = [x for x in node["anyOf"] if x.get("type") != "null"]
            if len(options) == 1:
                node = {**options[0], **{k: v for k, v in node.items() if k != "anyOf"}}
        return node

    return fix(schema)


def tool_declarations() -> list[dict]:
    return [{"type": "function", "name": t.name, "description": t.description,
             "parameters": _inline_schema(t.input_model)} for t in REGISTRY.values()]


def _text(text: str) -> dict:
    return {"type": "user_input", "content": [{"type": "text", "text": text}]}


@dataclass
class Session:
    history: list[dict] = field(default_factory=list)
    pending: dict[str, PendingAction] = field(default_factory=dict)


class Agent:
    def __init__(self, client=None, model: str = config.GEMINI_MODEL, max_steps: int = config.AGENT_MAX_STEPS):
        self._client = client
        self.model = model
        self.max_steps = max_steps
        self.sessions: dict[str, Session] = {}

    @property
    def client(self):
        if self._client is None:
            self._client = genai.Client()  # reads GEMINI_API_KEY
        return self._client

    def chat(self, message: str, session_id: str | None = None,
             confirm_action_ids: list[str] | None = None) -> ChatResponse:
        request_id = uuid.uuid4().hex[:12]
        session_id = session_id or uuid.uuid4().hex[:12]
        session = self.sessions.setdefault(session_id, Session())
        start = time.perf_counter()
        trace: list[ToolTrace] = []

        # 1. Run actions the user explicitly confirmed, exactly as they were proposed.
        notes = []
        for action_id in confirm_action_ids or []:
            action = session.pending.pop(action_id, None)
            if action is None:
                notes.append(f"Confirmation for unknown or expired action id '{action_id}' was ignored.")
                continue
            r = run_tool(action.tool, action.args, request_id, confirmed=True)
            trace.append(ToolTrace(tool=r.tool, args=action.args, status=r.status,
                                   duration_ms=r.duration_ms, error=r.error))
            notes.append(f"The user confirmed action {action_id}; it was executed: {action.tool}"
                         f"({json.dumps(action.args)}) -> {json.dumps(r.model_dump(), default=str)}")
        text = ("[system note] " + " ".join(notes) + "\n\n" + message) if notes else message
        session.history.append(_text(text))

        # 2. Let the model call tools until it answers.
        answer = None
        for _ in range(self.max_steps):
            interaction = self.client.interactions.create(
                model=self.model, store=False, input=session.history,
                tools=tool_declarations(), system_instruction=SYSTEM_PROMPT)
            session.history.extend(s.model_dump(exclude_none=True) for s in interaction.steps)
            calls = [s for s in interaction.steps if s.type == "function_call"]
            if not calls:
                answer = interaction.output_text or "(the model returned no text)"
                break
            for call in calls:
                args = dict(call.arguments or {})
                r = run_tool(call.name, args, request_id)
                payload = r.model_dump()
                if r.status == "needs_confirmation":
                    action = PendingAction(action_id=uuid.uuid4().hex[:8], tool=call.name, args=args)
                    session.pending[action.action_id] = action
                    payload["pending_action_id"] = action.action_id
                trace.append(ToolTrace(tool=call.name, args=args, status=r.status,
                                       duration_ms=r.duration_ms, error=r.error))
                session.history.append({
                    "type": "function_result", "name": call.name, "call_id": call.id,
                    "result": [{"type": "text", "text": json.dumps(payload, default=str)[:MAX_RESULT_CHARS]}],
                })
        if answer is None:
            answer = (f"I stopped after {self.max_steps} tool rounds without reaching a conclusion. "
                      "The tool calls made so far are listed; please narrow the question.")

        log.info(json.dumps({"event": "chat", "request_id": request_id, "session_id": session_id,
                             "tools": [t.tool for t in trace], "statuses": [t.status for t in trace],
                             "pending": list(session.pending),
                             "duration_ms": round((time.perf_counter() - start) * 1000, 1)}))
        return ChatResponse(request_id=request_id, session_id=session_id, answer=answer,
                            tool_calls=trace, pending_actions=list(session.pending.values()))
