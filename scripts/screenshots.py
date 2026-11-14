"""Capture evidence screenshots of the running MLflow and Airflow UIs, plus DVC/Kubernetes CLI output.

    uv run --with playwright python scripts/screenshots.py

Needs MLflow (:5001), Airflow (:8080) and the cluster running. Uses the installed Google Chrome.
The Airflow password is read from .env and never printed.
"""

import html
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "screenshots"
load_dotenv(ROOT / ".env")
MLFLOW, AIRFLOW = "http://127.0.0.1:5001", "http://127.0.0.1:8080"
DAG = "training_pipeline"
FAILED_RUN, OK_RUN = sys.argv[1], sys.argv[2]  # Airflow run ids to show
DEPLOYED_RUN = "d1141c0cb0374e12ac4e51e97b937dd8"
BIN = ROOT / ".venv" / "bin"


def sh(cmd: str) -> str:
    env = {**os.environ, "PATH": f"{BIN}:{os.environ['PATH']}"}
    p = subprocess.run(cmd, shell=True, cwd=ROOT, capture_output=True, text=True, env=env)
    out = "\n".join(ln for ln in (p.stdout + p.stderr).splitlines() if "warn" not in ln.lower())
    return f"$ {cmd}\n{out}\n"


def terminal(page, name: str, title: str, cmds: list[str]):
    body = html.escape("\n".join(sh(c) for c in cmds))
    page.set_content(f"""<body style="margin:0;background:#1e1e1e;color:#d4d4d4;font:13px Menlo,monospace">
      <div style="padding:10px 16px;background:#333;color:#fff;font-family:-apple-system">{html.escape(title)}</div>
      <pre style="padding:16px;margin:0;white-space:pre-wrap">{body}</pre></body>""")
    page.screenshot(path=OUT / name, full_page=True)
    print("saved", name)


def shot(page, url: str, name: str, wait_text: str | None = None, full=False):
    page.goto(url)
    if wait_text:
        page.get_by_text(wait_text).first.wait_for(timeout=20000)
    page.wait_for_timeout(2500)
    page.screenshot(path=OUT / name, full_page=full)
    print("saved", name)


OUT.mkdir(exist_ok=True)
with sync_playwright() as p:
    browser = p.chromium.launch(channel="chrome")
    page = browser.new_page(viewport={"width": 1440, "height": 900}, device_scale_factor=1)

    # MLflow: runs table with metric/provenance columns, deployed run details, model registry
    ml = "?workflowType=machine_learning"
    page.set_viewport_size({"width": 1800, "height": 900})
    page.goto(f"{MLFLOW}/#/experiments/1/runs{ml}")
    page.get_by_text("logreg-data-v2").first.wait_for(timeout=20000)
    page.get_by_role("button", name="Close").click()  # hide the assistant side panel
    page.get_by_role("button", name="Columns").click()
    for col in ["f1", "roc_auc", "model", "data_version", "quality_gate", "git_dirty"]:
        page.locator("[role=dialog], [role=menu], [role=tree], [role=listbox]").first \
            .get_by_text(col, exact=True).click()
    page.keyboard.press("Escape")
    page.wait_for_timeout(2000)
    page.screenshot(path=OUT / "mlflow-01-runs.png")
    print("saved mlflow-01-runs.png")
    page.set_viewport_size({"width": 1440, "height": 900})
    shot(page, f"{MLFLOW}/#/experiments/1/runs/{DEPLOYED_RUN}{ml}", "mlflow-02-deployed-run.png", "rf-data-v2")
    shot(page, f"{MLFLOW}/#/models/breast-cancer-classifier{ml}", "mlflow-03-model-registry.png", "Version 11")

    # Airflow: log in, then DAG grid, failed run, the failing task's log, a successful run
    page.goto(f"{AIRFLOW}/auth/login")
    page.get_by_label("Username").fill(os.environ["AIRFLOW_USERNAME"])
    page.get_by_label("Password").fill(os.environ["AIRFLOW_PASSWORD"])
    page.get_by_role("button", name="Sign in").click()
    page.wait_for_url(lambda u: "/auth/login" not in u, timeout=20000)
    shot(page, f"{AIRFLOW}/dags/{DAG}", "airflow-01-dag-runs.png", DAG)
    shot(page, f"{AIRFLOW}/dags/{DAG}/runs/{quote(OK_RUN)}", "airflow-02-successful-run.png", "quality_gate")
    shot(page, f"{AIRFLOW}/dags/{DAG}/runs/{quote(FAILED_RUN)}", "airflow-03-failed-run.png", "validate_dataset")
    shot(page, f"{AIRFLOW}/dags/{DAG}/runs/{quote(FAILED_RUN)}/tasks/validate_dataset",
         "airflow-04-validation-failure-log.png", "missing values")

    # DVC and Kubernetes have no UI here: render their CLI output
    terminal(page, "dvc-01-tracking-and-versions.png", "DVC: tracked data, versions, remote, pipeline", [
        "cat data/dataset.csv.dvc", "git tag --list 'data-*' -n1", "dvc remote list", "dvc status",
        "dvc status -c data/dataset.csv.dvc", "dvc dag"])
    terminal(page, "k8s-01-workloads.png", "Kubernetes: namespace mlops-app", [
        "kubectl get deploy,rs,pods,svc,endpointslices,configmap -n mlops-app -o wide",
        "kubectl rollout history deployment/prediction-api -n mlops-app"])
    terminal(page, "k8s-02-deployment-spec.png", "Kubernetes: probes, resources, rollout strategy", [
        "kubectl describe deployment prediction-api -n mlops-app"])
    browser.close()
