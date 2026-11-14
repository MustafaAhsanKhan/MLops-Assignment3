"""Prediction API for the breast-cancer classifier (runs in Kubernetes).

/health = liveness (process is up), /ready = readiness (model loaded), /version, /predict.
If the model fails to load the app keeps running but /ready returns 503 with the reason,
so Kubernetes stops sending traffic and the error is visible in logs and probe events.
"""

import json
import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path

import joblib
import pandas as pd
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

MODEL_DIR = Path(os.environ.get("MODEL_DIR", Path(__file__).parent / "model"))
THRESHOLD = float(os.environ.get("PREDICTION_THRESHOLD", "0.5"))
CLASS_NAMES = {0: "malignant", 1: "benign"}

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                    format="%(asctime)s %(levelname)s prediction-api %(message)s")
log = logging.getLogger("prediction-api")
state: dict = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        state["model"] = joblib.load(MODEL_DIR / "model.joblib")
        state["meta"] = json.loads((MODEL_DIR / "metadata.json").read_text())
        log.info("loaded %s v%s from %s (threshold=%s)", state["meta"]["model_name"],
                 state["meta"]["model_version"], MODEL_DIR, THRESHOLD)
    except Exception as e:
        state["error"] = f"model load failed from {MODEL_DIR}: {e}"
        log.error(state["error"])
    yield


app = FastAPI(title="prediction-api", lifespan=lifespan)


class PredictRequest(BaseModel):
    instances: list[dict[str, float]] = Field(min_length=1, max_length=1000)


@app.get("/health")
def health():
    return {"status": "alive"}


@app.get("/ready")
def ready():
    if "model" not in state:
        raise HTTPException(503, state.get("error", "model not loaded"))
    return {"status": "ready", "model_version": state["meta"]["model_version"]}


@app.get("/version")
def version():
    return {"app_version": os.environ.get("APP_VERSION", "unknown"),
            "prediction_threshold": THRESHOLD, **state.get("meta", {})}


@app.post("/predict")
def predict(req: PredictRequest):
    if "model" not in state:
        raise HTTPException(503, state.get("error", "model not loaded"))
    features = state["meta"]["features"]
    missing = sorted({f for row in req.instances for f in features if f not in row})
    if missing:
        raise HTTPException(422, f"missing features: {missing}")
    start = time.perf_counter()
    proba = state["model"].predict_proba(pd.DataFrame(req.instances)[features])[:, 1]
    labels = [int(p >= THRESHOLD) for p in proba]
    log.info("predict n=%d ms=%.1f", len(labels), (time.perf_counter() - start) * 1000)
    return {
        "model_version": state["meta"]["model_version"],
        "predictions": [{"label": lbl, "class": CLASS_NAMES[lbl], "probability_benign": round(float(p), 4)}
                        for lbl, p in zip(labels, proba)],
    }
