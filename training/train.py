"""Training pipeline. Each stage is a subcommand so Airflow can run them as separate tasks.

    python training/train.py validate|preprocess|train|evaluate|gate|all [options]

Stages pass files through --workdir. `evaluate` logs to MLflow with dataset provenance tags;
`gate` registers the model (alias "candidate") only if it passes, otherwise exits non-zero.
"""

import argparse
import hashlib
import json
import logging
import os
import subprocess
import sys
from pathlib import Path

import joblib
import mlflow
import pandas as pd
import yaml
from dotenv import load_dotenv
from mlflow import MlflowClient
from sklearn.ensemble import RandomForestClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import accuracy_score, f1_score, precision_score, recall_score, roc_auc_score
from sklearn.model_selection import train_test_split
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data" / "dataset.csv"
DVC_FILE = ROOT / "data" / "dataset.csv.dvc"
EXPERIMENT = "breast-cancer-classifier"
MODEL_NAME = "breast-cancer-classifier"
TARGET = "target"
MIN_ROWS = 100

log = logging.getLogger("training")


def git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout.strip()


def dvc_md5(dvc_text: str) -> str:
    return yaml.safe_load(dvc_text)["outs"][0]["md5"]


def provenance() -> dict[str, str]:
    """Which dataset is on disk: its md5, the data-* git tag with that md5, and the code commit."""
    md5 = hashlib.md5(DATA.read_bytes()).hexdigest()
    version = "untagged"
    for tag in git("tag", "--list", "data-*").split():
        if dvc_md5(git("show", f"{tag}:data/dataset.csv.dvc")) == md5:
            version = tag
            break
    return {
        "git_commit": git("rev-parse", "HEAD"),
        "git_dirty": str(bool(git("status", "--porcelain"))).lower(),
        "data_path": "data/dataset.csv",
        "data_md5": md5,
        "data_version": version,
        "data_matches_dvc": str(md5 == dvc_md5(DVC_FILE.read_text())).lower(),
    }


def validate(args) -> None:
    df = pd.read_csv(DATA)
    errors = []
    if TARGET not in df.columns:
        errors.append(f"missing target column '{TARGET}'")
    elif not set(df[TARGET].unique()) == {0, 1}:
        errors.append(f"target must contain both classes 0 and 1, got {sorted(df[TARGET].unique())}")
    if len(df) < MIN_ROWS:
        errors.append(f"only {len(df)} rows, need at least {MIN_ROWS}")
    if (nulls := int(df.isna().sum().sum())) > 0:
        errors.append(f"{nulls} missing values")
    if non_numeric := [c for c in df.columns if not pd.api.types.is_numeric_dtype(df[c])]:
        errors.append(f"non-numeric columns: {non_numeric}")
    report = {"rows": len(df), "columns": len(df.columns), "errors": errors, **provenance()}
    (args.workdir / "validation.json").write_text(json.dumps(report, indent=2))
    if errors:
        raise ValueError("dataset validation failed: " + "; ".join(errors))
    log.info("validation passed: %s rows, data_version=%s", len(df), report["data_version"])


def preprocess(args) -> None:
    df = pd.read_csv(DATA).drop_duplicates()
    train, test = train_test_split(df, test_size=0.2, random_state=42, stratify=df[TARGET])
    train.to_csv(args.workdir / "train.csv", index=False)
    test.to_csv(args.workdir / "test.csv", index=False)
    log.info("split %s rows into train=%s test=%s", len(df), len(train), len(test))


def build_model(args):
    if args.model == "logreg":
        params = {"model": "logreg", "C": args.C}
        clf = LogisticRegression(C=args.C, max_iter=1000)
    else:
        params = {"model": "rf", "n_estimators": args.n_estimators, "max_depth": args.max_depth}
        clf = RandomForestClassifier(
            n_estimators=args.n_estimators, max_depth=args.max_depth, random_state=42
        )
    return make_pipeline(StandardScaler(), clf), params


def train(args) -> None:
    df = pd.read_csv(args.workdir / "train.csv")
    model, params = build_model(args)
    model.fit(df.drop(columns=TARGET), df[TARGET])
    joblib.dump(model, args.workdir / "model.joblib")
    (args.workdir / "params.json").write_text(json.dumps(params))
    log.info("trained %s", params)


def evaluate(args) -> None:
    test = pd.read_csv(args.workdir / "test.csv")
    X, y = test.drop(columns=TARGET), test[TARGET]
    model = joblib.load(args.workdir / "model.joblib")
    pred, proba = model.predict(X), model.predict_proba(X)[:, 1]
    metrics = {
        "accuracy": accuracy_score(y, pred),
        "precision": precision_score(y, pred),
        "recall": recall_score(y, pred),
        "f1": f1_score(y, pred),
        "roc_auc": roc_auc_score(y, proba),
    }
    params = json.loads((args.workdir / "params.json").read_text())
    prov = provenance()

    mlflow.set_experiment(EXPERIMENT)
    with mlflow.start_run(run_name=f"{params['model']}-{prov['data_version']}") as run:
        mlflow.log_params({**params, "n_test": len(test)})
        mlflow.log_metrics(metrics)
        mlflow.set_tags({**prov, "pipeline": args.pipeline_tag})
        mlflow.log_input(
            mlflow.data.from_pandas(test, source=prov["data_path"], name=prov["data_version"]),
            context="test",
        )
        mlflow.log_artifact(str(args.workdir / "validation.json"))
        info = mlflow.sklearn.log_model(
            model, name="model", input_example=X.head(2),
            skops_trusted_types=["sklearn.tree._tree.Tree"],  # sklearn's own RF tree type
        )
    (args.workdir / "run.json").write_text(
        json.dumps({"run_id": run.info.run_id, "model_uri": info.model_uri, "metrics": metrics})
    )
    log.info("logged run %s: %s", run.info.run_id, metrics)


def gate(args) -> None:
    run = json.loads((args.workdir / "run.json").read_text())
    m = run["metrics"]
    client = MlflowClient()
    prov = client.get_run(run["run_id"]).data.tags
    reasons = []
    if m["f1"] < args.min_f1:
        reasons.append(f"f1 {m['f1']:.4f} < {args.min_f1}")
    if m["roc_auc"] < args.min_roc_auc:
        reasons.append(f"roc_auc {m['roc_auc']:.4f} < {args.min_roc_auc}")
    if prov.get("data_matches_dvc") != "true":
        reasons.append("dataset on disk does not match the DVC-tracked version")

    client.set_tag(run["run_id"], "quality_gate", "failed" if reasons else "passed")
    client.set_tag(run["run_id"], "quality_gate_reasons", "; ".join(reasons) or "none")
    if reasons:
        raise RuntimeError("quality gate failed, model not registered: " + "; ".join(reasons))

    mv = mlflow.register_model(run["model_uri"], MODEL_NAME, tags={
        k: prov[k] for k in ("data_version", "data_md5", "git_commit")
    })
    # "candidate" only: promoting to production is a human decision.
    client.set_registered_model_alias(MODEL_NAME, "candidate", mv.version)
    log.info("quality gate passed; registered %s v%s as @candidate", MODEL_NAME, mv.version)


STAGES = {"validate": validate, "preprocess": preprocess, "train": train,
          "evaluate": evaluate, "gate": gate}


def main(argv=None) -> int:
    load_dotenv(ROOT / ".env")
    p = argparse.ArgumentParser()
    p.add_argument("stage", choices=[*STAGES, "all"])
    p.add_argument("--workdir", type=Path,
                   default=Path(os.environ.get("DATA_ROOT", ROOT)) / "work" / "manual")
    p.add_argument("--model", choices=["logreg", "rf"], default="logreg")
    p.add_argument("--C", type=float, default=1.0)
    p.add_argument("--n-estimators", type=int, default=100)
    p.add_argument("--max-depth", type=int, default=None)
    p.add_argument("--min-f1", type=float, default=0.95)
    p.add_argument("--min-roc-auc", type=float, default=0.97)
    p.add_argument("--pipeline-tag", default="manual", help="who ran it, e.g. airflow run id")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    args.workdir.mkdir(parents=True, exist_ok=True)

    try:
        for name in STAGES if args.stage == "all" else [args.stage]:
            log.info("stage %s", name)
            STAGES[name](args)
    except Exception as e:
        log.error("%s", e)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
