#!/usr/bin/env python3
"""
Git worktree management, path scoping, branch switching, and collision handling.
"""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
from typing import Any

from herdr_task.backend.models import (
    DEFAULT_CONFIG,
    slugify,
    validate_git_branch,
)


def resolve_repo_path(repo: str, cfg: dict[str, Any]) -> tuple[str, Path]:
    expanded = Path(os.path.expanduser(repo))
    if expanded.is_dir() and ((expanded / ".git").exists() or (expanded / "HEAD").exists()):
        return expanded.name, expanded.resolve()

    for root in cfg.get("repo_roots", []):
        candidate = Path(os.path.expanduser(root)) / repo
        if candidate.is_dir() and (candidate / ".git").exists():
            return candidate.name, candidate.resolve()

    raise ValueError(f"Repository '{repo}' not found in configured repo_roots: {cfg.get('repo_roots')}")


def list_git_worktrees(repo_path: Path) -> list[dict[str, str]]:
    res = subprocess.run(
        ["git", "-C", str(repo_path), "worktree", "list", "--porcelain"],
        capture_output=True,
        text=True,
        check=False,
    )
    if res.returncode != 0:
        return []
    entries: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for line in res.stdout.splitlines():
        if not line.strip():
            if current:
                entries.append(current)
                current = {}
            continue
        if line.startswith("worktree "):
            current["path"] = line[len("worktree "):].strip()
        elif line.startswith("branch refs/heads/"):
            current["branch"] = line[len("branch refs/heads/"):].strip()
        elif line == "detached":
            current["detached"] = "true"
    if current:
        entries.append(current)
    return entries


def git_ref_exists(repo_path: Path, ref: str) -> bool:
    res = subprocess.run(
        ["git", "-C", str(repo_path), "show-ref", "--verify", "--quiet", ref],
        capture_output=True,
        check=False,
    )
    return res.returncode == 0


def detect_default_base_ref(repo_path: Path) -> str:
    for candidate in ("refs/remotes/origin/main", "refs/remotes/origin/master", "refs/heads/main", "refs/heads/master"):
        if git_ref_exists(repo_path, candidate):
            return candidate.replace("refs/remotes/", "").replace("refs/heads/", "")
    return "HEAD"


def check_worktree_safety(worktree_path: str) -> dict[str, Any]:
    p = Path(worktree_path)
    if not p.exists():
        return {"exists": False, "clean": True, "dirty_files": []}

    status_res = subprocess.run(
        ["git", "-C", str(p), "status", "--porcelain"],
        capture_output=True,
        text=True,
        check=False,
    )
    dirty_lines = [line for line in status_res.stdout.splitlines() if line.strip()]
    return {
        "exists": True,
        "clean": len(dirty_lines) == 0,
        "dirty_files": dirty_lines,
    }


def _fetch_branch_if_needed(git_dir: Path, repo_path: Path, branch: str) -> None:
    if (
        not git_ref_exists(repo_path, f"refs/heads/{branch}")
        and not git_ref_exists(repo_path, f"refs/remotes/origin/{branch}")
    ):
        try:
            subprocess.run(
                ["git", "-C", str(git_dir), "fetch", "--quiet", "origin", branch],
                capture_output=True,
                timeout=8.0,
                check=False,
            )
        except Exception:
            pass


def _detach_worktree_if_clean(wt_path: str) -> bool:
    if not check_worktree_safety(wt_path)["clean"]:
        return False
    res = subprocess.run(
        ["git", "-C", wt_path, "checkout", "--detach", "--quiet"],
        capture_output=True,
        check=False,
    )
    return res.returncode == 0


def switch_worktree_branch(
    wt: dict[str, Any],
    target_branch: str,
    cfg: dict[str, Any],
    task_id: str | None = None,
    force: bool = False,
) -> tuple[bool, str]:
    """
    Switches a registered task worktree to `target_branch` in-place via `git checkout`.
    Never moves the worktree directory on disk, so running panes keep a valid $PWD.
    """
    try:
        target_branch = validate_git_branch(target_branch)
    except ValueError as e:
        return False, str(e)

    repo_name = wt["repo"]
    repo_path = Path(wt.get("repo_path") or "")
    if not repo_path.exists():
        _, repo_path = resolve_repo_path(repo_name, cfg)
        wt["repo_path"] = str(repo_path)

    cur_wt_path = Path(wt.get("worktree_path") or "")
    if not cur_wt_path.exists():
        info = ensure_git_worktree(repo_name, target_branch, cfg, task_id=task_id or wt.get("task_id"))
        wt["worktree_path"] = info["worktree_path"]
        wt["branch"] = info["branch"]
        return True, f"Provisioned worktree for {repo_name} on '{wt['branch']}' at {wt['worktree_path']}"

    if not force:
        safety = check_worktree_safety(str(cur_wt_path))
        if not safety["clean"]:
            return (
                False,
                f"Worktree for '{repo_name}' has uncommitted changes ({len(safety['dirty_files'])} files); "
                "commit/stash first or pass --force",
            )

    real_cur = os.path.realpath(str(cur_wt_path))
    for gwt in list_git_worktrees(repo_path):
        if gwt.get("branch") == target_branch and gwt.get("path"):
            real_gwt = os.path.realpath(gwt["path"])
            if real_gwt != real_cur:
                if not _detach_worktree_if_clean(real_gwt):
                    return (
                        False,
                        f"Branch '{target_branch}' is already checked out with uncommitted changes in {real_gwt}",
                    )
            break

    _fetch_branch_if_needed(cur_wt_path, repo_path, target_branch)

    if git_ref_exists(repo_path, f"refs/heads/{target_branch}"):
        co_cmd = ["git", "-C", str(cur_wt_path), "checkout", target_branch]
    elif git_ref_exists(repo_path, f"refs/remotes/origin/{target_branch}"):
        co_cmd = ["git", "-C", str(cur_wt_path), "checkout", "--track", "-b", target_branch, f"origin/{target_branch}"]
    else:
        co_cmd = ["git", "-C", str(cur_wt_path), "checkout", "-b", target_branch]

    co_res = subprocess.run(co_cmd, capture_output=True, text=True, check=False)
    if co_res.returncode != 0:
        err_txt = co_res.stderr.strip() or co_res.stdout.strip()
        return False, f"git checkout '{target_branch}' failed in {repo_name}: {err_txt}"

    wt["branch"] = target_branch
    return True, f"Switched {repo_name} worktree to branch '{wt['branch']}' ({wt['worktree_path']})"


def ensure_git_worktree(
    repo: str,
    branch: str,
    cfg: dict[str, Any],
    task_id: str | None = None,
    used_wt_paths: set[str] | None = None,
) -> dict[str, str]:
    """
    Task-scoped git worktree provisioning at `<worktrees_root>/<task_id>/<repo>`.
    """
    branch = validate_git_branch(branch)
    repo_name, repo_path = resolve_repo_path(repo, cfg)
    wt_root = Path(os.path.expanduser(cfg.get("worktrees_root", DEFAULT_CONFIG["worktrees_root"])))
    scope_folder = slugify(task_id, max_len=48) if task_id else slugify(branch, max_len=64)
    target_wt_path = (wt_root / scope_folder / repo_name).resolve()
    used_real = {os.path.realpath(p) for p in (used_wt_paths or set()) if p}

    if os.path.realpath(str(target_wt_path)) in used_real:
        target_wt_path = (wt_root / scope_folder / f"{repo_name}-{slugify(branch, max_len=24)}").resolve()

    # 1. If target_wt_path already exists as a valid git worktree, reuse it
    if target_wt_path.exists() and (target_wt_path / ".git").exists():
        wt_obj = {
            "repo": repo_name,
            "repo_path": str(repo_path),
            "worktree_path": str(target_wt_path),
            "branch": branch,
        }
        res = subprocess.run(
            ["git", "-C", str(target_wt_path), "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
        )
        cur_b = res.stdout.strip() if res.returncode == 0 else ""
        if cur_b and cur_b != branch and check_worktree_safety(str(target_wt_path))["clean"]:
            ok, _ = switch_worktree_branch(wt_obj, branch, cfg, task_id=task_id)
            if not ok:
                wt_obj["branch"] = cur_b
        else:
            wt_obj["branch"] = cur_b or branch
        return wt_obj

    subprocess.run(["git", "-C", str(repo_path), "worktree", "prune"], capture_output=True, check=False)

    # 2. If branch is currently checked out in another worktree, handle collision cleanly
    real_repo_path = os.path.realpath(str(repo_path))
    for wt in list_git_worktrees(repo_path):
        wt_p = wt.get("path", "")
        wt_b = wt.get("branch", "")
        if wt_b == branch and wt_p:
            real_wt_p = os.path.realpath(wt_p)
            if real_wt_p == real_repo_path:
                if not _detach_worktree_if_clean(str(repo_path)):
                    raise RuntimeError(
                        f"Branch '{branch}' is checked out with uncommitted changes in main repo {repo_path}"
                    )
            elif real_wt_p not in used_real:
                return {
                    "repo": repo_name,
                    "repo_path": str(repo_path),
                    "worktree_path": wt_p,
                    "branch": branch,
                }
            else:
                raise RuntimeError(
                    f"Branch '{branch}' is already checked out in another task worktree ({wt_p})"
                )
            break

    target_wt_path.parent.mkdir(parents=True, exist_ok=True)

    # 3. Checkout local branch, tracking remote branch, or create new branch from default base
    if git_ref_exists(repo_path, f"refs/heads/{branch}"):
        cmd = ["git", "-C", str(repo_path), "worktree", "add", str(target_wt_path), branch]
    else:
        _fetch_branch_if_needed(repo_path, repo_path, branch)
        if git_ref_exists(repo_path, f"refs/remotes/origin/{branch}"):
            cmd = [
                "git", "-C", str(repo_path), "worktree", "add",
                "--track", "-b", branch, str(target_wt_path), f"origin/{branch}",
            ]
        else:
            base_ref = detect_default_base_ref(repo_path)
            cmd = ["git", "-C", str(repo_path), "worktree", "add", "-b", branch, str(target_wt_path), base_ref]

    res = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if res.returncode != 0:
        raise RuntimeError(f"git worktree add failed for {repo_name}: {res.stderr.strip() or res.stdout.strip()}")

    return {
        "repo": repo_name,
        "repo_path": str(repo_path),
        "worktree_path": str(target_wt_path),
        "branch": branch,
    }


# Backwards-compatible alias
provision_worktree = ensure_git_worktree


def remove_worktree(repo_path: Path | str, worktree_path: Path | str, force: bool = False) -> tuple[bool, str]:
    cmd = ["git", "-C", str(repo_path), "worktree", "remove", str(worktree_path)]
    if force:
        cmd.append("--force")
    res = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if res.returncode == 0:
        return True, ""
    return False, res.stderr.strip() or res.stdout.strip()
