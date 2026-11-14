# Failure investigation 1: training blocked by invalid data

**Injected fault:** 30 values of `mean radius` in the working copy of `data/dataset.csv` were set to empty (not committed, so DVC sees the file as modified). Then the training DAG was triggered through the agent.

### User (2026-11-14T23:34:19)

> Trigger the training pipeline with the default settings and report whether it succeeded.

**Tool calls** (executed by the agent, in order):

| # | Tool | Arguments | Status | ms | Error |
|---|---|---|---|---|---|
| 1 | `trigger_dag` | `{}` | needs_confirmation | 0 | trigger_dag changes system state; it runs only after the user explicitly confirms. |

### Agent

To trigger the training pipeline, an explicit user confirmation is required.

**Findings**
- **Airflow**: The `trigger_dag` tool returned `needs_confirmation` with pending action ID `73e4cffa`. No training DAG run has been started yet because state-changing actions require confirmation.

**Uncertainties**
- Whether the training pipeline will succeed or fail once triggered.
- Task execution details, metrics, and logs for the run.

**Recommended actions**
- Confirm the action so the training pipeline (`training_pipeline`) can be triggered with the default settings.

**Pending actions (not executed until the user confirms):**

- `73e4cffa`: `trigger_dag({})`

<sub>request_id `587a31bc330e` · session `617dcbb1ca48`</sub>

---
### User (2026-11-14T23:34:45)

_Confirmed pending action(s): `73e4cffa`_

> Confirmed, run it.

**Tool calls** (executed by the agent, in order):

| # | Tool | Arguments | Status | ms | Error |
|---|---|---|---|---|---|
| 1 | `trigger_dag` | `{}` | ok | 29 |  |
| 2 | `wait_for_dag_run` | `{"dag_id": "training_pipeline", "dag_run_id": "manual__2026-11-14T18:34:25.477997+00:00"}` | ok | 15128 |  |
| 3 | `get_task_log` | `{"dag_id": "training_pipeline", "dag_run_id": "manual__2026-11-14T18:34:25.477997+00:00", "task_id": "validate_dataset"}` | ok | 42 |  |

### Agent

**Findings**

- **Airflow**: 
  - The training pipeline DAG run `manual__2026-11-14T18:34:25.477997+00:00` was triggered with default settings (`model: logreg`, `C: 1.0`, `n_estimators: 100`).
  - The run **failed** with state `failed` (`wait_for_dag_run`).
  - The first task, `validate_dataset`, failed with exit code 1 due to dataset validation errors (`get_task_log`). Specifically, the task log reported: `2026-11-14 23:34:33,995 ERROR training dataset validation failed: 30 missing values`.
  - All downstream tasks (`preprocess_data`, `train_model`, `evaluate_and_log_mlflow`, and `quality_gate`) were skipped / marked as `upstream_failed`.

**Uncertainties**
- None regarding the pipeline execution or failure reason.

**Recommended actions**
- Inspect the dataset for missing values (30 missing values detected during validation) before attempting to retrain or run the pipeline again.

<sub>request_id `6400e06f316c` · session `617dcbb1ca48`</sub>

---
### User (2026-11-14T23:34:55)

> Why did it fail? Has the tracked dataset changed, and did anything get registered or deployed because of this run?

**Tool calls** (executed by the agent, in order):

| # | Tool | Arguments | Status | ms | Error |
|---|---|---|---|---|---|
| 1 | `check_data_changes` | `{}` | ok | 1464 |  |
| 2 | `get_dataset_ version` | `{}` | error | 0 | unknown tool 'get_dataset_ version' |
| 3 | `get_dataset_version` | `{}` | ok | 1095 |  |
| 4 | `list_runs` | `{}` | ok | 119 |  |

### Agent

**Findings**

- **Airflow**: 
  - The training run failed during the `validate_dataset` task because of **30 missing values** found in the dataset (`get_task_log`).
- **DVC / Dataset**: 
  - The tracked dataset file (`data/dataset.csv`) has been modified (`check_data_changes`, `get_dataset_version`), meaning the dataset on disk differs from the version tracked by DVC (`data-v2`).
- **MLflow**: 
  - Because the training pipeline failed on the very first validation task, no training run was executed or logged in MLflow for this manual trigger (`list_runs` shows no new run corresponding to run ID `manual__2026-11-14T18:34:25.477997+00:00`).
- **Kubernetes**: 
  - Since no MLflow run was produced, no new model was registered or deployed as a result of this run.

**Uncertainties**
- None regarding the failure reason, data state, or deployment status.

**Recommended actions**
- Inspect `data/dataset.csv` to resolve or clean up the 30 missing values before re-running the training pipeline.

<sub>request_id `a54cd7529ebc` · session `617dcbb1ca48`</sub>

---
## Root cause and resolution

**Root cause:** the working copy of `data/dataset.csv` no longer matched the DVC-tracked version
`data-v2`. It had 30 empty values in `mean radius`. The `validate_dataset` task rejects missing
values (`training/train.py validate`), so it exited 1, and Airflow marked every downstream task
`upstream_failed`.

**Why it is safe:** validation has `retries=0` (bad data does not get better on retry), and the
quality gate is the only step that registers a model. So a bad dataset cannot produce an MLflow
run, a registry version or a deployment. The agent confirmed there was no new MLflow run.

**How the agent diagnosed it:** `wait_for_dag_run` → the failed task and its error line;
`get_task_log` → "dataset validation failed: 30 missing values"; `check_data_changes` → DVC
reports `data/dataset.csv` modified versus the tracked version; `list_runs` → no run was logged.

**Resolution:** restore the tracked version from the DVC cache:

```text
$ dvc checkout --force data/dataset.csv.dvc
M       data/dataset.csv
$ dvc status data/dataset.csv.dvc
Data and pipelines are up to date.
$ md5 -q data/dataset.csv
5afc23b9622f8f9ffa60824f4e97d0d5
```

The md5 matches the `data-v2` pointer (`5afc23b9622f8f9ffa60824f4e97d0d5`), so the next
triggered run validates normally (scenario 3 shows a successful run on this data).

**Reviewer check:** all agent statements match Airflow, DVC and MLflow. The Kubernetes line is a
correct deduction (no run means no registered model, so nothing to deploy), but the agent did not
query the cluster to verify it. It also hit the misspelled tool name again and recovered.
