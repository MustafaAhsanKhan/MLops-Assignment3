"""Kubernetes tools for the prediction service: workloads, logs, services, health, events, version.

Every tool is limited to config.ALLOWED_NAMESPACES. restart/scale change cluster state and only
run after explicit user confirmation.
"""

import json
from datetime import UTC, datetime

import urllib3
from kubernetes import client as k8s
from kubernetes import config as kube_config
from kubernetes.client.rest import ApiException
from pydantic import BaseModel, Field

from agent import config
from agent.tools.base import PermissionDenied, ToolError, tool

_loaded = {"ok": False}
TIMEOUT = config.HTTP_TIMEOUT_S


def _load():
    if not _loaded["ok"]:
        try:
            kube_config.load_kube_config()
        except Exception as e:
            raise ToolError(f"no usable kubeconfig: {e}") from e
        _loaded["ok"] = True


def core() -> k8s.CoreV1Api:
    _load()
    return k8s.CoreV1Api()


def apps() -> k8s.AppsV1Api:
    _load()
    return k8s.AppsV1Api()


def _call(fn, *args, **kwargs):
    try:
        return fn(*args, _request_timeout=TIMEOUT, **kwargs)
    except ApiException as e:
        raise ToolError(f"Kubernetes API {e.status} {e.reason}: {str(e.body)[:200]}") from e
    except urllib3.exceptions.HTTPError as e:
        raise ToolError(f"Kubernetes API unreachable: {type(e).__name__}") from e


def _iso(ts) -> str | None:
    return ts.isoformat(timespec="seconds") if ts else None


class NsArgs(BaseModel):
    namespace: str = config.K8S_NAMESPACE


class LogArgs(NsArgs):
    pod: str
    tail_lines: int = Field(50, ge=1, le=500)
    previous: bool = Field(False, description="Logs of the previous (crashed) container")


class DeploymentArgs(NsArgs):
    deployment: str = "prediction-api"


class ScaleArgs(DeploymentArgs):
    replicas: int = Field(ge=1, le=5)


class EventArgs(NsArgs):
    warnings_only: bool = True
    limit: int = Field(20, ge=1, le=100)


def allowed_namespace(a: NsArgs) -> None:
    if a.namespace not in config.ALLOWED_NAMESPACES:
        raise PermissionDenied(f"namespace '{a.namespace}' is not allowed "
                               f"(allowed: {sorted(config.ALLOWED_NAMESPACES)})")


def _pod_summary(p) -> dict:
    containers = []
    for cs in p.status.container_statuses or []:
        waiting = cs.state.waiting if cs.state else None
        last = cs.last_state.terminated if cs.last_state else None
        containers.append({"name": cs.name, "ready": cs.ready, "restarts": cs.restart_count, "image": cs.image,
                           "waiting_reason": waiting.reason if waiting else None,
                           "waiting_message": (waiting.message or "")[:200] if waiting else None,
                           "last_termination": {"reason": last.reason, "exit_code": last.exit_code,
                                                "finished": _iso(last.finished_at)} if last else None})
    return {"name": p.metadata.name, "phase": p.status.phase, "node": p.spec.node_name,
            "started": _iso(p.status.start_time), "version": (p.metadata.labels or {}).get("app.kubernetes.io/version"),
            "ready": bool(containers) and all(c["ready"] for c in containers), "containers": containers}


def _deployment_summary(d) -> dict:
    s = d.status
    return {"name": d.metadata.name, "image": d.spec.template.spec.containers[0].image,
            "version": (d.metadata.labels or {}).get("app.kubernetes.io/version"),
            "revision": (d.metadata.annotations or {}).get("deployment.kubernetes.io/revision"),
            "replicas": {"desired": d.spec.replicas, "updated": s.updated_replicas or 0,
                         "ready": s.ready_replicas or 0, "available": s.available_replicas or 0,
                         "unavailable": s.unavailable_replicas or 0},
            "conditions": [{"type": c.type, "status": c.status, "reason": c.reason, "message": c.message}
                           for c in s.conditions or []]}


def _events(namespace: str, warnings_only: bool, limit: int) -> list[dict]:
    selector = "type=Warning" if warnings_only else None
    items = _call(core().list_namespaced_event, namespace, field_selector=selector).items
    def when(e):
        return e.last_timestamp or e.event_time or e.metadata.creation_timestamp
    items = sorted(items, key=lambda e: when(e) or datetime.min.replace(tzinfo=UTC))[-limit:]
    return [{"time": _iso(when(e)), "type": e.type, "reason": e.reason, "count": e.count,
             "object": f"{e.involved_object.kind}/{e.involved_object.name}", "message": (e.message or "")[:300]}
            for e in items]


@tool("kubernetes", NsArgs, precheck=allowed_namespace)
def list_workloads(a: NsArgs) -> dict:
    """List Deployments (desired/ready/updated replicas, image, version, conditions) and Pods
    (phase, readiness, restarts, waiting reasons) in the application namespace."""
    deps = _call(apps().list_namespaced_deployment, a.namespace).items
    pods = _call(core().list_namespaced_pod, a.namespace).items
    return {"namespace": a.namespace, "deployments": [_deployment_summary(d) for d in deps],
            "pods": [_pod_summary(p) for p in pods]}


@tool("kubernetes", LogArgs, precheck=allowed_namespace)
def get_pod_logs(a: LogArgs) -> dict:
    """Last log lines of a pod (use previous=true for the container that crashed before a restart)."""
    text = _call(core().read_namespaced_pod_log, a.pod, a.namespace, tail_lines=a.tail_lines,
                 previous=a.previous)
    return {"pod": a.pod, "previous": a.previous, "lines": text.splitlines()}


@tool("kubernetes", NsArgs, precheck=allowed_namespace)
def inspect_services(a: NsArgs) -> dict:
    """List Services with type, ports and how many ready/not-ready endpoints back each one."""
    out = []
    for s in _call(core().list_namespaced_service, a.namespace).items:
        slices = _call(k8s.DiscoveryV1Api().list_namespaced_endpoint_slice, a.namespace,
                       label_selector=f"kubernetes.io/service-name={s.metadata.name}").items
        eps = [e for sl in slices for e in (sl.endpoints or [])]
        ready = [e.addresses[0] for e in eps if e.conditions and e.conditions.ready]
        out.append({"name": s.metadata.name, "type": s.spec.type, "cluster_ip": s.spec.cluster_ip,
                    "ports": [{"port": p.port, "target_port": str(p.target_port), "name": p.name}
                              for p in s.spec.ports or []],
                    "selector": s.spec.selector, "ready_endpoints": ready,
                    "not_ready_endpoints": len(eps) - len(ready)})
    return {"namespace": a.namespace, "services": out}


@tool("kubernetes", EventArgs, precheck=allowed_namespace)
def get_events(a: EventArgs) -> dict:
    """Recent namespace events (default: warnings only), e.g. failed pulls, probe failures, back-offs."""
    return {"namespace": a.namespace, "events": _events(a.namespace, a.warnings_only, a.limit)}


@tool("kubernetes", NsArgs, precheck=allowed_namespace)
def find_unhealthy_pods(a: NsArgs) -> dict:
    """Health check of the namespace: pods that are not ready, waiting (e.g. ImagePullBackOff,
    CrashLoopBackOff) or have restarted, deployments with a stalled rollout, and warning events."""
    pods = [_pod_summary(p) for p in _call(core().list_namespaced_pod, a.namespace).items]
    unhealthy = [p for p in pods if not p["ready"] or p["phase"] != "Running"
                 or any(c["restarts"] or c["waiting_reason"] for c in p["containers"])]
    deps = [_deployment_summary(d) for d in _call(apps().list_namespaced_deployment, a.namespace).items]
    degraded = [d for d in deps if d["replicas"]["available"] < d["replicas"]["desired"]
                or d["replicas"]["updated"] < d["replicas"]["desired"]]
    return {"namespace": a.namespace, "healthy": not unhealthy and not degraded,
            "total_pods": len(pods), "unhealthy_pods": unhealthy, "degraded_deployments": degraded,
            "warning_events": _events(a.namespace, True, 15)}


@tool("kubernetes", DeploymentArgs, precheck=allowed_namespace)
def get_deployed_version(a: DeploymentArgs) -> dict:
    """Currently deployed application version: image tag, version label, rollout revision, and the
    model version/provenance reported live by the service's /version endpoint."""
    d = _deployment_summary(_call(apps().read_namespaced_deployment, a.deployment, a.namespace))
    live, live_error = None, None
    try:
        resp = _call(core().connect_get_namespaced_service_proxy_with_path, f"{a.deployment}:http",
                     a.namespace, "version", _preload_content=False)
        live = json.loads(resp.data)
    except Exception as e:  # service down is a finding, not a tool failure
        live_error = str(e)[:200]
    return {"deployment": d["name"], "image": d["image"], "version_label": d["version"],
            "revision": d["revision"], "replicas": d["replicas"], "live_version": live,
            "live_version_error": live_error}


@tool("kubernetes", DeploymentArgs, mutating=True, precheck=allowed_namespace)
def restart_deployment(a: DeploymentArgs) -> dict:
    """Rolling restart of a deployment (like `kubectl rollout restart`). Changes cluster state:
    requires explicit user confirmation."""
    now = datetime.now(UTC).isoformat(timespec="seconds")
    body = {"spec": {"template": {"metadata": {"annotations": {"kubectl.kubernetes.io/restartedAt": now}}}}}
    _call(apps().patch_namespaced_deployment, a.deployment, a.namespace, body)
    return {"deployment": a.deployment, "namespace": a.namespace, "restarted_at": now}


@tool("kubernetes", ScaleArgs, mutating=True, precheck=allowed_namespace)
def scale_deployment(a: ScaleArgs) -> dict:
    """Scale a deployment to 1-5 replicas. Changes cluster state: requires explicit user confirmation."""
    _call(apps().patch_namespaced_deployment_scale, a.deployment, a.namespace, {"spec": {"replicas": a.replicas}})
    return {"deployment": a.deployment, "namespace": a.namespace, "replicas": a.replicas}
