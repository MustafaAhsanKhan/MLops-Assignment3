# Failure investigation 2: broken rollout (image cannot be pulled)

**Injected fault:** the deployment was updated to an image tag that does not exist:
`kubectl set image deployment/prediction-api -n mlops-app api=prediction-api:model-v99`.

### User (2026-11-14T23:36:02)

> We just rolled out a new version of the prediction API and something looks wrong. Is the API still serving, which version is live, and why is the new version not coming up?

**Tool calls** (executed by the agent, in order):

| # | Tool | Arguments | Status | ms | Error |
|---|---|---|---|---|---|
| 1 | `get_deployed_version` | `{}` | ok | 36 |  |
| 2 | `find_unhealthy_pods` | `{}` | ok | 29 |  |
| 3 | `inspect_services` | `{}` | ok | 73 |  |
| 4 | `get_events` | `{"warnings_only": true}` | ok | 21 |  |

### Agent

### Findings

* **Kubernetes:**
  * **API Service Status:** Yes, the API is still serving traffic. The `prediction-api` service has 2 ready endpoints backing it (`10.42.0.4` and `10.42.0.5`), as reported by `inspect_services`.
  * **Deployed Version:** The currently deployed deployment version/image is `prediction-api:model-v99` (with version label `model-v8`), and the live service `/version` endpoint reports running `model-v8` (model version 8, run id `d1141c0cb0374e12ac4e51e97b937dd8`), as verified by `get_deployed_version`.
  * **Why the New Version is Not Coming Up:** The new rollout's pod (`prediction-api-85c58f4fd-fmg44`) is stuck in `ImagePullBackOff` phase because pulling image `prediction-api:model-v99` failed with `ErrImagePull: Error response from daemon: pull access denied for prediction-api, repository does not exist or may require 'docker login'`, as shown by `find_unhealthy_pods` and `get_events`.

### Uncertainties

* No tool-returned caveats were reported.

### Recommended Actions

* Verify whether the image tag `model-v99` was built and pushed to the container registry, or correct the image tag/repository name in the deployment configuration.
* If authentication is required to pull the image, configure the appropriate image pull secret for the deployment namespace (`mlops-app`).

<sub>request_id `af6f8e28a185` · session `24d12d66079b`</sub>

---
## Root cause and resolution

**Root cause:** the Deployment's pod template pointed at `prediction-api:model-v99`, which was
never built. k3s could not find it locally (`imagePullPolicy: IfNotPresent`), tried Docker Hub,
and was refused (`ErrImagePull`, then `ImagePullBackOff`). The new pod never became ready.

**Why users were not affected:** the rollout strategy is `maxUnavailable: 0, maxSurge: 1`, so
Kubernetes keeps the old ReplicaSet (v8, 2 ready pods) serving until a new pod passes its
readiness probe (`/ready`). The Service kept routing only to the 2 ready v8 endpoints. The agent
reported this split exactly: the spec says v99, while the label and the live `/version` say v8.

**How the agent diagnosed it:** `get_deployed_version` (spec image vs live version),
`find_unhealthy_pods` (the waiting reason and message of the new pod), `inspect_services`
(2 ready endpoints, so still serving), and `get_events` (the pull error from the kubelet).

**Resolution:** the manifests in git are the source of truth, so re-apply them (this is a cluster
change the agent deliberately has no tool for; the only state-changing K8s tools are restart and
scale, both behind confirmation):

```text
$ kubectl apply -f kubernetes/
configmap/prediction-api-config unchanged
deployment.apps/prediction-api configured
namespace/mlops-app unchanged
service/prediction-api unchanged
$ kubectl rollout status deployment/prediction-api -n mlops-app
deployment "prediction-api" successfully rolled out
$ kubectl get pods -n mlops-app
NAME                             READY   STATUS        RESTARTS        AGE
prediction-api-85c58f4fd-fmg44   0/1     Terminating   0               48s
prediction-api-b48bd666d-lnqtr   1/1     Running       1 (7h26m ago)   9h
prediction-api-b48bd666d-s4nws   1/1     Running       1 (7h26m ago)   9h
```

The live service reports model version 8 on 2/2 ready replicas, and the bad ReplicaSet is scaled to 0.
An alternative is `kubectl rollout undo deployment/prediction-api -n mlops-app`.

**Prevention (proposed):** build and load the image before changing the tag (what
`scripts/deploy-k8s.sh` does), lower `progressDeadlineSeconds` (default 600 s) so a stuck rollout
is flagged sooner, and have `get_deployed_version` report the Deployment's `Progressing` condition.
