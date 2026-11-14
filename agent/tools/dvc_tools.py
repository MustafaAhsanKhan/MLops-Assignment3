"""DVC tools: tracked datasets, versions, comparisons, change detection and run provenance.

Only read-only commands from an allowlist are executed; nothing here changes the workspace.
"""

import json
import shutil
import subprocess
import sys
from pathlib import Path

import dvc.api
import yaml
from pydantic import BaseModel, Field, field_validator

from agent import config
from agent.tools.base import PermissionDenied, ToolError, tool
from agent.tools.mlflow_tools import resolve_run

ROOT = config.REPO_ROOT
DVC = str(Path(sys.executable).with_name("dvc")) if Path(sys.executable).with_name("dvc").exists() \
    else (shutil.which("dvc") or "dvc")
ALLOWED = {"dvc": {"status", "diff", "remote"}, "git": {"show", "log", "tag", "rev-parse", "ls-files", "status"}}


def run_cmd(*cmd: str) -> str:
    """Run an allowlisted read-only dvc/git command in the repo and return stdout."""
    prog, sub = cmd[0], cmd[1]
    if sub not in ALLOWED.get(prog, set()):
        raise PermissionDenied(f"command not allowed: {prog} {sub}")
    exe = DVC if prog == "dvc" else "git"
    try:
        p = subprocess.run([exe, *cmd[1:]], cwd=ROOT, capture_output=True, text=True,
                           timeout=config.TOOL_TIMEOUT_S)
    except subprocess.TimeoutExpired as e:
        raise ToolError(f"{prog} {sub} timed out") from e
    if p.returncode != 0:
        raise ToolError(f"{prog} {sub} failed: {p.stderr.strip()[-300:]}")
    return p.stdout


def _md5_at(dvc_file: str, rev: str) -> str | None:
    try:
        return yaml.safe_load(run_cmd("git", "show", f"{rev}:{dvc_file}"))["outs"][0]["md5"]
    except ToolError:
        return None


def _data_tags(dvc_file: str) -> list[dict]:
    tags = []
    for tag in run_cmd("git", "tag", "--list", "data-*", "--sort=creatordate").split():
        commit, date = run_cmd("git", "log", "-1", "--format=%H %cI", tag).split()
        tags.append({"tag": tag, "md5": _md5_at(dvc_file, tag), "commit": commit, "date": date})
    return tags


def _rows(path: str, rev: str | None) -> int | None:
    try:
        with dvc.api.open(path, repo=str(ROOT), rev=rev) as f:
            return sum(1 for _ in f) - 1
    except Exception:
        return None


class PathArgs(BaseModel):
    path: str = Field("data/dataset.csv", description="Path of the DVC-tracked file")

    @field_validator("path")
    @classmethod
    def inside_repo(cls, v: str) -> str:
        if v.startswith("/") or ".." in Path(v).parts:
            raise ValueError("path must be relative to the repository and stay inside it")
        return v


class CompareArgs(PathArgs):
    rev_a: str = Field("data-v1", description="Git tag/commit of the first version")
    rev_b: str = Field("data-v2", description="Git tag/commit of the second version")


class RevArgs(PathArgs):
    rev: str = Field(description="Git tag or commit of the dataset version, e.g. data-v1")


class RunArgs(BaseModel):
    run_id: str = Field(min_length=6, description="MLflow run id or unique prefix")


@tool("dvc")
def list_tracked_datasets(_: BaseModel) -> dict:
    """List files tracked by DVC with their current md5/size, and all tagged dataset versions."""
    out = []
    for dvc_file in run_cmd("git", "ls-files", "*.dvc").split():
        meta = yaml.safe_load((ROOT / dvc_file).read_text())["outs"][0]
        path = str(Path(dvc_file).parent / meta["path"])
        out.append({"path": path, "dvc_file": dvc_file, "md5": meta["md5"], "size_bytes": meta.get("size"),
                    "versions": _data_tags(dvc_file)})
    return {"datasets": out, "remotes": run_cmd("dvc", "remote", "list").split("\n")[:-1]}


@tool("dvc", PathArgs)
def get_dataset_version(a: PathArgs) -> dict:
    """Current version of a dataset: DVC md5, matching data-* tag, the git commit that last changed
    it, HEAD, and whether the file on disk still matches what DVC tracks."""
    dvc_file = f"{a.path}.dvc"
    md5 = _md5_at(dvc_file, "HEAD")
    if md5 is None:
        raise ToolError(f"{a.path} is not tracked by DVC at HEAD")
    commit, date, *msg = run_cmd("git", "log", "-1", "--format=%H %cI %s", "--", dvc_file).split()
    tag = next((t["tag"] for t in _data_tags(dvc_file) if t["md5"] == md5), None)
    status = json.loads(run_cmd("dvc", "status", "--json", dvc_file) or "{}")
    return {"path": a.path, "md5": md5, "version_tag": tag, "last_changed_commit": commit,
            "last_changed_at": date, "commit_message": " ".join(msg),
            "head_commit": run_cmd("git", "rev-parse", "HEAD").strip(),
            "workspace_matches_dvc": not status, "dvc_status": status}


@tool("dvc", CompareArgs)
def compare_dataset_versions(a: CompareArgs) -> dict:
    """Compare two dataset versions (git tags/commits): md5, size, row counts and DVC diff."""
    dvc_file = f"{a.path}.dvc"
    sides = {}
    for rev in (a.rev_a, a.rev_b):
        try:
            meta = yaml.safe_load(run_cmd("git", "show", f"{rev}:{dvc_file}"))["outs"][0]
        except ToolError as e:
            raise ToolError(f"'{rev}' has no {dvc_file}: {e}") from e
        sides[rev] = {"md5": meta["md5"], "size_bytes": meta.get("size"), "rows": _rows(a.path, rev)}
    diff = json.loads(run_cmd("dvc", "diff", "--json", a.rev_a, a.rev_b) or "{}")
    return {"path": a.path, "versions": sides, "identical": sides[a.rev_a]["md5"] == sides[a.rev_b]["md5"],
            "dvc_diff": {k: [x["path"] for x in v] for k, v in diff.items() if v}}


@tool("dvc")
def check_data_changes(_: BaseModel) -> dict:
    """Detect whether DVC-tracked data on disk differs from what is committed (dvc status), and
    whether any .dvc pointer files have uncommitted changes."""
    status = json.loads(run_cmd("dvc", "status", "--json") or "{}")
    pointer_changes = run_cmd("git", "status", "--porcelain", "--", "*.dvc").strip().splitlines()
    return {"data_changed": bool(status) or bool(pointer_changes), "dvc_status": status,
            "uncommitted_dvc_files": pointer_changes}


@tool("dvc", RunArgs)
def get_dataset_for_run(a: RunArgs) -> dict:
    """Which dataset version an MLflow run was trained on (from the provenance it recorded), checked
    against DVC, plus the exact commands to reproduce that data."""
    run = resolve_run(a.run_id)
    t = run.data.tags
    if "data_md5" not in t:
        raise ToolError(f"run {run.info.run_id} has no recorded dataset provenance")
    tag = t.get("data_version")
    tag_md5 = _md5_at("data/dataset.csv.dvc", tag) if tag and tag != "untagged" else None
    return {
        "run_id": run.info.run_id, "run_name": run.info.run_name,
        "recorded": {k: t.get(k) for k in ("data_version", "data_md5", "data_path", "git_commit",
                                           "git_dirty", "data_matches_dvc")},
        "verified_against_dvc": tag_md5 == t["data_md5"] if tag_md5 else None,
        "reproduce": [f"git checkout {tag or t.get('git_commit')} -- data/dataset.csv.dvc",
                      "dvc checkout data/dataset.csv   # or: dvc pull data/dataset.csv",
                      f"# training code for this run: git checkout {t.get('git_commit')}"],
    }


@tool("dvc", RevArgs)
def explain_reproduce_dataset(a: RevArgs) -> dict:
    """Explain how to restore a previous dataset version (does not change anything), after checking
    that the version exists and recording its md5 and row count."""
    md5 = _md5_at(f"{a.path}.dvc", a.rev)
    if md5 is None:
        raise ToolError(f"no DVC-tracked {a.path} at '{a.rev}'")
    return {"rev": a.rev, "md5": md5, "rows": _rows(a.path, a.rev),
            "steps": [f"git checkout {a.rev} -- {a.path}.dvc",
                      f"dvc checkout {a.path}   # restores from the local cache",
                      f"dvc pull {a.path}       # if the cache is empty: fetch from the DVC remote",
                      f"verify: md5 of {a.path} should be {md5}"]}
