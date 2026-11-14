#!/bin/zsh
# Start the MLflow tracking server; state lives under $DATA_ROOT/mlflow.
set -euo pipefail
cd "${0:A:h}/.."
set -a; source .env; set +a
mkdir -p "$DATA_ROOT/mlflow/artifacts"
exec .venv/bin/mlflow server \
  --backend-store-uri "sqlite:///$DATA_ROOT/mlflow/mlflow.db" \
  --artifacts-destination "$DATA_ROOT/mlflow/artifacts" \
  --host 127.0.0.1 --port 5001
