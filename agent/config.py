"""Agent settings, read from the environment (.env locally). No secrets are logged."""

import os
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(REPO_ROOT / ".env")

# Fail fast instead of MLflow's default minutes of retries when the server is down.
os.environ.setdefault("MLFLOW_HTTP_REQUEST_MAX_RETRIES", "0")
os.environ.setdefault("MLFLOW_HTTP_REQUEST_TIMEOUT", "10")

MLFLOW_TRACKING_URI = os.getenv("MLFLOW_TRACKING_URI", "http://127.0.0.1:5001")
MLFLOW_EXPERIMENT = "breast-cancer-classifier"
MODEL_NAME = "breast-cancer-classifier"

AIRFLOW_API_URL = os.getenv("AIRFLOW_API_URL", "http://127.0.0.1:8080")
AIRFLOW_USERNAME = os.getenv("AIRFLOW_USERNAME", "admin")
AIRFLOW_PASSWORD = os.getenv("AIRFLOW_PASSWORD", "")
APPROVED_DAGS = {"training_pipeline"}  # the only DAGs the agent may trigger

K8S_NAMESPACE = os.getenv("K8S_NAMESPACE", "mlops-app")
ALLOWED_NAMESPACES = {K8S_NAMESPACE}  # the agent may not read or change anything else

TOOL_TIMEOUT_S = float(os.getenv("TOOL_TIMEOUT_S", "20"))
HTTP_TIMEOUT_S = float(os.getenv("HTTP_TIMEOUT_S", "10"))

GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite")
