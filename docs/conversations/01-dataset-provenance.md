# Scenario 1: Dataset provenance

Expected evidence: correct DVC version and a reproducible reference.

### User (2026-11-14T23:29:40)

> Which dataset version was used to train the model that is currently deployed? Give me the Git commit and explain exactly how to reproduce that dataset.

**Tool calls** (executed by the agent, in order):

| # | Tool | Arguments | Status | ms | Error |
|---|---|---|---|---|---|
| 1 | `get_deployed_version` | `{}` | ok | 55 |  |
| 2 | `get_model_registry` | `{}` | ok | 122 |  |
| 3 | `list_runs` | `{}` | ok | 100 |  |
| 4 | `get_dataset_for_run` | `{"run_id": "d1141c0cb0374e12ac4e51e97b937dd8"}` | ok | 49 |  |
| 5 | `explain_reproduce_dataset` | `{"rev": "data-v2"}` | ok | 283 |  |

### Agent

### Findings
- **Currently deployed model**: The prediction service (`prediction-api`) is running image `prediction-api:model-v8` (live model version 8), corresponding to MLflow run `d1141c0cb0374e12ac4e51e97b937dd8` (**Tool**: `get_deployed_version`).
- **Dataset version used**: That run was trained on dataset version `data-v2` with DVC MD5 `5afc23b9622f8f9ffa60824f4e97d0d5` (**Tool**: `get_dataset_for_run`).
- **Git commit**: The training run recorded Git commit `be70102d5a13456a7e6f7531fff241a7d26b7853` (**Tool**: `get_dataset_for_run`).
- **Dataset reproduction steps**: To reproduce the exact dataset version (`data-v2`), run the following commands (**Tools**: `get_dataset_for_run`, `explain_reproduce_dataset`):
  1. `git checkout data-v2 -- data/dataset.csv.dvc`
  2. `dvc checkout data/dataset.csv` (or `dvc pull data/dataset.csv` if cache is empty)
  3. Verify that the MD5 sum of `data/dataset.csv` matches `5afc23b9622f8f9ffa60824f4e97d0d5` (569 rows).

### Uncertainties
- None; the deployed version, dataset version, DVC MD5, Git commit, and reproduction instructions are all fully verified by tool outputs.

### Recommended actions
- None required; the deployed model version and its associated dataset version are correctly tracked and reproducible.

<sub>request_id `d055c298ce51` · session `64b7d7f15f7d`</sub>

---
