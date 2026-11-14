"""MLflow tools (MlflowClient only, no UI scraping): experiments, runs, best run, comparisons, registry."""

from datetime import UTC, datetime

from mlflow import MlflowClient
from mlflow.exceptions import MlflowException
from pydantic import BaseModel, Field

from agent import config
from agent.tools.base import ToolError, tool

KEY_TAGS = ("data_version", "data_md5", "git_commit", "git_dirty", "data_matches_dvc",
            "quality_gate", "quality_gate_reasons", "pipeline")
DEFAULT_METRICS = ["f1", "roc_auc", "accuracy", "precision", "recall"]


def client() -> MlflowClient:
    return MlflowClient(tracking_uri=config.MLFLOW_TRACKING_URI)


def _call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except MlflowException as e:
        raise ToolError(f"MLflow error: {e.message[:300]}") from e
    except Exception as e:  # connection refused etc. surface from requests/urllib3
        raise ToolError(f"MLflow unreachable at {config.MLFLOW_TRACKING_URI}: {type(e).__name__}") from e


def _iso(ms: int | None) -> str | None:
    return datetime.fromtimestamp(ms / 1000, UTC).isoformat(timespec="seconds") if ms else None


def _experiment_id(name: str) -> str:
    exp = _call(client().get_experiment_by_name, name)
    if exp is None:
        raise ToolError(f"experiment '{name}' not found")
    return exp.experiment_id


def summarize_run(r) -> dict:
    return {
        "run_id": r.info.run_id,
        "run_name": r.info.run_name,
        "status": r.info.status,
        "start_time": _iso(r.info.start_time),
        "metrics": {k: round(v, 4) for k, v in r.data.metrics.items()},
        "params": dict(r.data.params),
        "tags": {k: r.data.tags[k] for k in KEY_TAGS if k in r.data.tags},
    }


def resolve_run(run_id: str):
    """Accept a full run id or a unique prefix (e.g. the 8 characters shown in the UI)."""
    if len(run_id) == 32:
        return _call(client().get_run, run_id)
    exps = [e.experiment_id for e in _call(client().search_experiments)]
    matches = [r for r in _call(client().search_runs, exps, max_results=1000)
               if r.info.run_id.startswith(run_id)]
    if len(matches) != 1:
        raise ToolError(f"run id prefix '{run_id}' matched {len(matches)} runs; give more characters")
    return matches[0]


def _recent_runs(experiment: str, n: int):
    return _call(client().search_runs, [_experiment_id(experiment)], "attributes.status = 'FINISHED'",
                 order_by=["attributes.start_time DESC"], max_results=n)


class ExperimentArgs(BaseModel):
    experiment: str = config.MLFLOW_EXPERIMENT
    max_results: int = Field(10, ge=1, le=50)


class RunArgs(BaseModel):
    run_id: str = Field(min_length=6, description="Full MLflow run id or a unique prefix")


class BestRunArgs(BaseModel):
    metric: str = "f1"
    experiment: str = config.MLFLOW_EXPERIMENT
    last_n: int = Field(20, ge=1, le=200, description="Only consider the N most recent finished runs")
    higher_is_better: bool = True


class CompareRunsArgs(BaseModel):
    run_ids: list[str] | None = Field(None, min_length=2, max_length=10)
    last_n: int = Field(5, ge=2, le=20, description="Used when run_ids is not given")
    experiment: str = config.MLFLOW_EXPERIMENT
    metrics: list[str] = Field(default_factory=lambda: list(DEFAULT_METRICS))


class CompareExperimentsArgs(BaseModel):
    experiments: list[str] = Field(min_length=2, max_length=5)
    metric: str = "f1"


class RegistryArgs(BaseModel):
    model_name: str = config.MODEL_NAME


@tool("mlflow")
def list_experiments(_: BaseModel) -> dict:
    """List MLflow experiments with id, name and creation time."""
    exps = _call(client().search_experiments)
    return {"experiments": [{"experiment_id": e.experiment_id, "name": e.name,
                             "created": _iso(e.creation_time)} for e in exps]}


@tool("mlflow", ExperimentArgs)
def list_runs(a: ExperimentArgs) -> dict:
    """List the most recent training runs of an experiment with metrics, params and provenance tags
    (data_version, git_commit, quality_gate)."""
    runs = _call(client().search_runs, [_experiment_id(a.experiment)],
                 order_by=["attributes.start_time DESC"], max_results=a.max_results)
    return {"experiment": a.experiment, "runs": [summarize_run(r) for r in runs]}


@tool("mlflow", RunArgs)
def get_run_details(a: RunArgs) -> dict:
    """Get one run's metrics, parameters, provenance tags, artifacts and any registered model
    versions created from it."""
    r = resolve_run(a.run_id)
    c = client()
    artifacts = [f.path for f in _call(c.list_artifacts, r.info.run_id)]
    versions = _call(c.search_model_versions, f"run_id='{r.info.run_id}'")
    return {**summarize_run(r), "artifacts": artifacts,
            "registered_versions": [{"name": v.name, "version": v.version} for v in versions]}


@tool("mlflow", BestRunArgs)
def find_best_run(a: BestRunArgs) -> dict:
    """Rank recent finished runs by a metric and return the best one, the ranking and caveats that
    matter before calling it production-ready (quality gate, data version, registry status)."""
    runs = [r for r in _recent_runs(a.experiment, a.last_n) if a.metric in r.data.metrics]
    if not runs:
        raise ToolError(f"no finished runs with metric '{a.metric}' in '{a.experiment}'")
    ranked = sorted(runs, key=lambda r: r.data.metrics[a.metric], reverse=a.higher_is_better)
    best = ranked[0]
    tags = best.data.tags
    versions = _call(client().search_model_versions, f"run_id='{best.info.run_id}'")
    latest_data = runs[0].data.tags.get("data_version")  # runs are newest first
    caveats = []
    if tags.get("quality_gate") != "passed":
        caveats.append(f"best run did not pass the quality gate ({tags.get('quality_gate', 'no gate result')})")
    if tags.get("data_version") != latest_data:
        caveats.append(f"best run used {tags.get('data_version')} but the newest run used {latest_data}")
    if tags.get("git_dirty") == "true":
        caveats.append("best run was trained from uncommitted code (git_dirty=true)")
    if not versions:
        caveats.append("best run has no registered model version, so it cannot be deployed as-is")
    caveats.append(f"ranking uses only '{a.metric}' on one held-out test split; it is not evidence "
                   "of production readiness on its own")
    return {
        "metric": a.metric,
        "considered_runs": len(runs),
        "best": {**summarize_run(best), "registered_versions": [v.version for v in versions]},
        "ranking": [{"run_id": r.info.run_id, "run_name": r.info.run_name,
                     a.metric: round(r.data.metrics[a.metric], 4),
                     "data_version": r.data.tags.get("data_version"),
                     "quality_gate": r.data.tags.get("quality_gate")} for r in ranked[:10]],
        "caveats": caveats,
    }


@tool("mlflow", CompareRunsArgs)
def compare_runs(a: CompareRunsArgs) -> dict:
    """Compare several runs side by side (given run ids, or the last N runs) on chosen metrics and
    say which run leads each metric."""
    runs = ([resolve_run(i) for i in a.run_ids] if a.run_ids
            else _recent_runs(a.experiment, a.last_n))
    rows = [{"run_id": r.info.run_id, "run_name": r.info.run_name, "start_time": _iso(r.info.start_time),
             "params": dict(r.data.params),
             **{m: round(r.data.metrics[m], 4) for m in a.metrics if m in r.data.metrics},
             "data_version": r.data.tags.get("data_version"),
             "quality_gate": r.data.tags.get("quality_gate")} for r in runs]
    leaders = {m: max((row for row in rows if m in row), key=lambda row: row[m])["run_id"]
               for m in a.metrics if any(m in row for row in rows)}
    return {"runs": rows, "leader_per_metric": leaders}


@tool("mlflow", CompareExperimentsArgs)
def compare_experiments(a: CompareExperimentsArgs) -> dict:
    """Compare two or more experiments: number of finished runs and the best run on a metric."""
    out = []
    for name in a.experiments:
        runs = [r for r in _recent_runs(name, 500) if a.metric in r.data.metrics]
        best = max(runs, key=lambda r: r.data.metrics[a.metric], default=None)
        out.append({"experiment": name, "finished_runs": len(runs),
                    "best_run_id": best.info.run_id if best else None,
                    f"best_{a.metric}": round(best.data.metrics[a.metric], 4) if best else None})
    return {"metric": a.metric, "experiments": out}


@tool("mlflow", RegistryArgs)
def get_model_registry(a: RegistryArgs) -> dict:
    """List registered model versions with aliases (e.g. @candidate), source run, metrics, data
    version and quality-gate result."""
    c = client()
    model = _call(c.get_registered_model, a.model_name)
    alias_of = {}
    for alias, version in (model.aliases or {}).items():
        alias_of.setdefault(str(version), []).append(alias)
    versions = sorted(_call(c.search_model_versions, f"name='{a.model_name}'"),
                      key=lambda v: int(v.version), reverse=True)
    out = []
    for v in versions[:20]:
        run = _call(c.get_run, v.run_id) if v.run_id else None
        out.append({
            "version": v.version, "aliases": alias_of.get(str(v.version), []), "status": v.status,
            "created": _iso(v.creation_timestamp), "run_id": v.run_id,
            "f1": round(run.data.metrics.get("f1", float("nan")), 4) if run else None,
            "data_version": v.tags.get("data_version"),
            "quality_gate": run.data.tags.get("quality_gate") if run else None,
        })
    return {"model_name": a.model_name, "aliases": {k: str(v) for k, v in (model.aliases or {}).items()},
            "versions": out}
