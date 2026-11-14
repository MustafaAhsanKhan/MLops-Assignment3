"""run_tool safety path: validation, permissions, confirmation, timeouts, service failures, logs."""

import json
import logging
import time

import httpx
import pytest

from agent import config
from agent.tools import REGISTRY, airflow_tools, dvc_tools, kubernetes_tools, mlflow_tools, run_tool
from agent.tools.base import NoArgs, PermissionDenied, Tool


class FakeApps:
    """Records Kubernetes AppsV1 calls instead of making them."""

    def __init__(self):
        self.calls = []

    def patch_namespaced_deployment_scale(self, name, namespace, body, **kw):
        self.calls.append(("scale", name, namespace, body))

    def patch_namespaced_deployment(self, name, namespace, body, **kw):
        self.calls.append(("patch", name, namespace, body))


@pytest.fixture
def fake_apps(monkeypatch):
    fake = FakeApps()
    monkeypatch.setattr(kubernetes_tools, "apps", lambda: fake)
    return fake


def test_registry_has_all_four_systems_and_only_three_mutating_tools():
    assert {t.module for t in REGISTRY.values()} == {"dvc", "mlflow", "airflow", "kubernetes"}
    assert {n for n, t in REGISTRY.items() if t.mutating} == {"trigger_dag", "restart_deployment",
                                                               "scale_deployment"}


def test_unknown_tool_is_an_error_not_a_crash():
    r = run_tool("delete_everything", {}, "t")
    assert r.status == "error" and "unknown tool" in r.error and r.data is None


def test_invalid_arguments_are_rejected_before_anything_runs(fake_apps):
    r = run_tool("scale_deployment", {"replicas": 50}, "t", confirmed=True)
    assert r.status == "error" and r.error.startswith("invalid arguments: replicas")
    assert fake_apps.calls == []


@pytest.mark.parametrize("tool_name,args", [
    ("restart_deployment", {"namespace": "kube-system"}),
    ("scale_deployment", {"namespace": "kube-system", "replicas": 2}),
    ("get_pod_logs", {"namespace": "kube-system", "pod": "coredns"}),
])
def test_kubernetes_outside_allowed_namespace_is_denied_even_when_confirmed(fake_apps, tool_name, args):
    r = run_tool(tool_name, args, "t", confirmed=True)
    assert r.status == "denied" and "kube-system" in r.error
    assert fake_apps.calls == []


def test_mutating_tool_needs_confirmation_then_runs_with_given_args(fake_apps):
    args = {"deployment": "prediction-api", "replicas": 3}
    r = run_tool("scale_deployment", args, "t")
    assert r.status == "needs_confirmation" and fake_apps.calls == []

    r = run_tool("scale_deployment", args, "t", confirmed=True)
    assert r.status == "ok"
    assert fake_apps.calls == [("scale", "prediction-api", config.K8S_NAMESPACE, {"spec": {"replicas": 3}})]


def test_trigger_dag_only_for_approved_dags(monkeypatch):
    posted = []
    monkeypatch.setattr(airflow_tools, "api", lambda *a, **kw: posted.append((a, kw)) or {"dag_run_id": "x"})
    r = run_tool("trigger_dag", {"dag_id": "drop_database"}, "t", confirmed=True)
    assert r.status == "denied" and posted == []

    r = run_tool("trigger_dag", {"conf": {"model": "rf", "n_estimators": 50}}, "t", confirmed=True)
    assert r.status == "ok"
    (method, path), kw = posted[0]
    assert (method, path) == ("POST", "/dags/training_pipeline/dagRuns")
    assert kw["json"]["conf"] == {"model": "rf", "C": 1.0, "n_estimators": 50}


def test_slow_tool_times_out(monkeypatch):
    def slow(_):
        time.sleep(1)
        return {}
    monkeypatch.setitem(REGISTRY, "slow_tool", Tool("slow_tool", "test", "", NoArgs, slow, False, None))
    r = run_tool("slow_tool", {}, "t", timeout=0.1)
    assert r.status == "timeout" and r.data is None and "0.1s" in r.error


def test_airflow_unreachable_is_reported(monkeypatch):
    def refuse(*a, **kw):
        raise httpx.ConnectError("connection refused")
    monkeypatch.setattr(config, "AIRFLOW_PASSWORD", "x")
    monkeypatch.setattr(airflow_tools.httpx, "post", refuse)
    monkeypatch.setattr(airflow_tools.httpx, "request", refuse)
    monkeypatch.setitem(airflow_tools._token, "value", None)
    r = run_tool("list_dag_runs", {}, "t")
    assert r.status == "error" and "unreachable" in r.error and r.data is None


def test_mlflow_unreachable_is_reported(monkeypatch):
    class DownClient:
        def __getattr__(self, name):
            def fail(*a, **kw):
                raise ConnectionError("refused")
            return fail
    monkeypatch.setattr(mlflow_tools, "client", DownClient)
    r = run_tool("list_experiments", {}, "t")
    assert r.status == "error" and "MLflow unreachable" in r.error and r.data is None


def test_dvc_tools_only_run_allowlisted_read_only_commands():
    for cmd in [("git", "push"), ("dvc", "gc"), ("rm", "-rf")]:
        with pytest.raises(PermissionDenied):
            dvc_tools.run_cmd(*cmd)
    r = run_tool("get_dataset_version", {"path": "../../etc/passwd"}, "t")
    assert r.status == "error" and "inside" in r.error


def test_every_call_writes_one_structured_log_line_with_secrets_redacted(caplog, fake_apps):
    caplog.set_level(logging.INFO, logger="agent.tools")
    run_tool("scale_deployment", {"replicas": 2, "api_token": "s3cret"}, "req-42")
    lines = [json.loads(r.message) for r in caplog.records if r.name == "agent.tools"]
    assert len(lines) == 1
    line = lines[0]
    assert line["event"] == "tool_call" and line["request_id"] == "req-42"
    assert line["tool"] == "scale_deployment" and line["status"] == "needs_confirmation"
    assert line["args"] == {"replicas": 2, "api_token": "***"}
    assert "s3cret" not in caplog.text
