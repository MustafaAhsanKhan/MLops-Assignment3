# Scenario 2: Model selection

Expected evidence: MLflow metrics and a defensible candidate selection.

### User (2026-11-14T23:29:53)

> Compare the last five training runs and recommend the best candidate based on F1 score.

**Tool calls** (executed by the agent, in order):

| # | Tool | Arguments | Status | ms | Error |
|---|---|---|---|---|---|
| 1 | `compare_runs` | `{"experiment": "breast-cancer-classifier", "last_n": 5, "metrics": ["f1", "precision", "recall", "accuracy"]}` | ok | 577 |  |
| 2 | `list_runs` | `{"experiment": "breast-cancer-classifier", "max_results": 5}` | ok | 39 |  |
| 3 | `get_model_registry` | `{"model_name": "breast-cancer-classifier"}` | ok | 323 |  |
| 4 | `get_deployed_version` | `{}` | ok | 43 |  |

### Agent

### Findings

- **Last Five Training Runs Comparison** (`compare_runs` and `list_runs`):
  1. `17e2ce8ac6724bd4b49255702c87f028` (`logreg-data-v2`, trained 2026-11-14T09:23:56): F1 = **0.9861**, Precision = 0.9861, Recall = 0.9861, Accuracy = 0.9825, Quality Gate = `passed`.
  2. `5ddb0baf945b4e44b150475241a5b212` (`rf-data-v2`, trained 2026-11-14T09:26:29): F1 = **0.9655**, Precision = 0.9589, Recall = 0.9722, Accuracy = 0.9561, Quality Gate = `passed`.
  3. `d1141c0cb0374e12ac4e51e97b937dd8` (`rf-data-v2`, trained 2026-11-14T09:01:21): F1 = **0.9655**, Precision = 0.9589, Recall = 0.9722, Accuracy = 0.9561, Quality Gate = `passed`.
  4. `3e231d4349a74184afc5309f3850fc13` (`rf-data-v2`, trained 2026-11-14T08:46:11): F1 = **0.9655**, Precision = 0.9589, Recall = 0.9722, Accuracy = 0.9561, Quality Gate = `passed`.
  5. `43ff22c32c1248a1a6d5c66674c989e5` (`rf-data-v2`, trained 2026-11-14T08:46:16): F1 = **0.9281**, Precision = 0.8765, Recall = 0.9861, Accuracy = 0.9035, Quality Gate = `failed` (reasons: `f1 0.9281 < 0.95; roc_auc 0.8740 < 0.97`).

- **Model Registry & Status** (`get_model_registry`):
  - Run `17e2ce8ac6724bd4b49255702c87f028` corresponds to **Model Version 9** (quality gate passed).
  - Run `5ddb0baf945b4e44b150475241a5b212` corresponds to **Model Version 10**, which currently holds the `@candidate` alias.

- **Current Deployment State** (`get_deployed_version`):
  - Deployment `prediction-api` is currently serving model version **8** (`d1141c0cb0374e12ac4e51e97b937dd8`), which achieved an F1 score of 0.9655.

---

### Uncertainties

- None regarding the metrics or registry states of the last five runs.

---

### Recommended Actions

1. **Recommend Model Version 9 (`17e2ce8ac6724bd4b49255702c87f028`) as the Best Candidate**:
   - Out of the last five training runs, run `17e2ce8ac6724bd4b49255702c87f028` achieves the highest F1 score (**0.9861**), along with superior precision, recall, and accuracy.
   - It successfully passed its quality gate on dataset version `data-v2`.
2. **Promote and Deploy**:
   - Assign the `@candidate` alias (or promote) Model Version 9 in the MLflow registry and update the `prediction-api` deployment to use Model Version 9 to replace the currently deployed Version 8 (which has a lower F1 score of 0.9655).

<sub>request_id `53c7b8cddabf` · session `42c573022c86`</sub>

---
