"""Airflow 3 tools over the REST API v2 with JWT auth: DAGs, runs, tasks, logs, triggering."""

import re
import time
from typing import Literal
from urllib.parse import quote

import httpx
from pydantic import BaseModel, Field

from agent import config
from agent.tools.base import PermissionDenied, ToolError, tool

_token: dict[str, str | None] = {"value": None}


def _get_token(refresh: bool = False) -> str:
    if not config.AIRFLOW_PASSWORD:
        raise ToolError("Airflow credentials are not configured (AIRFLOW_PASSWORD)")
    if refresh or not _token["value"]:
        r = httpx.post(f"{config.AIRFLOW_API_URL}/auth/token", timeout=config.HTTP_TIMEOUT_S,
                       json={"username": config.AIRFLOW_USERNAME, "password": config.AIRFLOW_PASSWORD})
        if r.status_code >= 400:
            raise ToolError(f"Airflow login failed (HTTP {r.status_code})")
        _token["value"] = r.json()["access_token"]
    return _token["value"]


def api(method: str, path: str, **kwargs) -> dict:
    url = f"{config.AIRFLOW_API_URL}/api/v2{path}"
    try:
        for attempt in (0, 1):  # retry once with a fresh token if it expired
            r = httpx.request(method, url, timeout=config.HTTP_TIMEOUT_S,
                              headers={"Authorization": f"Bearer {_get_token(refresh=attempt == 1)}"},
                              **kwargs)
            if r.status_code != 401:
                break
    except httpx.TimeoutException as e:
        raise ToolError(f"Airflow API timed out ({config.AIRFLOW_API_URL})") from e
    except httpx.HTTPError as e:
        raise ToolError(f"Airflow API unreachable at {config.AIRFLOW_API_URL}: {type(e).__name__}") from e
    if r.status_code == 404:
        raise ToolError(f"Airflow: not found ({path})")
    if r.status_code >= 400:
        raise ToolError(f"Airflow API HTTP {r.status_code}: {r.text[:300]}")
    return r.json()


def _q(s: str) -> str:
    return quote(s, safe="")


def _runs(dag_id: str, limit: int, state: str | None = None) -> list[dict]:
    params = {"order_by": "-run_after", "limit": limit}
    if state:
        params["state"] = state
    return api("GET", f"/dags/{_q(dag_id)}/dagRuns", params=params)["dag_runs"]


def _run_summary(r: dict) -> dict:
    return {k: r.get(k) for k in ("dag_run_id", "state", "run_type", "run_after", "start_date",
                                  "end_date", "duration", "conf")}


def _tasks(dag_id: str, run_id: str) -> list[dict]:
    tis = api("GET", f"/dags/{_q(dag_id)}/dagRuns/{_q(run_id)}/taskInstances")["task_instances"]
    keys = ("task_id", "state", "try_number", "max_tries", "start_date", "end_date", "duration")
    return sorted(({k: t.get(k) for k in keys} for t in tis), key=lambda t: t["start_date"] or "~")


def _log_lines(dag_id: str, run_id: str, task_id: str, try_number: int) -> list[str]:
    content = api("GET", f"/dags/{_q(dag_id)}/dagRuns/{_q(run_id)}/taskInstances/{_q(task_id)}"
                         f"/logs/{try_number}")["content"]
    lines = [c.get("event", "") if isinstance(c, dict) else str(c) for c in content]
    return [ln for ln in lines if ln and not ln.startswith("::")]


def _resolve_run_id(dag_id: str, run_id: str) -> str:
    if run_id != "latest":
        return run_id
    runs = _runs(dag_id, 1)
    if not runs:
        raise ToolError(f"DAG '{dag_id}' has no runs")
    return runs[0]["dag_run_id"]


class DagArgs(BaseModel):
    dag_id: str = "training_pipeline"
    limit: int = Field(5, ge=1, le=25)


class RunArgs(BaseModel):
    dag_id: str = "training_pipeline"
    dag_run_id: str = Field("latest", description="DAG run id, or 'latest'")


class LogArgs(RunArgs):
    task_id: str
    try_number: int | None = Field(None, ge=1, description="Defaults to the latest try")
    tail: int = Field(40, ge=5, le=200)


class TrainingConf(BaseModel):
    model: Literal["logreg", "rf"] = "logreg"
    C: float = Field(1.0, gt=0)
    n_estimators: int = Field(100, ge=1, le=1000)


class TriggerArgs(BaseModel):
    dag_id: str = "training_pipeline"
    conf: TrainingConf = Field(default_factory=TrainingConf)


def _approved_dag(a: TriggerArgs) -> None:
    if a.dag_id not in config.APPROVED_DAGS:
        raise PermissionDenied(f"DAG '{a.dag_id}' is not approved for triggering "
                               f"(approved: {sorted(config.APPROVED_DAGS)})")


@tool("airflow")
def list_dags(_: BaseModel) -> dict:
    """List Airflow DAGs with paused state, tags, import errors and whether the agent may trigger them."""
    dags = api("GET", "/dags")["dags"]
    errors = api("GET", "/importErrors")["import_errors"]
    return {"dags": [{"dag_id": d["dag_id"], "is_paused": d["is_paused"], "tags": [t["name"] for t in d["tags"]],
                      "has_import_errors": d["has_import_errors"],
                      "approved_for_trigger": d["dag_id"] in config.APPROVED_DAGS} for d in dags],
            "import_errors": [{"file": e.get("filename"), "error": e.get("stack_trace", "")[-300:]} for e in errors]}


@tool("airflow", DagArgs)
def list_dag_runs(a: DagArgs) -> dict:
    """List the most recent runs of a DAG with state, timing and the config they ran with."""
    return {"dag_id": a.dag_id, "runs": [_run_summary(r) for r in _runs(a.dag_id, a.limit)]}


@tool("airflow", RunArgs)
def get_dag_run_status(a: RunArgs) -> dict:
    """Status of one DAG run (or the latest) with every task's state, try number and duration."""
    run_id = _resolve_run_id(a.dag_id, a.dag_run_id)
    run = api("GET", f"/dags/{_q(a.dag_id)}/dagRuns/{_q(run_id)}")
    return {**_run_summary(run), "tasks": _tasks(a.dag_id, run_id)}


class WaitArgs(RunArgs):
    max_wait_s: int = Field(150, ge=10, le=300)


@tool("airflow", WaitArgs, timeout=320)
def wait_for_dag_run(a: WaitArgs) -> dict:
    """Wait until a DAG run (or the latest) finishes, polling every 5 s, then return its final state,
    task states and, if it failed, the error lines of the failed tasks. Use this once to report
    whether a triggered run succeeded, instead of polling get_dag_run_status."""
    run_id = _resolve_run_id(a.dag_id, a.dag_run_id)
    deadline = time.monotonic() + a.max_wait_s
    while True:
        run = api("GET", f"/dags/{_q(a.dag_id)}/dagRuns/{_q(run_id)}")
        if run["state"] in ("success", "failed") or time.monotonic() >= deadline:
            break
        time.sleep(5)
    tasks = _tasks(a.dag_id, run_id)
    failures = [{"task_id": t["task_id"], "error_lines": [
        ln for ln in _log_lines(a.dag_id, run_id, t["task_id"], t["try_number"])
        if re.search(r"ERROR|Error|Exception|failed", ln)][-8:]} for t in tasks if t["state"] == "failed"]
    return {**_run_summary(run), "finished": run["state"] in ("success", "failed"),
            "tasks": tasks, "failures": failures}


@tool("airflow", DagArgs)
def find_failed_tasks(a: DagArgs) -> dict:
    """Find failed tasks in recent failed runs of a DAG, with the error lines from their logs and
    which downstream tasks were blocked."""
    out = []
    for run in _runs(a.dag_id, a.limit, state="failed"):
        tasks = _tasks(a.dag_id, run["dag_run_id"])
        for t in (t for t in tasks if t["state"] == "failed"):
            lines = _log_lines(a.dag_id, run["dag_run_id"], t["task_id"], t["try_number"])
            errors = [ln for ln in lines if re.search(r"ERROR|Error|Exception|failed", ln)]
            out.append({"dag_run_id": run["dag_run_id"], "task_id": t["task_id"], "try_number": t["try_number"],
                        "error_lines": errors[-8:],
                        "blocked_downstream": [x["task_id"] for x in tasks if x["state"] == "upstream_failed"]})
    return {"dag_id": a.dag_id, "failed_tasks": out, "checked_runs": a.limit}


@tool("airflow", LogArgs)
def get_task_log(a: LogArgs) -> dict:
    """Last lines of a task's log for one try of a DAG run (default: latest run, latest try)."""
    run_id = _resolve_run_id(a.dag_id, a.dag_run_id)
    try_number = a.try_number or next(
        (t["try_number"] for t in _tasks(a.dag_id, run_id) if t["task_id"] == a.task_id), None)
    if not try_number:
        raise ToolError(f"task '{a.task_id}' has not run in {run_id}")
    lines = _log_lines(a.dag_id, run_id, a.task_id, try_number)
    return {"dag_run_id": run_id, "task_id": a.task_id, "try_number": try_number, "lines": lines[-a.tail:]}


@tool("airflow", TriggerArgs, mutating=True, precheck=_approved_dag)
def trigger_dag(a: TriggerArgs) -> dict:
    """Trigger an approved training DAG with a model config. Changes state: requires explicit user
    confirmation. Returns the new run id; then call wait_for_dag_run once for the result."""
    r = api("POST", f"/dags/{_q(a.dag_id)}/dagRuns", json={"logical_date": None, "conf": a.conf.model_dump()})
    return _run_summary(r)
