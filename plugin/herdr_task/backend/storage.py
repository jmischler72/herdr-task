#!/usr/bin/env python3
"""
Task storage, persistence, locking, task resolution, and resource classification.
"""

from __future__ import annotations

from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from typing import Any
from urllib.parse import urlparse
import uuid

from herdr_task.backend.models import (
    DEFAULT_CONFIG,
    IGNORED_JIRA_PREFIXES,
    TaskPaths,
    _extract_jira_key_from_text,
    is_valid_task_id,
    now_iso,
    resolve_bin,
)


JIRA_KEY_RE = re.compile(r"^([A-Z][A-Z0-9_]{1,15}-\d+)$")
JIRA_URL_RE = re.compile(r"/browse/([A-Z][A-Z0-9_]{1,15}-\d+)")
GH_PR_URL_RE = re.compile(r"github\.com/([^/]+)/([^/]+)/pull/(\d+)")
GH_ISSUE_URL_RE = re.compile(r"github\.com/([^/]+)/([^/]+)/issues/(\d+)")
GH_SHORT_RE = re.compile(r"^(?:([A-Za-z0-9._-]+)/)?([A-Za-z0-9._-]+)#(\d+)$")
GH_REMOTE_ORG_RE = re.compile(r"github\.com[:/]([^/]+)/([^/.]+?)(?:\.git)?$")
SLACK_URL_RE = re.compile(r"https?://([a-zA-Z0-9_-]+)\.slack\.com/archives/([A-Z0-9]+)/p(\d+)")


@contextmanager
def task_store_lock(paths: TaskPaths):
    paths.ensure_dirs()
    fd = os.open(str(paths.lock_file), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def atomic_write_json(target: Path, data: dict[str, Any]) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=f".{target.name}.", dir=str(target.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, str(target))
    except Exception:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise


def save_task(paths: TaskPaths, task: dict[str, Any]) -> None:
    """Convenience helper to persist a task atomically."""
    task["updated_at"] = now_iso()
    atomic_write_json(paths.task_file(task["id"]), task)


def load_task(paths: TaskPaths, task_id: str, include_archived: bool = False) -> dict[str, Any] | None:
    if not is_valid_task_id(task_id):
        return None
    f = paths.task_file(task_id)
    if f.exists():
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                if data.get("status") == "archived" and not include_archived:
                    return None
                return data
        except Exception:
            return None
    if include_archived:
        af = paths.archive_file(task_id)
        if af.exists():
            try:
                return json.loads(af.read_text(encoding="utf-8"))
            except Exception:
                return None
    return None


def list_all_tasks(paths: TaskPaths, include_archived: bool = False) -> list[dict[str, Any]]:
    paths.ensure_dirs()
    tasks: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for p in sorted(paths.base.glob("*.json")):
        if p.name == "config.json":
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict) and is_valid_task_id(data.get("id")):
                if data.get("status") == "archived" and not include_archived:
                    continue
                tasks.append(data)
                seen_ids.add(data["id"])
        except Exception:
            continue
    if include_archived:
        for p in sorted(paths.archive.glob("*.json")):
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(data, dict) and is_valid_task_id(data.get("id")) and data["id"] not in seen_ids:
                    tasks.append(data)
                    seen_ids.add(data["id"])
            except Exception:
                continue
    tasks.sort(key=lambda t: t.get("updated_at") or t.get("created_at") or "", reverse=True)
    return tasks


def collect_active_worktree_paths(paths: TaskPaths, exclude_task_id: str | None = None) -> set[str]:
    used: set[str] = set()
    for t in list_all_tasks(paths, include_archived=False):
        if exclude_task_id and t.get("id") == exclude_task_id:
            continue
        for wt in t.get("worktrees", []):
            wp = wt.get("worktree_path")
            if wp:
                used.add(os.path.realpath(wp))
    return used


def resolve_current_task_id(
    paths: TaskPaths,
    explicit_task_id: str | None = None,
    include_archived: bool = False,
) -> str | None:
    if explicit_task_id:
        exp_clean = explicit_task_id.strip()
        if is_valid_task_id(exp_clean):
            if paths.task_file(exp_clean).exists():
                return exp_clean
            if include_archived and paths.archive_file(exp_clean).exists():
                return exp_clean
        all_tasks = list_all_tasks(paths, include_archived=include_archived)
        exp_low = exp_clean.lower()
        for t in all_tasks:
            prev_lows = [str(p).lower() for p in (t.get("previous_ids") or [])]
            if t["id"].lower() == exp_low or exp_low in prev_lows:
                return t["id"]
        for t in all_tasks:
            if t["id"].lower().startswith(exp_low):
                return t["id"]
        return exp_clean if is_valid_task_id(exp_clean) else None

    if os.environ.get("HERDR_PILOT_MODE") == "new_task":
        return None

    tasks = list_all_tasks(paths)

    env_task_id = os.environ.get("HERDR_TASK_ID", "").strip()
    if env_task_id:
        if is_valid_task_id(env_task_id) and paths.task_file(env_task_id).exists():
            return env_task_id
        env_low = env_task_id.lower()
        for t in tasks:
            prev_lows = [str(p).lower() for p in (t.get("previous_ids") or [])]
            if t["id"].lower() == env_low or env_low in prev_lows:
                return t["id"]

    env_ws_id = os.environ.get("HERDR_WORKSPACE_ID", "").strip()
    if env_ws_id:
        for t in tasks:
            if t.get("workspace_id") == env_ws_id:
                return t["id"]

    try:
        cwd = os.path.realpath(os.getcwd())
    except Exception:
        cwd = os.getcwd()

    for t in tasks:
        for wt in t.get("worktrees", []):
            wt_path = wt.get("worktree_path")
            if wt_path:
                real_wt = os.path.realpath(wt_path)
                if cwd == real_wt or cwd.startswith(real_wt + os.sep):
                    return t["id"]
    return None


def discover_available_repos(cfg: dict[str, Any]) -> list[str]:
    repos: list[str] = []
    for root in cfg.get("repo_roots", []):
        rp = Path(os.path.expanduser(root))
        if rp.is_dir():
            try:
                for child in sorted(rp.iterdir()):
                    if child.is_dir() and (child / ".git").exists() and child.name not in repos:
                        repos.append(child.name)
            except Exception:
                pass
    return repos


def _infer_github_org_for_repo(repo_name: str, cfg: dict[str, Any]) -> str | None:
    from herdr_task.backend.worktree import resolve_repo_path
    try:
        _, repo_path = resolve_repo_path(repo_name, cfg)
        res = subprocess.run(
            ["git", "-C", str(repo_path), "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            timeout=2.0,
            check=False,
        )
        if res.returncode == 0 and res.stdout.strip():
            m = GH_REMOTE_ORG_RE.search(res.stdout.strip())
            if m:
                return m.group(1)
    except Exception:
        pass
    return None


def fetch_github_pr_metadata(org: str, repo: str, num: str) -> dict[str, Any]:
    """Zero-AI-token GitHub PR enrichment via `gh pr view`."""
    gh_bin = resolve_bin("gh")
    if not (shutil.which(gh_bin) or os.path.exists(gh_bin)):
        return {}
    try:
        res = subprocess.run(
            [
                gh_bin, "pr", "view", str(num),
                "-R", f"{org}/{repo}",
                "--json", "title,headRefName,state,isDraft,body",
            ],
            capture_output=True,
            text=True,
            timeout=4.0,
            check=False,
        )
        if res.returncode != 0 or not res.stdout.strip():
            return {}
        data = json.loads(res.stdout)
        pr_title = (data.get("title") or "").strip()
        head_ref = (data.get("headRefName") or "").strip()
        is_draft = bool(data.get("isDraft"))
        raw_state = (data.get("state") or "OPEN").upper()
        pr_status = "DRAFT" if is_draft and raw_state == "OPEN" else raw_state
        body = (data.get("body") or "").strip()

        summary_lines: list[str] = []
        for line in body.splitlines():
            clean = line.strip()
            if not clean or clean.startswith(("<!--", "#", "---", "- [ ]", "- [x]")):
                continue
            summary_lines.append(clean)
            if len(summary_lines) >= 2:
                break
        body_snip = " ".join(summary_lines)[:180]
        summary = f"{pr_title} (branch: {head_ref})" if head_ref else pr_title
        if body_snip and body_snip.lower() != pr_title.lower():
            summary = f"{summary} — {body_snip}"

        jira_key = None
        for text_cand in (pr_title, head_ref, body[:400]):
            jira_key = _extract_jira_key_from_text(text_cand)
            if jira_key:
                break

        return {
            "title": pr_title,
            "branch": head_ref,
            "status": pr_status,
            "summary": summary,
            "jira_key": jira_key,
            "repo": repo,
            "org": org,
        }
    except Exception:
        return {}


def classify_resource(
    raw_input: str,
    cfg: dict[str, Any],
    category: str | None = None,
    provider: str | None = None,
    label: str | None = None,
    status: str | None = None,
    summary: str | None = None,
    added_by: str | None = None,
    metadata: dict[str, Any] | None = None,
    fetch_remote: bool = True,
) -> dict[str, Any]:
    val = raw_input.strip()
    det_cat = category
    det_prov = provider
    ref = val
    url = val
    meta_out: dict[str, Any] = dict(metadata or {})
    explicit_label = bool(label and label.strip())
    default_label: str | None = None

    m_jira_key = JIRA_KEY_RE.match(val)
    m_jira_url = JIRA_URL_RE.search(val)
    m_gh_pr = GH_PR_URL_RE.search(val)
    m_gh_issue = GH_ISSUE_URL_RE.search(val)
    m_gh_short = GH_SHORT_RE.match(val)
    m_slack = SLACK_URL_RE.search(val)

    if m_jira_key:
        key = m_jira_key.group(1)
        det_cat = det_cat or "ticket"
        det_prov = det_prov or "jira"
        ref = key
        base = cfg.get("jira_base_url", DEFAULT_CONFIG["jira_base_url"]).rstrip("/")
        url = f"{base}/{key}"
        default_label = key
    elif m_jira_url:
        key = m_jira_url.group(1)
        det_cat = det_cat or "ticket"
        det_prov = det_prov or "jira"
        ref = key
        default_label = key
    elif m_gh_pr or m_gh_short:
        if m_gh_pr:
            org, repo, num = m_gh_pr.group(1), m_gh_pr.group(2), m_gh_pr.group(3)
        else:
            assert m_gh_short is not None
            org = m_gh_short.group(1) or meta_out.get("org") or _infer_github_org_for_repo(m_gh_short.group(2), cfg)
            repo, num = m_gh_short.group(2), m_gh_short.group(3)
            url = f"https://github.com/{org}/{repo}/pull/{num}" if org else ""
        det_cat = det_cat or "pull_request"
        det_prov = det_prov or "github"
        ref = f"{repo}#{num}"
        meta_out.setdefault("repo", repo)
        if org:
            meta_out.setdefault("org", org)
        default_label = f"{repo} #{num}"
        if org and fetch_remote and not (label and status and summary and meta_out.get("branch")):
            pr_meta = fetch_github_pr_metadata(org, repo, num)
            if pr_meta:
                if not label and pr_meta.get("title"):
                    label = f"{repo}#{num}: {pr_meta['title']}"
                    explicit_label = True
                if not status and pr_meta.get("status"):
                    status = pr_meta["status"]
                if not summary and pr_meta.get("summary"):
                    summary = pr_meta["summary"]
                if pr_meta.get("branch"):
                    meta_out["branch"] = pr_meta["branch"]
                if pr_meta.get("title"):
                    meta_out["pr_title"] = pr_meta["title"]
                if pr_meta.get("jira_key"):
                    meta_out["jira_key"] = pr_meta["jira_key"]
    elif m_gh_issue:
        org, repo, num = m_gh_issue.group(1), m_gh_issue.group(2), m_gh_issue.group(3)
        det_cat = det_cat or "issue"
        det_prov = det_prov or "github"
        ref = f"{repo}#{num}"
        meta_out.setdefault("repo", repo)
        meta_out.setdefault("org", org)
        default_label = f"{repo} issue #{num}"
    elif m_slack:
        team_slug, chan_id, ts_raw = m_slack.group(1), m_slack.group(2), m_slack.group(3)
        det_cat = det_cat or "conversation"
        det_prov = det_prov or "slack"
        ts_dot = f"{ts_raw[:-6]}.{ts_raw[-6:]}" if len(ts_raw) > 6 else ts_raw
        ref = f"slack://channel?id={chan_id}&message={ts_dot}"
        url = ref
        default_label = f"Slack thread ({team_slug} / {chan_id})"
    elif any(k in val.lower() for k in ("securityhub", "grafana", "datadoghq", "sentry.io")):
        det_cat = det_cat or "alert"
        det_prov = det_prov or "alert"
        default_label = "Alert / Monitor"
    elif any(d in val.lower() for d in ("notion.so", "notion.site", "confluence", "atlassian.net/wiki", "figma.com", "docs.google.com")):
        det_cat = det_cat or "doc"
        det_prov = det_prov or "doc"
        default_label = "Documentation / Spec"
    else:
        det_cat = det_cat or "custom"
        det_prov = det_prov or ("web" if val.startswith(("http://", "https://")) else "custom")
        parsed = urlparse(val)
        default_label = f"{parsed.netloc}{parsed.path}" if parsed.netloc else val

    return {
        "id": f"res_{uuid.uuid4().hex[:8]}",
        "category": det_cat,
        "provider": det_prov,
        "ref": ref,
        "url": url,
        "label": label or default_label or ref,
        "_explicit_label": explicit_label,
        "status": status,
        "summary": summary,
        "metadata": meta_out,
        "added_by": added_by or os.environ.get("USER") or "user",
        "updated_at": now_iso(),
    }


def upsert_resource(task: dict[str, Any], new_res: dict[str, Any]) -> dict[str, Any]:
    explicit_label = bool(new_res.pop("_explicit_label", False))
    resources = task.setdefault("resources", [])
    for existing in resources:
        same_ref = existing.get("ref") and existing.get("ref") == new_res.get("ref")
        same_url = existing.get("url") and existing.get("url") == new_res.get("url")
        if same_ref or same_url:
            for k in ("category", "provider", "status", "summary", "added_by"):
                if new_res.get(k) is not None:
                    existing[k] = new_res[k]
            if new_res.get("label") is not None:
                if explicit_label or not existing.get("label") or existing.get("label") == existing.get("ref"):
                    existing["label"] = new_res["label"]
            if new_res.get("url") and new_res["url"].startswith(("http://", "https://", "slack://")):
                existing["url"] = new_res["url"]
            if new_res.get("metadata"):
                existing.setdefault("metadata", {}).update(new_res["metadata"])
            existing["updated_at"] = now_iso()
            return existing
    resources.append(new_res)
    return new_res
