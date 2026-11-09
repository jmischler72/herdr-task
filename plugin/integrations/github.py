#!/usr/bin/env python3
"""
github — Unified modular integration for herdr-task interactive forms & git workflows.

Provides:
  - Candidate GitHub Pull Requests (`gh pr list` + cached task PRs + browser redirect links).
  - Predefined status badges for GitHub PRs, Issues, Alerts, Docs, and Conversations.
  - Repository discovery and local/remote Git branch suggestions for task & worktree forms.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
from typing import Any


INTEGRATION_ID = "github"
AUTOMATION_ID = "github_git"
DESCRIPTION = "GitHub PR picker, browser shortcuts, resource status presets, and Git repo/branch options"

DEFAULT_SETTINGS: dict[str, Any] = {
    "enabled": True,
    "max_prs_per_repo": 15,
    "timeout_sec": 4.0,
}

GH_REMOTE_RE = re.compile(r"github\.com[:/]([^/]+)/([^/.]+?)(?:\.git)?$")

STATUS_PRESETS_BY_CATEGORY: dict[str, list[tuple[str, str]]] = {
    "pull_request": [
        ("OPEN", "🔀 OPEN — Ready for review / active PR"),
        ("DRAFT", "📝 DRAFT — Work in progress draft PR"),
        ("IN REVIEW", "👀 IN REVIEW — Under team code review"),
        ("APPROVED", "✅ APPROVED — Approved and ready to merge"),
        ("CHANGES REQUESTED", "🔄 CHANGES REQUESTED — Needs follow-up commits"),
        ("MERGED", "🟣 MERGED — Merged into base branch"),
        ("CLOSED", "🚫 CLOSED — Closed without merge"),
    ],
    "issue": [
        ("OPEN", "🐛 OPEN — Active issue"),
        ("IN PROGRESS", "🚧 IN PROGRESS — Currently being worked on"),
        ("CLOSED", "✅ CLOSED — Resolved / closed issue"),
    ],
    "alert": [
        ("FIRING", "🚨 FIRING — Active alert / incident"),
        ("ACKNOWLEDGED", "👀 ACKNOWLEDGED — Investigated / triaged"),
        ("RESOLVED", "✅ RESOLVED — Alert recovered"),
        ("SILENCED", "🔕 SILENCED — Muted / maintenance window"),
    ],
    "doc": [
        ("DRAFT", "📝 DRAFT — Work in progress specification"),
        ("IN REVIEW", "👀 IN REVIEW — RFC / spec under review"),
        ("APPROVED", "✅ APPROVED — Finalized / signed-off doc"),
        ("OUTDATED", "📦 OUTDATED — Superseded documentation"),
    ],
    "conversation": [
        ("ACTIVE", "💬 ACTIVE — Ongoing thread / discussion"),
        ("RESOLVED", "✅ RESOLVED — Decision reached"),
    ],
    "custom": [
        ("OPEN", "🔗 OPEN"),
        ("IN PROGRESS", "🚧 IN PROGRESS"),
        ("DONE", "✅ DONE"),
    ],
}


def _resolve_gh_bin() -> str | None:
    found = shutil.which("gh")
    if found:
        return found
    for prefix in ("/opt/homebrew/bin", "/usr/local/bin"):
        cand = os.path.join(prefix, "gh")
        if os.path.exists(cand):
            return cand
    return None


def _resolve_repo_path(repo_name: str, cfg: dict[str, Any]) -> Path | None:
    exp = Path(os.path.expanduser(repo_name))
    if exp.is_dir() and ((exp / ".git").exists() or (exp / "HEAD").exists()):
        return exp.resolve()
    for root in cfg.get("repo_roots") or []:
        cand = Path(os.path.expanduser(str(root))) / repo_name
        if cand.is_dir() and (cand / ".git").exists():
            return cand.resolve()
    return None


def _infer_github_org_repo(repo_path: Path) -> tuple[str, str] | None:
    try:
        res = subprocess.run(
            ["git", "-C", str(repo_path), "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            timeout=2.0,
            check=False,
        )
        if res.returncode == 0 and res.stdout.strip():
            m = GH_REMOTE_RE.search(res.stdout.strip())
            if m:
                return m.group(1), m.group(2)
    except Exception:
        pass
    return None


def _list_repo_open_prs(
    gh_bin: str,
    org: str,
    repo: str,
    limit: int = 15,
    timeout: float = 4.0,
) -> list[dict[str, Any]]:
    try:
        res = subprocess.run(
            [
                gh_bin,
                "pr",
                "list",
                "-R",
                f"{org}/{repo}",
                "--state",
                "open",
                "--limit",
                str(limit),
                "--json",
                "number,title,headRefName,state,isDraft,url,author",
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        if res.returncode == 0 and res.stdout.strip():
            data = json.loads(res.stdout)
            if isinstance(data, list):
                return [item for item in data if isinstance(item, dict)]
    except Exception:
        pass
    return []


def provide_candidates(
    kind: str,
    context: dict[str, Any],
    settings: dict[str, Any],
) -> list[dict[str, Any]]:
    if kind not in ("pull_request", "github", "resource"):
        return []

    cfg = context.get("config") or {}
    task = context.get("task") if isinstance(context.get("task"), dict) else {}
    all_tasks = context.get("all_tasks") if isinstance(context.get("all_tasks"), list) else []
    selected_repos = list(context.get("repos") or [])
    for wt in task.get("worktrees") or []:
        if isinstance(wt, dict) and wt.get("repo") and wt["repo"] not in selected_repos:
            selected_repos.append(wt["repo"])

    candidates: list[dict[str, Any]] = []
    seen_refs: set[str] = set()
    org_repos: list[tuple[str, str]] = []

    for r_name in selected_repos:
        rp = _resolve_repo_path(str(r_name), cfg)
        if rp:
            pair = _infer_github_org_repo(rp)
            if pair and pair not in org_repos:
                org_repos.append(pair)

    # 1. Browser redirect options for GitHub PRs
    for org, repo in org_repos:
        pulls_url = f"https://github.com/{org}/{repo}/pulls"
        candidates.append({
            "value": f"__open_url__:{pulls_url}",
            "action": "open_url",
            "url": pulls_url,
            "label": f"🌐 Open GitHub PRs in Browser — {org}/{repo} ({pulls_url})",
            "provider": "github",
            "category": "pull_request",
            "preview": (
                f"🌐 Open {org}/{repo} Pull Requests in Browser\n\n"
                f"URL: {pulls_url}\n\n"
                "Opens GitHub Pull Requests in your browser and returns to this form."
            ),
        })

    gh_my_pulls = "https://github.com/pulls"
    candidates.append({
        "value": f"__open_url__:{gh_my_pulls}",
        "action": "open_url",
        "url": gh_my_pulls,
        "label": "🌐 Open GitHub in Browser — My Pull Requests (https://github.com/pulls)",
        "provider": "github",
        "category": "pull_request",
        "preview": (
            f"🌐 Open GitHub Pull Requests Dashboard\n\n"
            f"URL: {gh_my_pulls}\n\n"
            "Opens your GitHub Pull Requests page in the browser."
        ),
    })

    # 2. Live open PRs via `gh pr list` on attached/selected repositories
    gh_bin = _resolve_gh_bin()
    limit = int(settings.get("max_prs_per_repo") or 15)
    timeout = float(settings.get("timeout_sec") or 4.0)

    if gh_bin and org_repos:
        for org, repo in org_repos:
            for pr in _list_repo_open_prs(gh_bin, org, repo, limit=limit, timeout=timeout):
                num = pr.get("number")
                if not num:
                    continue
                ref = f"{repo}#{num}"
                if ref in seen_refs:
                    continue
                seen_refs.add(ref)
                title = (pr.get("title") or "").strip()
                branch = (pr.get("headRefName") or "").strip()
                is_draft = bool(pr.get("isDraft"))
                raw_state = (pr.get("state") or "OPEN").upper()
                st_badge = "DRAFT" if is_draft and raw_state == "OPEN" else raw_state
                url = (pr.get("url") or f"https://github.com/{org}/{repo}/pull/{num}").strip()
                author = ((pr.get("author") or {}).get("login") or "").strip()
                summary = f"{title} (branch: {branch})" if branch else title

                candidates.append({
                    "value": url,
                    "label": f"🔀 [{st_badge}] {ref} — {title}  ({branch})",
                    "provider": "github",
                    "category": "pull_request",
                    "preview": (
                        f"🔀 {ref} [{st_badge}]\n"
                        f"Title:   {title}\n"
                        f"Branch:  {branch}\n"
                        f"Author:  {author or '-'}\n"
                        f"URL:     {url}"
                    ),
                    "resource_patch": {
                        "ref": ref,
                        "url": url,
                        "category": "pull_request",
                        "provider": "github",
                        "label": f"{ref}: {title}" if title else ref,
                        "status": st_badge,
                        "summary": summary,
                        "metadata": {
                            "org": org,
                            "repo": repo,
                            "branch": branch,
                            "pr_title": title,
                        },
                    },
                })

    # 3. Cached PRs from active/archived local tasks
    task_sources = ([task] if task else []) + list(all_tasks)
    for t_obj in task_sources:
        if not isinstance(t_obj, dict):
            continue
        for r in t_obj.get("resources") or []:
            if not isinstance(r, dict) or r.get("category") != "pull_request":
                continue
            ref = (r.get("ref") or "").strip()
            if not ref or ref in seen_refs:
                continue
            seen_refs.add(ref)
            st_badge = (r.get("status") or "OPEN").upper()
            lbl = r.get("label") or ref
            url = r.get("url") or ref
            meta = r.get("metadata") or {}
            branch = meta.get("branch") or ""
            candidates.append({
                "value": url,
                "label": f"🔀 [{st_badge}] {lbl}" + (f"  ({branch})" if branch else ""),
                "provider": "github",
                "category": "pull_request",
                "preview": (
                    f"🔀 {lbl} [{st_badge}] (Cached from task '{t_obj.get('id', '')}')\n"
                    f"Branch:  {branch or '-'}\n"
                    f"URL:     {url}\n"
                    f"Summary: {r.get('summary') or '(none)'}"
                ),
                "resource_patch": {
                    "ref": ref,
                    "url": url,
                    "category": "pull_request",
                    "provider": "github",
                    "label": lbl,
                    "status": r.get("status"),
                    "summary": r.get("summary"),
                    "metadata": dict(meta),
                },
            })

    return candidates


def _list_git_branches_for_repo(repo_path: Path, limit: int = 40) -> list[tuple[str, str]]:
    try:
        res = subprocess.run(
            [
                "git",
                "-C",
                str(repo_path),
                "for-each-ref",
                "--sort=-committerdate",
                "--format=%(refname:short)\t%(committerdate:relative)\t%(subject)",
                "refs/heads",
                "refs/remotes/origin",
            ],
            capture_output=True,
            text=True,
            timeout=3.0,
            check=False,
        )
        if res.returncode != 0:
            return []
        seen: set[str] = set()
        out: list[tuple[str, str]] = []
        for line in res.stdout.splitlines():
            parts = line.split("\t", 2)
            raw_ref = parts[0].strip()
            if not raw_ref or raw_ref == "origin/HEAD" or raw_ref.endswith("/HEAD"):
                continue
            b_name = raw_ref[len("origin/"):] if raw_ref.startswith("origin/") else raw_ref
            if b_name in seen:
                continue
            seen.add(b_name)
            rel_time = parts[1].strip() if len(parts) > 1 else ""
            subj = parts[2].strip()[:50] if len(parts) > 2 else ""
            source_tag = "remote" if raw_ref.startswith("origin/") else "local"
            desc = f"🌿 {b_name}  ({source_tag}, {rel_time}) — {subj}" if rel_time else f"🌿 {b_name}"
            out.append((b_name, desc))
            if len(out) >= limit:
                break
        return out
    except Exception:
        pass
    return []


def provide_field_options(
    field: str,
    context: dict[str, Any],
    settings: dict[str, Any],
) -> list[dict[str, Any]]:
    cfg = context.get("config") or {}
    task = context.get("task") if isinstance(context.get("task"), dict) else {}

    if field == "resource.status":
        res = context.get("resource") if isinstance(context.get("resource"), dict) else {}
        category = (res.get("category") or context.get("category") or "custom").lower()
        provider = (res.get("provider") or context.get("provider") or "").lower()
        if provider == "jira" or category == "ticket":
            return []
        presets = STATUS_PRESETS_BY_CATEGORY.get(category) or STATUS_PRESETS_BY_CATEGORY["custom"]
        return [
            {"value": val, "label": label, "provider": provider or category}
            for val, label in presets
        ]

    if field in ("task.branch", "worktree.branch"):
        target_repo = str(context.get("repo") or "").strip()
        repos_to_inspect: list[str] = []
        if target_repo:
            repos_to_inspect.append(target_repo)
        else:
            for r_name in context.get("repos") or []:
                if str(r_name).strip():
                    repos_to_inspect.append(str(r_name).strip())
            for wt in task.get("worktrees") or []:
                if isinstance(wt, dict) and wt.get("repo") and wt["repo"] not in repos_to_inspect:
                    repos_to_inspect.append(wt["repo"])

        options: list[dict[str, Any]] = []
        seen_branches: set[str] = set()

        task_id = str(context.get("task_id") or task.get("id") or "").strip()
        cur_branch = str(context.get("current_branch") or task.get("default_branch") or "").strip()
        if cur_branch:
            seen_branches.add(cur_branch)
            options.append({
                "value": cur_branch,
                "label": f"🌿 {cur_branch}  (current)",
                "provider": "git",
            })
        if task_id:
            feat_b = f"feat/{task_id}"
            if feat_b not in seen_branches:
                seen_branches.add(feat_b)
                options.append({
                    "value": feat_b,
                    "label": f"✨ {feat_b}  (suggested task branch)",
                    "provider": "git",
                })

        for r in task.get("resources") or []:
            if isinstance(r, dict) and r.get("category") == "pull_request":
                meta = r.get("metadata") or {}
                pr_b = (meta.get("branch") or "").strip()
                pr_repo = (meta.get("repo") or "").strip()
                if pr_b and pr_b not in seen_branches and (not target_repo or pr_repo == target_repo):
                    seen_branches.add(pr_b)
                    options.append({
                        "value": pr_b,
                        "label": f"🔀 {pr_b}  (from linked PR {r.get('ref', '')})",
                        "provider": "github",
                    })

        for r_name in repos_to_inspect[:3]:
            rp = _resolve_repo_path(r_name, cfg)
            if rp:
                for b_name, desc in _list_git_branches_for_repo(rp):
                    if b_name not in seen_branches:
                        seen_branches.add(b_name)
                        prefix = f"[{r_name}] " if len(repos_to_inspect) > 1 else ""
                        options.append({
                            "value": b_name,
                            "label": f"{prefix}{desc}",
                            "provider": "git",
                        })

        return options

    return []
