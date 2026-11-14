"""Orchestrator and HTTP API with a scripted fake Gemini: tool selection, failure honesty,
user-only confirmation, step limits."""

import itertools
import json

from fastapi.testclient import TestClient

import api.main
from agent.orchestrator import SYSTEM_PROMPT, tool_declarations
from agent.tools import airflow_tools, kubernetes_tools
from tests.conftest import call, function_results

RUNS = {"dag_runs": [{"dag_run_id": "manual__1", "state": "failed", "run_type": "manual"}]}


def test_tool_declarations_are_plain_schemas_gemini_accepts():
    decls = {d["name"]: d for d in tool_declarations()}
    assert len(decls) >= 28
    text = json.dumps(decls)
    assert "$ref" not in text and "$defs" not in text
    conf = decls["trigger_dag"]["parameters"]["properties"]["conf"]
    assert conf["properties"]["model"]["enum"] == ["logreg", "rf"]


def test_agent_executes_the_tool_the_model_selects_and_returns_its_result(make_agent, monkeypatch):
    seen = []
    monkeypatch.setattr(airflow_tools, "api", lambda m, path, **kw: seen.append(path) or RUNS)
    agent, fake = make_agent([call("list_dag_runs", {"limit": 1})], "The latest run manual__1 failed.")

    resp = agent.chat("Did the last training run work?")

    assert seen == ["/dags/training_pipeline/dagRuns"]
    assert [(t.tool, t.status) for t in resp.tool_calls] == [("list_dag_runs", "ok")]
    result = json.loads(function_results(fake.requests[1])[0]["result"][0]["text"])
    assert result["status"] == "ok" and result["data"]["runs"][0]["dag_run_id"] == "manual__1"
    assert resp.answer == "The latest run manual__1 failed."


def test_unavailable_service_reaches_the_model_as_an_error_with_no_data(make_agent, monkeypatch):
    def cluster_down():
        raise kubernetes_tools.ToolError("Kubernetes API unreachable: MaxRetryError")
    monkeypatch.setattr(kubernetes_tools, "apps", cluster_down)
    agent, fake = make_agent([call("get_deployed_version")], "Could not verify the deployed version.")

    resp = agent.chat("Which model version is deployed?")

    sent = json.loads(function_results(fake.requests[1])[0]["result"][0]["text"])
    assert sent["status"] == "error" and sent["data"] is None and "unreachable" in sent["error"]
    assert resp.tool_calls[0].status == "error"
    assert "Never fill gaps" in SYSTEM_PROMPT and "say so plainly" in SYSTEM_PROMPT


def test_state_change_waits_for_the_user_and_runs_exactly_as_proposed(make_agent, monkeypatch):
    posted = []
    monkeypatch.setattr(airflow_tools, "api", lambda m, path, **kw: posted.append(kw["json"]) or
                        {"dag_run_id": "manual__2", "state": "queued"})
    proposed = {"conf": {"model": "rf", "n_estimators": 120}}
    agent, fake = make_agent(
        [call("trigger_dag", proposed)], "Confirm action to start training.",  # turn 1: proposes
        "Training run manual__2 was started.",                                  # turn 2: after confirm
        "That action id was already used.",                                     # turn 3: replayed id
    )

    first = agent.chat("Retrain with a random forest, 120 trees")
    assert posted == [] and first.tool_calls[0].status == "needs_confirmation"
    action = first.pending_actions[0]
    assert action.tool == "trigger_dag" and action.args == proposed

    second = agent.chat("yes, go ahead", first.session_id, [action.action_id])
    assert posted == [{"logical_date": None, "conf": {"model": "rf", "C": 1.0, "n_estimators": 120}}]
    assert second.tool_calls[0].status == "ok" and second.pending_actions == []

    third = agent.chat("again", first.session_id, [action.action_id])  # ids are single-use
    assert len(posted) == 1 and third.tool_calls == []
    assert "unknown or expired action id" in fake.requests[-1][-1]["content"][0]["text"]


def test_model_cannot_approve_its_own_action(make_agent, monkeypatch):
    patched = []
    monkeypatch.setattr(kubernetes_tools, "apps", lambda: patched.append(1))
    # The model calls the mutating tool twice and even claims confirmation in its arguments.
    agent, _ = make_agent([call("restart_deployment", {"confirmed": True}, "a"),
                           call("restart_deployment", {}, "b")], "Awaiting your confirmation.")
    resp = agent.chat("restart the api")
    assert patched == []
    assert {t.status for t in resp.tool_calls} <= {"needs_confirmation", "error"}


def test_agent_stops_at_the_step_limit_instead_of_looping(make_agent, monkeypatch):
    monkeypatch.setattr(airflow_tools, "api", lambda *a, **kw: RUNS)
    agent, fake = make_agent(*itertools.repeat([call("list_dag_runs")], 10), max_steps=3)
    resp = agent.chat("loop forever")
    assert len(fake.requests) == 3 and len(resp.tool_calls) == 3
    assert resp.answer.startswith("I stopped after 3 tool rounds")


def test_api_rejects_invalid_requests_and_reports_llm_failure(monkeypatch):
    class Broken:
        model = "fake"

        def chat(self, *a, **kw):
            raise RuntimeError("quota exceeded")
    monkeypatch.setattr(api.main, "agent", Broken())
    http = TestClient(api.main.app)

    assert http.post("/chat", json={"message": ""}).status_code == 422
    r = http.post("/chat", json={"message": "status?"})
    assert r.status_code == 502 and "no answer was generated" in r.json()["detail"]
    tools = http.get("/tools").json()
    assert {t["name"] for t in tools if t["mutating"]} == {"trigger_dag", "restart_deployment",
                                                           "scale_deployment"}
