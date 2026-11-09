#!/usr/bin/env python3
"""
Headless CLI command implementations for herdr-task.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
from typing import Any
from urllib.parse import urlparse
import uuid

from herdr_task.backend.models import (
    ALLOWED_OPEN_SCHEMES,
    DEFAULT_CONFIG,
    TASK_STATUS_ICONS,
    VALID_TASK_STATUSES,
    TaskPaths,
    now_iso,
    resolve_bin,
    slugify,
)
from herdr_task.backend.storage import (
    GH_PR_URL_RE,
    GH_SHORT_RE,
    atomic_write_json,
    classify_resource,
    collect_active_worktree_paths,
    discover_available_repos,
    fetch_github_pr_metadata,
    list_all_tasks,
    load_task,
    resolve_current_task_id,
    task_store_lock,
    upsert_resource,
    _infer_github_org_for_repo,
)
from herdr_task.backend.worktree import (
    check_worktree_safety,
    ensure_git_worktree,
    resolve_repo_path,
    switch_worktree_branch,
)
from herdr_task.backend.herdr_client import (
    HerdrClient,
    compute_workspace_label,
    provision_herdr_workspace_for_task,
    _provision_tab_cockpit,
)
from herdr_task.backend.integration_engine import (
    dispatch_automation_event,
    discover_automations,
    discover_integrations,
    query_integration_candidates,
    query_integration_field_options,
)


def cmd_create(args: argparse.Namespace, paths: TaskPaths, cfg: dict[str, Any]) -> int:
    spec: dict[str, Any] = {}
    if getattr(args, "spec_stdin", False):
        raw_stdin = sys.stdin.read().strip()
        if raw_stdin:
            spec = json.loads(raw_stdin)

    title = spec.get("title") or getattr(args, "title", None) or "Untitled Task"
    raw_id = spec.get("id") or getattr(args, "id", None)
    brief = spec.get("brief") or getattr(args, "brief", None)
    raw_status = spec.get("status") or getattr(args, "task_status", None)
    explicit_task_status = bool(raw_status)
    initial_task_status = raw_status or "in_progress"
    if initial_task_status not in VALID_TASK_STATUSES:
        initial_task_status = "in_progress"

    raw_resources: list[Any] = list(spec.get("resources") or [])
    for link_val in getattr(args, "link", None) or []:
        raw_resources.append({
            "ref": link_val,
            "label": getattr(args, "label", None),
            "status": getattr(args, "status", None),
            "summary": getattr(args, "summary", None),
        })

    classified_resources: list[dict[str, Any]] = []
    first_ticket_key: str | None = None
    for item in raw_resources:
        if isinstance(item, str):
            res_obj = classify_resource(item, cfg, added_by="pilot")
        elif isinstance(item, dict):
            target_str = (
                item.get("input") or item.get("target") or item.get("url")
                or item.get("ref") or item.get("id") or ""
            )
            if not target_str:
                continue
            res_obj = classify_resource(
                target_str,
                cfg,
                category=item.get("category"),
                provider=item.get("provider"),
                label=item.get("label"),
                status=item.get("status"),
                summary=item.get("summary"),
                added_by=item.get("added_by") or "pilot",
                metadata=item.get("metadata"),
            )
        else:
            continue
        if res_obj["category"] == "ticket" and not first_ticket_key:
            first_ticket_key = res_obj["ref"]
        classified_resources.append(res_obj)

    repo_branch_overrides: dict[str, str] = {}
    inferred_pr_repos: list[str] = []
    for res_obj in list(classified_resources):
        meta = res_obj.get("metadata") or {}
        if meta.get("repo") and meta.get("branch"):
            repo_branch_overrides[meta["repo"]] = meta["branch"]
        if meta.get("repo") and meta["repo"] not in inferred_pr_repos:
            inferred_pr_repos.append(meta["repo"])
        if title == "Untitled Task" and meta.get("pr_title"):
            title = meta["pr_title"]
        jk = meta.get("jira_key")
        if jk and not first_ticket_key:
            first_ticket_key = jk
            if not any(r.get("ref") == jk for r in classified_resources):
                classified_resources.insert(0, classify_resource(jk, cfg, added_by="pilot"))

    if not raw_id:
        rand_suffix = uuid.uuid4().hex[:4]
        if first_ticket_key:
            raw_id = f"{first_ticket_key}-{slugify(title, 24)}-{rand_suffix}"
        else:
            raw_id = f"{slugify(title, 28)}-{rand_suffix}"
    task_id = slugify(raw_id, 48)

    now = now_iso()
    with task_store_lock(paths):
        while paths.task_file(task_id).exists():
            task_id = slugify(f"{task_id[:43]}-{uuid.uuid4().hex[:4]}", 48)
        first_pr_branch = next(iter(repo_branch_overrides.values()), None)
        default_branch = (
            spec.get("branch")
            or spec.get("default_branch")
            or getattr(args, "branch", None)
            or first_pr_branch
            or (f"feat/{task_id}" if first_ticket_key else task_id)
        )
        task: dict[str, Any] = {
            "id": task_id,
            "title": title,
            "brief": brief,
            "status": initial_task_status,
            "created_at": now,
            "updated_at": now,
            "workspace_id": None,
            "default_branch": default_branch,
            "worktrees": [],
            "resources": [],
            "notes": [],
        }
        for res_obj in classified_resources:
            upsert_resource(task, res_obj)
        atomic_write_json(paths.task_file(task_id), task)

    repos: list[str] = []
    if "repos" in spec and isinstance(spec["repos"], list):
        for r in spec["repos"]:
            if isinstance(r, str) and r.strip() and r.strip() not in repos:
                repos.append(r.strip())
            elif isinstance(r, dict):
                r_target = (r.get("path") or r.get("repo") or r.get("name") or "").strip()
                if r_target:
                    if r_target not in repos:
                        repos.append(r_target)
                    if r.get("branch"):
                        repo_branch_overrides[Path(r_target).name] = r["branch"]
                        repo_branch_overrides[r_target] = r["branch"]
    if getattr(args, "repos", None):
        for r in args.repos.split(","):
            if r.strip() and r.strip() not in repos:
                repos.append(r.strip())
    for r_single in getattr(args, "repo_list", None) or []:
        for r in r_single.split(","):
            if r.strip() and r.strip() not in repos:
                repos.append(r.strip())

    if not repos and inferred_pr_repos:
        for cand_repo in inferred_pr_repos:
            try:
                resolve_repo_path(cand_repo, cfg)
                repos.append(cand_repo)
            except Exception:
                pass

    worktrees: list[dict[str, Any]] = []
    used_wt_paths: set[str] = collect_active_worktree_paths(paths, exclude_task_id=task_id)
    try:
        for r_name in repos:
            r_short_guess = Path(r_name).name
            target_branch = (
                repo_branch_overrides.get(r_name)
                or repo_branch_overrides.get(r_short_guess)
                or default_branch
            )
            wt_info = ensure_git_worktree(
                r_name,
                target_branch,
                cfg,
                task_id=task_id,
                used_wt_paths=used_wt_paths,
            )
            used_wt_paths.add(wt_info["worktree_path"])
            worktrees.append({
                **wt_info,
                "workspace_id": None,
                "tab_id": None,
                "pane_id": None,
            })
    except Exception:
        with task_store_lock(paths):
            tf = paths.task_file(task_id)
            if tf.exists():
                tf.unlink()
        raise

    task["worktrees"] = worktrees
    _, auto_msgs = dispatch_automation_event(
        "task.created",
        task,
        paths,
        cfg,
        explicit_task_status=explicit_task_status,
    )

    if not getattr(args, "no_herdr", False):
        try:
            herdr = HerdrClient(cfg)
            provision_herdr_workspace_for_task(
                herdr,
                task,
                focus=bool(getattr(args, "focus", False)),
                cfg=cfg,
            )
        except Exception as e:
            print(f"Warning: could not provision Herdr workspace for task '{task_id}': {e}", file=sys.stderr)

    with task_store_lock(paths):
        task["updated_at"] = now_iso()
        atomic_write_json(paths.task_file(task_id), task)

    if getattr(args, "json", False):
        print(json.dumps(task, indent=2))
    else:
        print(f"Created task '{task_id}' (workspace: {task.get('workspace_id') or 'none'}, repos: {len(worktrees)}, resources: {len(task['resources'])})")
        for m in auto_msgs:
            print(f"  ↳ {m}")
    return 0


def cmd_current(args: argparse.Namespace, paths: TaskPaths, cfg: dict[str, Any]) -> int:
    tid = resolve_current_task_id(paths)
    if not tid:
        if getattr(args, "json", False):
            print(json.dumps({"task_id": None}))
        else:
            print("No active task resolved for current context.", file=sys.stderr)
        return 1
    if getattr(args, "json", False):
        print(json.dumps({"task_id": tid}))
    else:
        print(tid)
    return 0


def cmd_link(args: argparse.Namespace, paths: TaskPaths, cfg: dict[str, Any]) -> int:
    tid = resolve_current_task_id(paths, getattr(args, "task", None))
    if not tid:
        print("Error: could not resolve active task. Pass --task <id>.", file=sys.stderr)
        return 1

    meta_dict: dict[str, Any] = {}
    for kv in getattr(args, "metadata", None) or []:
        if "=" in kv:
            k, v = kv.split("=", 1)
            meta_dict[k.strip()] = v.strip()

    existing_task = load_task(paths, tid)
    if not existing_task:
        print(f"Error: task '{tid}' not found.", file=sys.stderr)
        return 1

    has_partial_update_flags = any(
        getattr(args, f, None) is not None for f in ("label", "status", "summary")
    )
    fetch_remote = True
    if has_partial_update_flags and not getattr(args, "sync_branch", False):
        m_pr = GH_PR_URL_RE.search(args.target.strip())
        pr_ref = f"{m_pr.group(2)}#{m_pr.group(3)}" if m_pr else args.target.strip()
        for r in existing_task.get("resources", []):
            if (r.get("ref") == pr_ref or r.get("url") == args.target.strip()) and (r.get("metadata") or {}).get("branch"):
                fetch_remote = False
                break

    res_obj = classify_resource(
        args.target,
        cfg,
        category=getattr(args, "category", None),
        provider=getattr(args, "provider", None),
        label=getattr(args, "label", None),
        status=getattr(args, "status", None),
        summary=getattr(args, "summary", None),
        added_by=getattr(args, "added_by", None),
        metadata=meta_dict or None,
        fetch_remote=fetch_remote,
    )

    branch_switch_msg: str | None = None
    title_updated = False
    auto_msgs: list[str] = []
    with task_store_lock(paths):
        task = load_task(paths, tid)
        if not task:
            print(f"Error: task '{tid}' not found.", file=sys.stderr)
            return 1
        old_title = task.get("title")
        saved = upsert_resource(task, res_obj)

        _, auto_msgs = dispatch_automation_event(
            "resource.linked",
            task,
            paths,
            cfg,
            resource=saved,
        )

        meta = saved.get("metadata") or {}
        jk = meta.get("jira_key")
        if jk and not any(r.get("category") == "ticket" and r.get("ref") == jk for r in task.get("resources", [])):
            jk_res = upsert_resource(task, classify_resource(jk, cfg, added_by="pilot"))
            _, jk_msgs = dispatch_automation_event("resource.linked", task, paths, cfg, resource=jk_res)
            auto_msgs.extend(jk_msgs)

        if task.get("title") in (None, "", "Untitled Task") and meta.get("pr_title"):
            task["title"] = meta["pr_title"]
        if task.get("title") != old_title:
            title_updated = True

        pr_repo = meta.get("repo")
        pr_branch = meta.get("branch")
        if pr_repo and pr_branch:
            placeholders = {
                b for b in (task.get("id"), task.get("default_branch"), f"feat/{task.get('id')}") if b
            }
            for wt in task.get("worktrees", []):
                if wt.get("repo") == pr_repo and wt.get("branch") != pr_branch:
                    if getattr(args, "sync_branch", False) or wt.get("branch") in placeholders:
                        ok, msg = switch_worktree_branch(wt, pr_branch, cfg, task_id=tid, force=False)
                        if ok:
                            branch_switch_msg = msg
                    break

        task["updated_at"] = now_iso()
        atomic_write_json(paths.task_file(tid), task)

    if not getattr(args, "no_herdr", False):
        herdr = HerdrClient(cfg)
        if title_updated and task.get("workspace_id"):
            herdr.rename_workspace(task["workspace_id"], compute_workspace_label(task))
        herdr.sync_sidebar_metadata(task)

    if getattr(args, "json", False):
        print(json.dumps(saved, indent=2))
    else:
        print(f"Linked [{saved['category']}] {saved['ref']} -> task '{tid}'")
        if branch_switch_msg:
            print(f"  ↳ {branch_switch_msg}")
        for m in auto_msgs:
            print(f"  ↳ {m}")
    return 0


def cmd_unlink(args: argparse.Namespace, paths: TaskPaths, cfg: dict[str, Any]) -> int:
    tid = resolve_current_task_id(paths, getattr(args, "task", None))
    if not tid:
        print("Error: could not resolve active task. Pass --task <id>.", file=sys.stderr)
        return 1

    target = args.target.strip()
    with task_store_lock(paths):
        task = load_task(paths, tid)
        if not task:
            print(f"Error: task '{tid}' not found.", file=sys.stderr)
            return 1
        before = len(task.get("resources", []))
        task["resources"] = [
            r for r in task.get("resources", [])
            if r.get("id") != target and r.get("ref") != target and r.get("url") != target
        ]
        removed = before - len(task["resources"])
        task["updated_at"] = now_iso()
        atomic_write_json(paths.task_file(tid), task)

    if not getattr(args, "no_herdr", False):
        HerdrClient(cfg).sync_sidebar_metadata(task)

    print(f"Removed {removed} resource(s) from '{tid}'.")
    return 0 if removed > 0 else 1


def cmd_note(args: argparse.Namespace, paths: TaskPaths, cfg: dict[str, Any]) -> int:
    tid = resolve_current_task_id(paths, getattr(args, "task", None))
    if not tid:
        print("Error: could not resolve active task. Pass --task <id>.", file=sys.stderr)
        return 1

    author = getattr(args, "author", None) or os.environ.get("USER") or "agent"
    note_entry = {
        "timestamp": now_iso(),
        "author": author,
        "text": args.text.strip(),
    }

    with task_store_lock(paths):
        task = load_task(paths, tid)
        if not task:
            print(f"Error: task '{tid}' not found.", file=sys.stderr)
            return 1
        task.setdefault("notes", []).append(note_entry)
        task["updated_at"] = now_iso()
        atomic_write_json(paths.task_file(tid), task)

    if getattr(args, "json", False):
        print(json.dumps(note_entry, indent=2))
    else:
        print(f"Added handoff note to '{tid}'.")
    return 0


def format_task_status_text(task: dict[str, Any], color: bool = False) -> str:
    c_bold = "\033[1m" if color else ""
    c_cyan = "\033[36m" if color else ""
    c_green = "\033[32m" if color else ""
    c_yellow = "\033[33m" if color else ""
    c_magenta = "\033[35m" if color else ""
    c_dim = "\033[2m" if color else ""
    c_reset = "\033[0m" if color else ""

    raw_st = (task.get("status") or "in_progress").lower()
    st_icon = TASK_STATUS_ICONS.get(raw_st, "🚧")
    st_color = (
        c_green if raw_st == "done"
        else c_magenta if raw_st == "in_review"
        else c_yellow if raw_st == "todo"
        else c_dim if raw_st == "archived"
        else c_cyan
    )

    lines = [
        f"{c_bold}{st_color}{st_icon} [{raw_st.upper()}] {task['id']}: {task.get('title', '')}{c_reset}  {c_dim}(ws: {task.get('workspace_id') or 'closed'}){c_reset}",
    ]
    if task.get("brief"):
        lines.append(f"{c_bold}Brief:{c_reset} {task['brief']}")

    lines.append("")
    wts = task.get("worktrees", [])
    lines.append(f"{c_bold}Repository Worktrees ({len(wts)}):{c_reset}")
    if not wts:
        lines.append(f"  {c_dim}(No repo worktrees attached){c_reset}")
    for wt in wts:
        tab_str = wt.get("workspace_id") or wt.get("tab_id") or "no-ws"
        lines.append(f"  ↳ [{tab_str}] {c_bold}{wt['repo']}{c_reset} ({c_yellow}{wt.get('branch', '')}{c_reset})")
        lines.append(f"           {c_dim}{wt.get('worktree_path', '')}{c_reset}")

    resources = task.get("resources", [])
    by_cat: dict[str, list[dict[str, Any]]] = {}
    for r in resources:
        by_cat.setdefault(r.get("category", "custom"), []).append(r)

    cat_titles = [
        ("ticket", "Tickets"),
        ("pull_request", "Pull Requests"),
        ("issue", "Issues"),
        ("alert", "Alerts & Monitors"),
        ("conversation", "Conversations"),
        ("doc", "Documentation & Specs"),
        ("custom", "Other Resources"),
    ]
    for cat_key, cat_title in cat_titles:
        items = by_cat.get(cat_key, [])
        if not items:
            continue
        lines.append("")
        lines.append(f"{c_bold}{cat_title} ({len(items)}):{c_reset}")
        for r in items:
            badge = f" [{r['status']}]" if r.get("status") else ""
            lines.append(f"  • {c_bold}{r.get('ref')}{c_reset}{c_yellow}{badge}{c_reset} — {r.get('label', '')}")
            lines.append(f"    {c_dim}{r.get('url', '')}{c_reset}")
            if r.get("summary"):
                lines.append(f"    {c_cyan}Summary:{c_reset} {r['summary']}")

    notes = task.get("notes", [])
    if notes:
        lines.append("")
        lines.append(f"{c_bold}Cross-Repo Handoff Notes ({len(notes)}):{c_reset}")
        for n in notes:
            lines.append(f"  > [{n.get('author', 'note')}] {n.get('text', '')}")

    return "\n".join(lines)


def cmd_status(args: argparse.Namespace, paths: TaskPaths, cfg: dict[str, Any]) -> int:
    tid = resolve_current_task_id(paths, getattr(args, "task_id", None), include_archived=True)
    if not tid:
        print("Error: could not resolve task. Pass <task-id>.", file=sys.stderr)
        return 1
    task = load_task(paths, tid, include_archived=True)
    if not task:
        print(f"Error: task '{tid}' not found.", file=sys.stderr)
        return 1

    if getattr(args, "json", False):
        print(json.dumps(task, indent=2))
    else:
        print(format_task_status_text(task, color=bool(getattr(args, "color", False))))
    return 0


def format_fzf_task_line(t: dict[str, Any]) -> str:
    repos_seen: list[str] = []
    for w in t.get("worktrees", []):
        if w["repo"] not in repos_seen:
            repos_seen.append(w["repo"])
    repos = ",".join(repos_seen) or "no-repo"
    tickets = ",".join(
        r["ref"] for r in t.get("resources", []) if r.get("category") == "ticket"
    ) or "-"
    ws = t.get("workspace_id") or "-"
    st = (t.get("status") or "in_progress").lower()
    st_icon = TASK_STATUS_ICONS.get(st, "🚧")
    branches = " ".join(w.get("branch", "") for w in t.get("worktrees", []))
    res_tokens = " ".join(
        f"{r.get('ref', '')} {r.get('label', '')} {r.get('provider', '')} {r.get('status', '')}"
        for r in t.get("resources", [])
    )
    search_blob = f"{st} {branches} {res_tokens}".replace("\t", " ").replace("\n", " ")
    return f"{t['id']}\t{st_icon} [{st.upper()}] [{ws}] {t.get('title', '')}  (repos: {repos} | tickets: {tickets})\t{search_blob}"


def cmd_list(args: argparse.Namespace, paths: TaskPaths, cfg: dict[str, Any]) -> int:
    tasks = list_all_tasks(paths, include_archived=bool(getattr(args, "all", False)))
    if getattr(args, "json", False):
        print(json.dumps(tasks, indent=2))
        return 0
    if getattr(args, "fzf", False):
        for t in tasks:
            print(format_fzf_task_line(t))
        return 0

    if not tasks:
        print("No tasks found.")
        return 0
    for t in tasks:
        repos = ", ".join(w["repo"] for w in t.get("worktrees", [])) or "none"
        res_cnt = len(t.get("resources", []))
        st = (t.get("status") or "in_progress").upper()
        print(f"{t['id']:32} | {st:11} | ws: {str(t.get('workspace_id') or '-'):5} | repos: {repos:24} | resources: {res_cnt} | {t.get('title', '')}")
    return 0


def cmd_focus(args: argparse.Namespace, paths: TaskPaths, cfg: dict[str, Any]) -> int:
    tid = resolve_current_task_id(paths, getattr(args, "task_id", None))
    if not tid:
        print("Error: could not resolve task.", file=sys.stderr)
        return 1

    task = load_task(paths, tid)
    if not task:
        print(f"Error: task '{tid}' not found.", file=sys.stderr)
        return 1

    herdr = HerdrClient(cfg)
    live_ws_ids = {w.get("workspace_id") for w in herdr.list_workspaces()}
    ws_id = task.get("workspace_id")

    if not ws_id or ws_id not in live_ws_ids:
        provision_herdr_workspace_for_task(herdr, task, focus=True, cfg=cfg)
        with task_store_lock(paths):
            fresh = load_task(paths, tid) or task
            fresh["workspace_id"] = task.get("workspace_id")
            fresh["worktrees"] = task.get("worktrees", [])
            fresh["updated_at"] = now_iso()
            atomic_write_json(paths.task_file(tid), fresh)
            task = fresh
    else:
        live_tab_ids = {t.get("tab_id") for t in herdr.list_tabs(ws_id)}
        updated = False
        for wt in task.get("worktrees", []):
            if (not wt.get("tab_id") or wt["tab_id"] not in live_tab_ids) and Path(wt["worktree_path"]).exists():
                wt_label = wt.get("tab_title") or wt["repo"]
                t_res = herdr.create_tab(ws_id, wt["worktree_path"], wt_label, tid, focus=False)
                if t_res:
                    wt["tab_id"] = (t_res.get("tab") or {}).get("tab_id")
                    _provision_tab_cockpit(herdr, wt, (t_res.get("root_pane") or {}).get("pane_id"), cfg)
                    updated = True
        if updated:
            with task_store_lock(paths):
                fresh = load_task(paths, tid) or task
                fresh["worktrees"] = task.get("worktrees", [])
                fresh["updated_at"] = now_iso()
                atomic_write_json(paths.task_file(tid), fresh)
                task = fresh

        target_repo = getattr(args, "repo", None)
        if target_repo:
            focused = False
            for wt in task.get("worktrees", []):
                if wt["repo"] == target_repo and wt.get("tab_id"):
                    herdr.focus_tab(wt["tab_id"])
                    focused = True
                    break
            if not focused:
                herdr.focus_workspace(ws_id)
        else:
            herdr.focus_workspace(ws_id)
        herdr.sync_sidebar_metadata(task)

    return 0


def cmd_worktree_add(args: argparse.Namespace, paths: TaskPaths, cfg: dict[str, Any]) -> int:
    tid = resolve_current_task_id(paths, getattr(args, "task", None))
    if not tid:
        print("Error: could not resolve active task. Pass --task <id>.", file=sys.stderr)
        return 1

    with task_store_lock(paths):
        task = load_task(paths, tid)
        if not task:
            print(f"Error: task '{tid}' not found.", file=sys.stderr)
            return 1

        explicit_branch = getattr(args, "branch", None)
        branch = explicit_branch or task.get("default_branch") or f"feat/{tid}"
        repo_short, _ = resolve_repo_path(args.repo, cfg)

        existing_same = next(
            (
                w for w in task.get("worktrees", [])
                if w.get("repo") == repo_short and (not explicit_branch or w.get("branch") == branch)
            ),
            None,
        )
        if existing_same and Path(existing_same.get("worktree_path", "")).exists():
            ws_id = task.get("workspace_id")
        else:
            existing_same = None
            used_wt_paths = collect_active_worktree_paths(paths, exclude_task_id=tid) | {
                w["worktree_path"] for w in task.get("worktrees", []) if w.get("worktree_path")
            }
            wt_info = ensure_git_worktree(args.repo, branch, cfg, task_id=tid, used_wt_paths=used_wt_paths)
            repo_short = wt_info["repo"]

            existing_tabs_for_repo = [w for w in task.get("worktrees", []) if w["repo"] == repo_short]
            tab_index = len(existing_tabs_for_repo) + 1
            tab_title = getattr(args, "label", None) or (repo_short if tab_index == 1 else f"{repo_short} #{tab_index}")

            new_wt: dict[str, Any] = {
                **wt_info,
                "tab_title": tab_title,
                "workspace_id": None,
                "tab_id": None,
                "pane_id": None,
            }
            task.setdefault("worktrees", []).append(new_wt)
            task["updated_at"] = now_iso()
            atomic_write_json(paths.task_file(tid), task)
            ws_id = task.get("workspace_id")

    if existing_same is not None:
        if not getattr(args, "no_herdr", False) and ws_id:
            herdr = HerdrClient(cfg)
            live_tab_ids = {t.get("tab_id") for t in herdr.list_tabs(ws_id)}
            if not existing_same.get("tab_id") or existing_same["tab_id"] not in live_tab_ids:
                t_res = herdr.create_tab(
                    ws_id,
                    existing_same["worktree_path"],
                    existing_same.get("tab_title") or repo_short,
                    tid,
                    focus=bool(getattr(args, "focus", False)),
                )
                if t_res:
                    existing_same["tab_id"] = (t_res.get("tab") or {}).get("tab_id")
                    _provision_tab_cockpit(herdr, existing_same, (t_res.get("root_pane") or {}).get("pane_id"), cfg)
                with task_store_lock(paths):
                    fresh = load_task(paths, tid)
                    if fresh:
                        for w in fresh.get("worktrees", []):
                            if w.get("worktree_path") == existing_same["worktree_path"]:
                                w["tab_id"] = existing_same.get("tab_id")
                                w["pane_id"] = existing_same.get("pane_id")
                                if "cockpit_panes" in existing_same:
                                    w["cockpit_panes"] = existing_same["cockpit_panes"]
                        fresh["updated_at"] = now_iso()
                        atomic_write_json(paths.task_file(tid), fresh)
            elif getattr(args, "focus", False):
                if existing_same.get("tab_id"):
                    herdr.focus_tab(existing_same["tab_id"])
        if getattr(args, "json", False):
            print(json.dumps(existing_same, indent=2))
        else:
            print(f"Worktree '{repo_short}' ({existing_same['worktree_path']}) is already attached to task '{tid}'.")
        return 0

    if not getattr(args, "no_herdr", False) and ws_id:
        herdr = HerdrClient(cfg)
        t_res = herdr.create_tab(
            ws_id,
            new_wt["worktree_path"],
            tab_title,
            tid,
            focus=bool(getattr(args, "focus", False)),
        )
        if t_res:
            new_wt["tab_id"] = (t_res.get("tab") or {}).get("tab_id")
            _provision_tab_cockpit(herdr, new_wt, (t_res.get("root_pane") or {}).get("pane_id"), cfg)

        with task_store_lock(paths):
            fresh = load_task(paths, tid)
            if fresh:
                for w in fresh.get("worktrees", []):
                    if w.get("worktree_path") == new_wt["worktree_path"]:
                        w["tab_id"] = new_wt.get("tab_id")
                        w["pane_id"] = new_wt.get("pane_id")
                        if "cockpit_panes" in new_wt:
                            w["cockpit_panes"] = new_wt["cockpit_panes"]
                fresh["updated_at"] = now_iso()
                atomic_write_json(paths.task_file(tid), fresh)
                task = fresh

    if not getattr(args, "no_herdr", False):
        HerdrClient(cfg).sync_sidebar_metadata(task)

    if getattr(args, "json", False):
        print(json.dumps(new_wt, indent=2))
    else:
        print(f"Added worktree '{repo_short}' ({new_wt['worktree_path']}) to task '{tid}'.")
    return 0


def cmd_worktree_branch(args: argparse.Namespace, paths: TaskPaths, cfg: dict[str, Any]) -> int:
    tid = resolve_current_task_id(paths, getattr(args, "task", None))
    if not tid:
        print("Error: could not resolve active task. Pass --task <id>.", file=sys.stderr)
        return 1

    target_repo = Path(args.repo).name
    target_branch = args.branch.strip()
    force = bool(getattr(args, "force", False))

    with task_store_lock(paths):
        task = load_task(paths, tid)
        if not task:
            print(f"Error: task '{tid}' not found.", file=sys.stderr)
            return 1

        target_wt = next(
            (w for w in task.get("worktrees", []) if w["repo"] == target_repo or w["repo"] == args.repo),
            None,
        )
        if not target_wt:
            print(f"Error: repo '{args.repo}' is not attached to task '{tid}'.", file=sys.stderr)
            return 1

        ok, msg = switch_worktree_branch(target_wt, target_branch, cfg, task_id=tid, force=force)
        if not ok:
            print(f"Error: {msg}", file=sys.stderr)
            return 1

        task["updated_at"] = now_iso()
        atomic_write_json(paths.task_file(tid), task)

    if getattr(args, "json", False):
        print(json.dumps(target_wt, indent=2))
    else:
        print(msg)
    return 0


def cmd_update(args: argparse.Namespace, paths: TaskPaths, cfg: dict[str, Any]) -> int:
    tid = resolve_current_task_id(paths, getattr(args, "task_id", None) or getattr(args, "task", None))
    if not tid:
        print("Error: could not resolve task to update. Pass <task-id> or --task <id>.", file=sys.stderr)
        return 1

    messages: list[str] = []
    title_changed = False
    force = bool(getattr(args, "force", False))

    with task_store_lock(paths):
        task = load_task(paths, tid)
        if not task:
            print(f"Error: task '{tid}' not found.", file=sys.stderr)
            return 1

        new_title = getattr(args, "title", None)
        if new_title is not None and new_title.strip():
            task["title"] = new_title.strip()
            title_changed = True
            messages.append(f"Updated title -> '{task['title']}'")

        new_brief = getattr(args, "brief", None)
        if new_brief is not None:
            task["brief"] = None if new_brief.strip() == "-" else new_brief.strip()
            messages.append("Updated task brief")

        new_status = getattr(args, "status", None)
        if new_status is not None and new_status.strip():
            norm_st = new_status.strip().lower().replace("-", "_").replace(" ", "_")
            if norm_st not in VALID_TASK_STATUSES:
                print(
                    f"Error: invalid status '{new_status}'. Choose from: {', '.join(VALID_TASK_STATUSES)}",
                    file=sys.stderr,
                )
                return 1
            old_status = task.get("status") or "in_progress"
            if norm_st != old_status:
                task["status"] = norm_st
                if norm_st == "archived":
                    task["archived_at"] = now_iso()
                messages.append(f"Updated task status: '{old_status}' -> '{norm_st}'")
                _, auto_msgs = dispatch_automation_event(
                    "task.status_changed",
                    task,
                    paths,
                    cfg,
                    previous_status=old_status,
                    new_status=norm_st,
                )
                messages.extend(auto_msgs)

        new_def_branch = getattr(args, "branch", None)
        if new_def_branch is not None and new_def_branch.strip():
            task["default_branch"] = new_def_branch.strip()
            messages.append(f"Updated default_branch -> '{task['default_branch']}'")

        repo_branch_map: dict[str, str] = {}
        if getattr(args, "sync_pr_branches", False):
            for r in task.get("resources", []):
                if r.get("category") == "pull_request":
                    meta = r.get("metadata") or {}
                    if not meta.get("branch") and r.get("url"):
                        m = GH_PR_URL_RE.search(r["url"])
                        if m:
                            gh_meta = fetch_github_pr_metadata(m.group(1), m.group(2), m.group(3))
                            if gh_meta.get("branch"):
                                meta["branch"] = gh_meta["branch"]
                                meta["repo"] = m.group(2)
                                r["metadata"] = meta
                    if meta.get("repo") and meta.get("branch"):
                        repo_branch_map[meta["repo"]] = meta["branch"]

        for rb in getattr(args, "repo_branch", None) or []:
            sep = ":" if ":" in rb else ("=" if "=" in rb else None)
            if sep:
                rk, bval = rb.split(sep, 1)
                if rk.strip() and bval.strip():
                    repo_branch_map[Path(rk.strip()).name] = bval.strip()

        if new_def_branch and not repo_branch_map and len(task.get("worktrees", [])) == 1:
            only_repo = task["worktrees"][0]["repo"]
            repo_branch_map[only_repo] = new_def_branch.strip()

        branch_errors = False
        for repo_key, target_b in repo_branch_map.items():
            target_wt = next((w for w in task.get("worktrees", []) if w["repo"] == repo_key), None)
            if target_wt:
                ok, msg = switch_worktree_branch(target_wt, target_b, cfg, task_id=tid, force=force)
                if ok:
                    messages.append(msg)
                else:
                    branch_errors = True
                    messages.append(f"Error: {msg}")
            else:
                branch_errors = True
                messages.append(f"Warning: repo '{repo_key}' is not attached to task '{tid}'")

        task["updated_at"] = now_iso()
        if task.get("status") == "archived":
            atomic_write_json(paths.archive_file(tid), task)
            active_f = paths.task_file(tid)
            if active_f.exists():
                active_f.unlink()
        else:
            atomic_write_json(paths.task_file(tid), task)

    if not getattr(args, "no_herdr", False) and task.get("workspace_id"):
        herdr = HerdrClient(cfg)
        if title_changed:
            herdr.rename_workspace(task["workspace_id"], compute_workspace_label(task))
        herdr.sync_sidebar_metadata(task)

    if getattr(args, "json", False):
        print(json.dumps(task, indent=2))
    else:
        for m in messages or [f"Task '{tid}' is up to date."]:
            stream = sys.stderr if m.startswith("Error:") else sys.stdout
            print(f"• {m}", file=stream)
    return 1 if branch_errors else 0


def cmd_sync(args: argparse.Namespace, paths: TaskPaths, cfg: dict[str, Any]) -> int:
    tid = resolve_current_task_id(paths, getattr(args, "task_id", None) or getattr(args, "task", None))
    if not tid:
        print("Error: could not resolve task to sync. Pass <task-id>.", file=sys.stderr)
        return 1

    messages: list[str] = []
    with task_store_lock(paths):
        task = load_task(paths, tid)
        if not task:
            print(f"Error: task '{tid}' not found.", file=sys.stderr)
            return 1

        modified = False
        for r in task.get("resources", []):
            if r.get("category") != "pull_request":
                continue
            meta = r.setdefault("metadata", {})
            org: str | None = None
            repo: str | None = None
            num: str | None = None
            m = GH_PR_URL_RE.search(r.get("url") or "")
            if m:
                org, repo, num = m.group(1), m.group(2), m.group(3)
            else:
                m_sh = GH_SHORT_RE.match(r.get("ref") or "")
                if m_sh:
                    repo = m_sh.group(2)
                    num = m_sh.group(3)
                    org = m_sh.group(1) or meta.get("org") or _infer_github_org_for_repo(repo, cfg)
            if org and repo and num:
                gh_meta = fetch_github_pr_metadata(org, repo, num)
                if gh_meta:
                    res_mod = False
                    if not r.get("url") or not r["url"].startswith("http"):
                        r["url"] = f"https://github.com/{org}/{repo}/pull/{num}"
                        res_mod = True
                    if gh_meta.get("status") and r.get("status") != gh_meta["status"]:
                        r["status"] = gh_meta["status"]
                        res_mod = True
                    if gh_meta.get("branch") and meta.get("branch") != gh_meta["branch"]:
                        meta["branch"] = gh_meta["branch"]
                        res_mod = True
                    if gh_meta.get("title") and meta.get("pr_title") != gh_meta["title"]:
                        meta["pr_title"] = gh_meta["title"]
                        res_mod = True
                    if gh_meta.get("title") and (not r.get("label") or r.get("label") in (f"{repo}#{num}", f"{repo} #{num}")):
                        r["label"] = f"{repo}#{num}: {gh_meta['title']}"
                        res_mod = True
                    if gh_meta.get("summary") and not r.get("summary"):
                        r["summary"] = gh_meta["summary"]
                        res_mod = True
                    if res_mod:
                        r["updated_at"] = now_iso()
                        modified = True
                    messages.append(f"[github] Synced {r.get('ref')} -> [{r.get('status') or 'OPEN'}]")

        auto_mod, auto_msgs = dispatch_automation_event("task.sync", task, paths, cfg)
        if auto_mod:
            modified = True
        messages.extend(auto_msgs)

        if modified:
            task["updated_at"] = now_iso()
            atomic_write_json(paths.task_file(tid), task)

    if not getattr(args, "no_herdr", False) and task.get("workspace_id"):
        HerdrClient(cfg).sync_sidebar_metadata(task)

    if getattr(args, "json", False):
        print(json.dumps({"task": task, "messages": messages}, indent=2))
    else:
        for m in messages or [f"Task '{tid}' is up to date."]:
            print(f"• {m}")
    return 0


def cmd_automations(args: argparse.Namespace, paths: TaskPaths, cfg: dict[str, Any]) -> int:
    autos = discover_automations(paths, cfg)
    out_list = [
        {
            "id": a["id"],
            "enabled": a["enabled"],
            "description": a["description"],
            "events": a["events"],
            "path": a["path"],
        }
        for a in autos
    ]
    if getattr(args, "json", False):
        print(json.dumps(out_list, indent=2))
        return 0

    if not out_list:
        print("No lifecycle automation modules found.")
        return 0

    for a in out_list:
        st_str = "ENABLED" if a["enabled"] else "DISABLED"
        ev_str = ", ".join(a["events"])
        print(f"• {a['id']:22} [{st_str:8}] — {a['description']}")
        print(f"  events: {ev_str}")
        print(f"  path:   {a['path']}")
    return 0


def cmd_integrations(args: argparse.Namespace, paths: TaskPaths, cfg: dict[str, Any]) -> int:
    action = getattr(args, "integ_action", None) or "list"
    target = getattr(args, "target", None)
    tid = resolve_current_task_id(paths, getattr(args, "task", None), include_archived=True)
    task = load_task(paths, tid, include_archived=True) if tid else None

    if action == "list":
        integs = discover_integrations(paths, cfg)
        out_list = [
            {
                "id": i["id"],
                "enabled": i["enabled"],
                "description": i["description"],
                "capabilities": i["capabilities"],
                "path": i["path"],
            }
            for i in integs
        ]
        if getattr(args, "json", False):
            print(json.dumps(out_list, indent=2))
            return 0
        if not out_list:
            print("No modular integration providers found.")
            return 0
        for i in out_list:
            st_str = "ENABLED" if i["enabled"] else "DISABLED"
            caps_str = ", ".join(i["capabilities"])
            print(f"• {i['id']:22} [{st_str:8}] — {i['description']}")
            print(f"  capabilities: {caps_str}")
            print(f"  path:         {i['path']}")
        return 0

    if action == "candidates":
        kind = (target or "resource").strip()
        cands = query_integration_candidates(kind, paths, cfg, task=task)
        if getattr(args, "json", False):
            print(json.dumps(cands, indent=2))
            return 0
        if not cands:
            print(f"No candidates returned for '{kind}'.")
            return 0
        for c in cands:
            print(f"{c.get('value', ''):36} | {c.get('label', '')}")
        return 0

    if action == "options":
        field = (target or "resource.status").strip()
        res_ctx = {
            "category": getattr(args, "category", None),
            "provider": getattr(args, "provider", None),
            "ref": getattr(args, "ref", None),
        }
        opts = query_integration_field_options(
            field,
            paths,
            cfg,
            task=task,
            resource=res_ctx,
            category=getattr(args, "category", None),
            provider=getattr(args, "provider", None),
            repo=getattr(args, "repo", None),
        )
        if getattr(args, "json", False):
            print(json.dumps(opts, indent=2))
            return 0
        if not opts:
            print(f"No field options returned for '{field}'.")
            return 0
        for o in opts:
            print(f"{o.get('value', ''):28} | {o.get('label', '')}")
        return 0

    return 1


def cmd_add(args: argparse.Namespace, paths: TaskPaths, cfg: dict[str, Any]) -> int:
    tid = resolve_current_task_id(paths, getattr(args, "task", None))
    if not tid:
        print("Error: could not resolve active task. Pass --task <id>.", file=sys.stderr)
        return 1

    repo = getattr(args, "repo", None)
    if not repo:
        fzf_bin = resolve_bin("fzf", cfg, "fzf_bin")
        repos = discover_available_repos(cfg)
        if not repos:
            print("No repositories found in configured repo_roots.", file=sys.stderr)
            return 1

        res = subprocess.run(
            [
                fzf_bin, "-m",
                "--prompt=Add Repository > ",
                "--header=[Tab] Select Multiple Repos   [Enter] Confirm Selection",
            ],
            input="\n".join(repos),
            capture_output=True,
            text=True,
            check=False,
        )
        if res.returncode != 0 or not res.stdout.strip():
            return 0
        rc_final = 0
        for r_name in [r.strip() for r in res.stdout.strip().splitlines() if r.strip()]:
            rc = cmd_worktree_add(
                argparse.Namespace(
                    task=tid,
                    repo=r_name,
                    branch=getattr(args, "branch", None),
                    label=None,
                    focus=getattr(args, "focus", True),
                    no_herdr=getattr(args, "no_herdr", False),
                    json=getattr(args, "json", False),
                ),
                paths,
                cfg,
            )
            if rc != 0:
                rc_final = rc
        return rc_final

    return cmd_worktree_add(
        argparse.Namespace(
            task=tid,
            repo=repo,
            branch=getattr(args, "branch", None),
            label=None,
            focus=getattr(args, "focus", True),
            no_herdr=getattr(args, "no_herdr", False),
            json=getattr(args, "json", False),
        ),
        paths,
        cfg,
    )


def open_url_target(url: str) -> bool:
    target = url.strip()
    if not target or target.startswith("-"):
        return False
    parsed = urlparse(target)
    if parsed.scheme.lower() not in ALLOWED_OPEN_SCHEMES:
        print(
            f"Error: refusing to open unsupported URL scheme '{parsed.scheme or 'none'}': {target}",
            file=sys.stderr,
        )
        return False
    opener = "open" if sys.platform == "darwin" else "xdg-open"
    res = subprocess.run([opener, target], capture_output=True, check=False)
    return res.returncode == 0


def cmd_open(args: argparse.Namespace, paths: TaskPaths, cfg: dict[str, Any]) -> int:
    tid = resolve_current_task_id(paths, getattr(args, "task", None), include_archived=True)
    if not tid:
        print("Error: could not resolve active task. Pass --task <id>.", file=sys.stderr)
        return 1
    task = load_task(paths, tid, include_archived=True)
    if not task:
        print(f"Error: task '{tid}' not found.", file=sys.stderr)
        return 1

    resources = task.get("resources", [])
    if not resources:
        print(f"No resources linked to task '{tid}'.")
        return 1

    selector = (getattr(args, "selector", None) or "").strip().lower()
    cat_aliases = {
        "jira": "ticket", "ticket": "ticket", "tickets": "ticket",
        "slack": "conversation", "conversation": "conversation",
        "pr": "pull_request", "prs": "pull_request", "pull_request": "pull_request",
        "alert": "alert", "alerts": "alert",
        "doc": "doc", "docs": "doc",
    }

    matched: list[dict[str, Any]] = []
    if not selector:
        fzf_bin = resolve_bin("fzf", cfg, "fzf_bin")
        if (shutil.which(fzf_bin) or os.path.exists(fzf_bin)) and sys.stdin.isatty():
            lines = [
                f"{r['id']}\t[{r.get('category')}] {r.get('ref')} — {r.get('label', '')} ({r.get('url', '')})"
                for r in resources
            ]
            res = subprocess.run(
                [fzf_bin, "--with-nth=2..", "--delimiter=\t", "--prompt=Open Resource > "],
                input="\n".join(lines),
                capture_output=True,
                text=True,
                check=False,
            )
            if res.returncode == 0 and res.stdout.strip():
                chosen_id = res.stdout.strip().split("\t", 1)[0]
                matched = [r for r in resources if r.get("id") == chosen_id]
        else:
            matched = resources[:1]
    elif selector in cat_aliases:
        target_cat = cat_aliases[selector]
        matched = [r for r in resources if r.get("category") == target_cat]
    else:
        matched = [
            r for r in resources
            if selector in (r.get("id") or "").lower()
            or selector in (r.get("ref") or "").lower()
            or selector in (r.get("provider") or "").lower()
            or selector in (r.get("label") or "").lower()
        ]

    if not matched:
        print(f"No matching resources for '{selector}' in task '{tid}'.", file=sys.stderr)
        return 1

    opened_any = False
    for r in matched:
        target_url = r.get("url") or r.get("ref") or ""
        if target_url and open_url_target(target_url):
            print(f"Opened: {target_url}")
            opened_any = True
    return 0 if opened_any else 1


def _remove_task_worktrees_on_disk(
    task: dict[str, Any],
    paths: TaskPaths,
    cfg: dict[str, Any],
    delete_branches: bool = False,
) -> None:
    tid = task["id"]
    other_tasks = [t for t in list_all_tasks(paths) if t.get("id") != tid]
    in_use_wt_paths: set[str] = set()
    in_use_repo_branches: set[tuple[str, str]] = set()
    for ot in other_tasks:
        for owt in ot.get("worktrees", []):
            if owt.get("worktree_path"):
                in_use_wt_paths.add(os.path.realpath(owt["worktree_path"]))
            if owt.get("repo") and owt.get("branch"):
                in_use_repo_branches.add((owt["repo"], owt["branch"]))

    for wt in task.get("worktrees", []):
        wt_path_str = wt.get("worktree_path", "")
        if not wt_path_str:
            continue
        wt_path = Path(wt_path_str)
        real_wt_path = os.path.realpath(str(wt_path))
        if real_wt_path in in_use_wt_paths:
            continue

        repo_path = Path(wt.get("repo_path", ""))
        if wt_path.exists() and repo_path.exists():
            rm_cmd = ["git", "-C", str(repo_path), "worktree", "remove", "--force", str(wt_path)]
            res = subprocess.run(rm_cmd, capture_output=True, text=True, check=False)
            if res.returncode != 0 and wt_path.exists():
                shutil.rmtree(wt_path, ignore_errors=True)
        elif wt_path.exists():
            shutil.rmtree(wt_path, ignore_errors=True)

        if repo_path.exists():
            subprocess.run(["git", "-C", str(repo_path), "worktree", "prune"], capture_output=True, check=False)
            branch_name = wt.get("branch") or ""
            if (
                delete_branches
                and branch_name
                and not branch_name.startswith("-")
                and (wt.get("repo"), branch_name) not in in_use_repo_branches
            ):
                subprocess.run(
                    ["git", "-C", str(repo_path), "branch", "-D", "--", branch_name],
                    capture_output=True,
                    check=False,
                )

    wt_root = Path(os.path.expanduser(cfg.get("worktrees_root", DEFAULT_CONFIG["worktrees_root"])))
    task_wt_dir = wt_root / slugify(tid, max_len=48)
    if task_wt_dir.is_dir():
        try:
            task_wt_dir.rmdir()
        except OSError:
            pass


def cmd_cleanup(args: argparse.Namespace, paths: TaskPaths, cfg: dict[str, Any]) -> int:
    try:
        signal.signal(signal.SIGHUP, signal.SIG_IGN)
    except Exception:
        pass

    tid = resolve_current_task_id(paths, getattr(args, "task_id", None))
    if not tid:
        print("Error: could not resolve task to clean up. Pass <task-id>.", file=sys.stderr)
        return 1

    task = load_task(paths, tid)
    if not task:
        print(f"Error: active task '{tid}' not found.", file=sys.stderr)
        return 1

    force = bool(getattr(args, "force", False))

    dirty_reports: list[str] = []
    for wt in task.get("worktrees", []):
        safety = check_worktree_safety(wt.get("worktree_path", ""))
        if not safety["clean"]:
            dirty_reports.append(
                f"  - {wt['repo']} ({wt['worktree_path']}): {len(safety['dirty_files'])} uncommitted/untracked file(s)"
            )

    if dirty_reports and not force:
        msg = (
            f"Safety Audit Blocked Cleanup for '{tid}':\n"
            + "\n".join(dirty_reports)
            + "\nCommit/stash your changes or re-run with --force."
        )
        print(msg, file=sys.stderr)
        return 1

    ws_id = task.get("workspace_id")
    if ws_id and not getattr(args, "no_herdr", False):
        HerdrClient(cfg).close_workspace(ws_id)

    _remove_task_worktrees_on_disk(
        task,
        paths,
        cfg,
        delete_branches=bool(getattr(args, "delete_branches", False)),
    )

    auto_msgs: list[str] = []
    with task_store_lock(paths):
        fresh_task = load_task(paths, tid)
        if fresh_task:
            task = fresh_task
        old_status = task.get("status") or "in_progress"
        task["status"] = "archived"
        task["workspace_id"] = None
        for wt in task.get("worktrees", []):
            wt["workspace_id"] = None
            wt["tab_id"] = None
            wt["pane_id"] = None
            wt.pop("cockpit_panes", None)
        task["archived_at"] = now_iso()
        task["updated_at"] = now_iso()
        _, auto_msgs = dispatch_automation_event(
            "task.cleanup",
            task,
            paths,
            cfg,
            previous_status=old_status,
            new_status="archived",
        )
        atomic_write_json(paths.archive_file(tid), task)
        active_file = paths.task_file(tid)
        if active_file.exists():
            active_file.unlink()

    if getattr(args, "json", False):
        print(json.dumps({"archived": tid, "status": "archived", "messages": auto_msgs}, indent=2))
    else:
        print(f"Cleaned up and archived task '{tid}'.")
        for m in auto_msgs:
            print(f"  ↳ {m}")
    return 0


def cmd_sync_sidebar(args: argparse.Namespace, paths: TaskPaths, cfg: dict[str, Any]) -> int:
    herdr = HerdrClient(cfg)
    live_ws_ids = {w.get("workspace_id") for w in herdr.list_workspaces()}
    synced = 0
    for task in list_all_tasks(paths):
        if task.get("workspace_id") in live_ws_ids:
            if herdr.sync_sidebar_metadata(task):
                synced += 1
    if getattr(args, "json", False):
        print(json.dumps({"synced": synced}))
    return 0
