"""Export a registered model version from MLflow into serving/model/ for the Docker image.

    python serving/export_model.py [version | alias]     (default: alias "candidate")

Writes model.joblib + metadata.json (provenance) and prints the model version as the last line.
"""

import json
import sys
from pathlib import Path

import joblib
import mlflow
from dotenv import load_dotenv
from mlflow import MlflowClient

ROOT = Path(__file__).resolve().parents[1]
MODEL_NAME = "breast-cancer-classifier"
OUT = ROOT / "serving" / "model"

load_dotenv(ROOT / ".env")
ref = sys.argv[1] if len(sys.argv) > 1 else "candidate"
client = MlflowClient()
mv = (client.get_model_version(MODEL_NAME, ref) if ref.isdigit()
      else client.get_model_version_by_alias(MODEL_NAME, ref))
run = client.get_run(mv.run_id)
model = mlflow.sklearn.load_model(f"models:/{MODEL_NAME}/{mv.version}")

OUT.mkdir(exist_ok=True)
joblib.dump(model, OUT / "model.joblib")
(OUT / "metadata.json").write_text(json.dumps({
    "model_name": MODEL_NAME,
    "model_version": mv.version,
    "run_id": mv.run_id,
    "features": list(model.feature_names_in_),
    "metrics": {k: run.data.metrics[k] for k in ("f1", "roc_auc", "accuracy")},
    **{k: run.data.tags.get(k) for k in ("data_version", "data_md5", "git_commit")},
}, indent=2))
print(mv.version)
