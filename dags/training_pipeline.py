"""Training pipeline: validate -> preprocess -> train -> evaluate/MLflow -> quality gate.

Each task runs one stage of training/train.py with the project venv (Airflow has its own venv).
A failed validate or quality_gate task stops everything after it, so nothing gets registered.
Retries cover transient faults (e.g. MLflow briefly down); validate and quality_gate are
deterministic checks, so retrying them would only delay the failure.
"""

from datetime import timedelta
from pathlib import Path

from airflow.providers.standard.operators.bash import BashOperator
from airflow.sdk import DAG, Param

PROJECT = Path(__file__).resolve().parents[1]
ARGS = (
    ' --workdir "${DATA_ROOT:?DATA_ROOT not set}/work/{{ run_id }}"'
    ' --pipeline-tag "airflow:{{ run_id }}"'
    " --model {{ params.model }} --C {{ params.C }} --n-estimators {{ params.n_estimators }}"
)

with DAG(
    dag_id="training_pipeline",
    description="Validate data, train, log to MLflow and apply the quality gate",
    schedule=None,  # triggered on demand (UI, REST API, agent)
    catchup=False,
    max_active_runs=1,
    is_paused_upon_creation=False,
    tags=["ml", "training"],
    params={
        "model": Param("logreg", enum=["logreg", "rf"]),
        "C": Param(1.0, type="number", exclusiveMinimum=0),
        "n_estimators": Param(100, type="integer", minimum=1),
    },
    default_args={"retries": 2, "retry_delay": timedelta(seconds=15)},
    doc_md=__doc__,
):

    def stage(name: str, task_id: str | None = None, retries: int = 2) -> BashOperator:
        return BashOperator(
            task_id=task_id or name,
            bash_command=f"cd '{PROJECT}' && .venv/bin/python training/train.py {name}" + ARGS,
            retries=retries,
        )

    (
        stage("validate", "validate_dataset", retries=0)
        >> stage("preprocess", "preprocess_data")
        >> stage("train", "train_model")
        >> stage("evaluate", "evaluate_and_log_mlflow")
        >> stage("gate", "quality_gate", retries=0)
    )
