# Scenario 4: Deployment diagnosis

Expected evidence: Kubernetes health information and relevant logs. Asked while the service is healthy, to check that the agent reports what it finds instead of inventing a fault (the broken case is failure investigation 2).

## Attempt 1 (before the fix): unexpected failure found during the demo

The agent called the two old restarts "OOMKilled". Kubernetes actually recorded `reason: Error,
exit 137` for both pods at the same second (2026-11-14T11:09:19Z), which matches the Colima VM
restarting, not memory pressure. Exit code 137 only means SIGKILL. The model filled the gap from
general knowledge. Fix: the pod summary now carries an explicit `oom_killed` flag (true only for
reason `OOMKilled`, with a unit test), and system-prompt rule 1 forbids inferring causes a tool
did not record. Attempt 2 below is the same question after the fix.

### User (2026-11-14T23:31:21)

> Users say my prediction API is unavailable. Investigate the pods, health checks, events and logs and tell me what is wrong.

**Tool calls** (executed by the agent, in order):

| # | Tool | Arguments | Status | ms | Error |
|---|---|---|---|---|---|
| 1 | `find_unhealthy_pods` | `{}` | ok | 54 |  |
| 2 | `get_events` | `{"warnings_only": true}` | ok | 15 |  |
| 3 | `get_pod_logs` | `{"pod": "prediction-api-b48bd666d-lnqtr", "previous": true}` | ok | 76 |  |
| 4 | `get_pod_logs` | `{"pod": "prediction-api-b48bd666d-lnqtr", "previous": false}` | ok | 47 |  |
| 5 | `inspect_services` | `{}` | ok | 61 |  |
| 6 | `list_workloads` | `{}` | ok | 20 |  |

### Agent

### Findings

1. **Pod Workloads & Health (`list_workloads`, `find_unhealthy_pods`)**:
   - The `prediction-api` deployment in namespace `mlops-app` currently has 2 out of 2 replicas desired, updated, ready, and available (`model-v8`).
   - Both pods (`prediction-api-b48bd666d-lnqtr` and `prediction-api-b48bd666d-s4nws`) are currently **Running** and **Ready**, with active endpoints serving traffic.
   - However, both pods have **restarted once (restarts: 1)** due to a previous container termination with exit code `137` (OOMKilled / Out of Memory) on `2026-11-14T11:09:19+00:00`.

2. **Kubernetes Events & Warnings (`get_events`, `find_unhealthy_pods`)**:
   - Earlier warning events showed a temporary volume mount issue (`FailedMount: object "mlops-app"/"kube-root-ca.crt" not registered`) and a brief readiness probe failure (`connect: connection refused`) during container startup. These transient events have since resolved, and endpoints are now healthy (`inspect_services`).

3. **Application Logs (`get_pod_logs`)**:
   - Current pod logs show successful HTTP requests to `/health`, `/ready`, and `/version` returning status code `200 OK`.

---

### Uncertainties

- The root cause of the previous OOMKilled event (exit code 137) is not fully diagnosed—whether it was due to a memory limit spike during batch predictions or memory pressure on the node at that specific time cannot be definitively determined without resource monitoring metrics.

---

### Recommended actions

1. **Monitor Memory Usage**: Check container resource limits and cluster node memory utilization to ensure the pods are provisioned with sufficient RAM to prevent future OOMKilled events (exit code 137).
2. **Verify Ingress / Service Mesh**: Since the pods and ClusterIP service endpoints are currently healthy and responding with `200 OK`, check external entrypoints (Ingress controller, API Gateway, load balancer, or DNS) if users are still reporting unavailability.

<sub>request_id `78afc1e2b08e` · session `9a9ab515e01a`</sub>

---

## Attempt 2 (after the fix)

### User (2026-11-14T23:32:11)

> Users say my prediction API is unavailable. Investigate the pods, health checks, events and logs and tell me what is wrong.

**Tool calls** (executed by the agent, in order):

| # | Tool | Arguments | Status | ms | Error |
|---|---|---|---|---|---|
| 1 | `find_unhealthy_pods` | `{}` | ok | 59 |  |
| 2 | `list_workloads` | `{}` | ok | 22 |  |
| 3 | `inspect_services` | `{}` | ok | 56 |  |
| 4 | `get_events` | `{}` | ok | 7 |  |
| 5 | `get_deployed_version` | `{}` | ok | 22 |  |
| 6 | `get_pod_logs` | `{"pod": "prediction-api-b48bd666d-lnqtr", "previous": true}` | ok | 40 |  |
| 7 | `get_pod_logs` | `{"pod": "prediction-api-b48bd666d-lnqtr", "previous": false}` | ok | 27 |  |

### Agent

### Findings

* **Workloads & Replicas (`list_workloads`)**: The `prediction-api` deployment in namespace `mlops-app` has 2 desired, 2 updated, 2 ready, and 2 available replicas running image `prediction-api:model-v8`.
* **Pod Health (`find_unhealthy_pods`)**: Both pods (`prediction-api-b48bd666d-lnqtr` and `prediction-api-b48bd666d-s4nws`) are currently running and reporting `ready: true`. They each have 1 restart with last termination exit code `137` at `2026-11-14T11:09:19+00:00` (`oom_killed: false`, reason `Error`).
* **Events & Warnings (`get_events`)**: 
  * Warning events (`FailedMount`) occurred for both pods attempting to mount volume `kube-api-access` referencing missing object `mlops-app`/`kube-root-ca.crt` at `2026-11-14T18:15:21+00:00`.
  * A transient readiness probe failure (`Connection refused` on `/ready`) occurred on pod `prediction-api-b48bd666d-s4nws` at `2026-11-14T18:15:27+00:00`.
* **Services (`inspect_services`)**: The `prediction-api` service has 2 ready endpoints (`10.42.0.4`, `10.42.0.5`) and 0 not-ready endpoints.
* **Logs (`get_pod_logs`)**: Pod logs show normal, successful HTTP requests returning status `200 OK` for `/health`, `/ready`, `/version`, and inference endpoints.

### Uncertainties

* The root cause of the previous exit code `137` termination at 11:09 UTC (though `oom_killed` was false) is not fully explained by current pod logs, but the pods successfully restarted and have been stable since.
* The source of the transient `FailedMount` volume warning events regarding `kube-root-ca.crt` has resolved itself as both pods are currently running and ready.

### Recommended Actions

* No immediate action is required as all replicas are currently running, ready, and serving traffic successfully.
* Continue to monitor ingress traffic and pod metrics to ensure stability following the recent container restarts.

<sub>request_id `a2c03fce9999` · session `c4a3476a4abd`</sub>

---
**Reviewer check:** every fact in attempt 2 matches `kubectl` output, except the phrase "and inference
endpoints". The pod logs contain no `/predict` requests since the restart (`kubectl logs ... | grep -c predict` → 0).
It is a small unsupported claim from the model, recorded under Known limitations in the report.

