# Scenario 5: Multi-tool reasoning (mandatory integration scenario)

Expected evidence: the agent combines MLflow, DVC, Kubernetes and Airflow, separates facts, uncertainties and recommendations, and does not start retraining on its own.

### User (2026-11-14T23:32:32)

> Find the best recent model, identify the dataset version used to train it, check the deployment status, and explain whether retraining is necessary.

**Tool calls** (executed by the agent, in order):

| # | Tool | Arguments | Status | ms | Error |
|---|---|---|---|---|---|
| 1 | `find_best_run` | `{"experiment": "breast-cancer-classifier", "higher_is_better": true, "last_n": 20, "metric": "f1"}` | ok | 822 |  |
| 2 | `get_model_registry` | `{"model_name": "breast-cancer-classifier"}` | ok | 181 |  |
| 3 | `get_deployed_version` | `{"deployment": "prediction-api", "namespace": "mlops-app"}` | ok | 37 |  |
| 4 | `find_unhealthy_pods` | `{"namespace": "mlops-app"}` | ok | 27 |  |
| 5 | `list_tracked_datasets` | `{}` | ok | 998 |  |
| 6 | `get_dataset_version` | `{"path": "data/dataset.csv"}` | ok | 999 |  |
| 7 | `list_dag_runs` | `{"dag_id": "training_pipeline", "limit": 5}` | ok | 84 |  |

### Agent

### Findings

* **Best Recent Model**: 
  * MLflow tool `find_best_run` identified the best recent run as `a08b493efcd34a728f181d267c41e626` (`logreg-data-v2`) with an F1 score of **0.9861**, accuracy of 0.9825, and ROC AUC of 0.9964.
  * In the MLflow Model Registry (`get_model_registry`), this corresponds to **Model Version 11**, which holds the `@candidate` alias and passed its quality gate.

* **Dataset Version**: 
  * According to MLflow run tags (`find_best_run`), DVC (`list_tracked_datasets`), and dataset tracking (`get_dataset_version`), the best model was trained on **`data-v2`** (MD5: `5afc23b9622f8f9ffa60824f4e97d0d5`).

* **Deployment Status**: 
  * The Kubernetes deployment `prediction-api` in namespace `mlops-app` (`get_deployed_version`) is running image `prediction-api:model-v8` (Revision 3).
  * Its live service endpoint reports running **Model Version 8** (`run_id`: `d1141c0cb0374e12ac4e51e97b937dd8`), which was trained on `data-v2` with an F1 score of 0.9655.
  * Health check (`find_unhealthy_pods`) shows 2/2 pods are running and ready, though both pods have restarted once (exit code 137, OOMKilled=false) and recently recorded volume mount/readiness warnings.

---

### Uncertainties

* Whether Model Version 11 (`@candidate`) is stable under production load or if the previous container restarts (exit code 137) indicate an underlying resource or stability issue in the deployment environment.

---

### Recommended actions

1. **Retraining is not necessary**: The newest dataset version (`data-v2`) is already fully captured by the best recent model (Version 11, trained on `data-v2` with `quality_gate: passed`). 
2. **Promote the candidate model**: Promote Model Version 11 (`@candidate`) to production in the model registry and update the `prediction-api` deployment image/version to run Model Version 11 (replacing the older Version 8).
3. **Monitor resource utilization**: Keep an eye on the `prediction-api` pod restarts (exit code 137) and verify memory limits, as pods previously terminated and experienced intermittent readiness probe failures.

<sub>request_id `b937a5eb7dc6` · session `fe53341f493f`</sub>

---
