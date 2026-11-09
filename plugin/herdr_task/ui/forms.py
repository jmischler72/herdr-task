#!/usr/bin/env python3
"""
Interactive Field-Navigator Forms and integration dropdown pickers for herdr-task.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
from typing import Any

from herdr_task.backend.models import (
    TASK_STATUS_ICONS,
    VALID_TASK_STATUSES,
    TaskPaths,
    now_iso,
    resolve_bin,
    slugify,
)
from herdr_task.backend.storage import (
    classify_resource,
    discover_available_repos,
    load_task,
    resolve_current_task_id,
    task_store_lock,
    atomic_write_json,
)
from herdr_task.backend.worktree import resolve_repo_path
from herdr_task.backend.herdr_client import HerdrClient
from herdr_task.backend.integration_engine import (
    enrich_draft_resource_via_integrations,
    query_integration_candidates,
    query_integration_field_options,
)
from herdr_task.backend.commands import (
    cmd_create,
    cmd_link,
    cmd_update,
    cmd_worktree_add,
    open_url_target,
)


KNOWN_RESOURCE_CATEGORIES: list[tuple[str, str]] = [
    ("ticket", "🎫 ticket — Jira / Issue Tracker Ticket"),
    ("pull_request", "🔀 pull_request — GitHub Pull Request"),
    ("issue", "🐛 issue — GitHub / Tracker Issue"),
    ("conversation", "💬 conversation — Slack Thread / Chat"),
    ("alert", "🚨 alert — Grafana / Datadog / Sentry Alert"),
    ("doc", "📄 doc — Notion / Confluence / Figma / Spec"),
    ("custom", "🔗 custom — General Web URL / Reference"),
]

KNOWN_RESOURCE_PROVIDERS: list[tuple[str, str]] = [
    ("jira", "🎫 jira — Atlassian Jira"),
    ("github", "🔀 github — GitHub"),
    ("slack", "💬 slack — Slack"),
    ("grafana", "🚨 grafana — Grafana"),
    ("datadog", "🚨 datadog — Datadog"),
    ("sentry", "🚨 sentry — Sentry"),
    ("notion", "📄 notion — Notion"),
    ("confluence", "📄 confluence — Atlassian Confluence"),
    ("figma", "🎨 figma — Figma"),
    ("web", "🌐 web — Web Link"),
    ("custom", "🔗 custom — Custom Provider"),
]

RESOURCE_CATEGORY_ICONS: dict[str, str] = {
    "ticket": "🎫",
    "pull_request": "🔀",
    "issue": "🐛",
    "conversation": "💬",
    "alert": "🚨",
    "doc": "📄",
    "custom": "🔗",
}


def prompt_inline_text(
    label: str,
    current_value: str = "",
    allow_clear: bool = True,
    hint: str = "",
) -> str | None:
    cur = str(current_value or "")
    clear_hint = "  |  Type '-' to clear" if allow_clear and cur else ""
    extra_hint = f"  |  {hint}" if hint else ""
    print(f"\n--- ✏️  Edit {label} ---")
    print(f"Press [Enter] to confirm{clear_hint}{extra_hint}  |  [Ctrl-C] Cancel")

    if sys.stdin.isatty():
        try:
            import readline
            readline.set_startup_hook(lambda: readline.insert_text(cur))
        except Exception:
            readline = None  # type: ignore[assignment]
        try:
            raw = input(f"{label}: ")
        except (EOFError, KeyboardInterrupt):
            print()
            return None
        finally:
            if "readline" in locals() and readline is not None:
                try:
                    readline.set_startup_hook(None)
                except Exception:
                    pass
        val = raw.strip()
        if val == "-" and allow_clear:
            return ""
        return val

    print(f"{label} [{cur or 'empty'}]: ", end="", flush=True)
    line = sys.stdin.readline()
    if not line:
        return None
    val = line.strip()
    if val == "-" and allow_clear:
        return ""
    if val == "":
        return cur
    return val


def prompt_fzf_dropdown(
    fzf_bin: str,
    options: list[dict[str, Any]],
    prompt: str,
    header: str,
    current_value: str = "",
    allow_custom: bool = False,
    allow_clear: bool = False,
) -> str | None:
    cur_norm = (current_value or "").strip().lower()
    while True:
        rows: list[str] = []
        by_key: dict[str, dict[str, Any]] = {}
        for idx, opt in enumerate(options):
            val = str(opt.get("value") or "").strip()
            lbl = str(opt.get("label") or val).strip()
            key = f"opt_{idx}"
            is_cur = bool(cur_norm and val.lower() == cur_norm)
            suffix = "  ✨ (current)" if is_cur else ""
            rows.append(f"{key}\t{lbl}{suffix}")
            by_key[key] = opt

        if allow_custom:
            rows.append("__custom__\t✏️  Type a custom value manually...")
        if allow_clear and current_value:
            rows.append("__clear__\t🗑️  Clear field (set to empty)")

        cmd = [
            fzf_bin,
            "--ansi",
            "--delimiter=\t",
            "--with-nth=2",
            f"--prompt={prompt}",
            f"--header={header}",
        ]
        if allow_custom:
            cmd.append("--print-query")

        res = subprocess.run(
            cmd,
            input="\n".join(rows),
            capture_output=True,
            text=True,
            check=False,
        )
        out_lines = [ln for ln in res.stdout.splitlines() if ln.strip() or allow_custom]
        if not allow_custom:
            if res.returncode != 0 or not res.stdout.strip():
                return None
            chosen_key = res.stdout.strip().splitlines()[0].split("\t", 1)[0].strip()
            query_str = ""
        else:
            if res.returncode not in (0, 1) or not out_lines:
                return None
            query_str = out_lines[0].strip() if len(out_lines) >= 1 else ""
            chosen_line = out_lines[1].strip() if len(out_lines) >= 2 else ""
            chosen_key = chosen_line.split("\t", 1)[0].strip() if chosen_line else ""
            if not chosen_key:
                return query_str if query_str else None

        if chosen_key == "__clear__":
            return ""
        if chosen_key == "__custom__":
            typed = prompt_inline_text(prompt.rstrip(" >"), current_value=current_value, allow_clear=allow_clear)
            return typed

        chosen_opt = by_key.get(chosen_key)
        if not chosen_opt:
            return query_str if (allow_custom and query_str) else None

        if chosen_opt.get("action") == "open_url" and chosen_opt.get("url"):
            open_url_target(str(chosen_opt["url"]))
            continue

        return str(chosen_opt.get("value") or "")


def _resolve_default_jira_browse_url(cfg: dict[str, Any]) -> str:
    raw = str(cfg.get("jira_base_url") or "").strip().rstrip("/")
    if not raw or "your-org.atlassian.net" in raw:
        integ_jira = (cfg.get("integrations") or {}).get("jira") or (cfg.get("automations") or {}).get("jira_status_sync")
        if isinstance(integ_jira, dict) and integ_jira.get("base_url"):
            raw = str(integ_jira["base_url"]).strip().rstrip("/")
    if raw.endswith("/browse"):
        raw = raw[:-len("/browse")].rstrip("/")
    if not raw or "your-org.atlassian.net" in raw:
        return ""
    return f"{raw}/jira/your-work"


def prompt_integration_candidate(
    fzf_bin: str,
    kind: str,
    paths: TaskPaths,
    cfg: dict[str, Any],
    prompt: str,
    header: str,
    current_value: str = "",
    task: dict[str, Any] | None = None,
    repos: list[str] | None = None,
    allow_clear: bool = True,
) -> tuple[str | None, dict[str, Any] | None]:
    self_bin = resolve_bin("herdr-task")

    with tempfile.TemporaryDirectory(prefix="herdr_cand_") as tmp_dir:
        preview_file = Path(tmp_dir) / "candidates.json"

        while True:
            cands = query_integration_candidates(
                kind,
                paths,
                cfg,
                task=task,
                repos=repos,
                current_ref=current_value,
            )

            selectable_cands = [c for c in cands if c.get("action") != "open_url"]
            browser_cands = [c for c in cands if c.get("action") == "open_url"]

            rows: list[str] = []
            by_key: dict[str, dict[str, Any]] = {}
            previews_by_key: dict[str, str] = {}

            cur_norm = (current_value or "").strip().lower()
            idx = 0
            for c in selectable_cands:
                key = f"cand_{idx}"
                idx += 1
                val = str(c.get("value") or "").strip()
                lbl = str(c.get("label") or val).strip()
                is_cur = bool(cur_norm and val.lower() == cur_norm)
                suffix = "  ✨ (current)" if is_cur else ""
                rows.append(f"{key}\t{lbl}{suffix}")
                by_key[key] = c
                previews_by_key[key] = str(c.get("preview") or f"{lbl}\n\nValue: {val}")

            for c in browser_cands:
                key = f"open_{idx}"
                idx += 1
                lbl = str(c.get("label") or c.get("url") or "Open in Browser").strip()
                rows.append(f"{key}\t{lbl}")
                by_key[key] = c
                previews_by_key[key] = str(c.get("preview") or f"🌐 Open in Browser:\n{c.get('url')}")

            rows.append("__manual__\t✏️  Enter Key, Shorthand (PROJ-123 / repo#42), or URL manually...")
            previews_by_key["__manual__"] = (
                "✏️  Manual Input\n\n"
                "Select this option (or type directly into the fzf search bar and press [Enter]) "
                "to enter any Jira ticket key (PROJ-123), GitHub PR shorthand (org/repo#42), "
                "Slack thread URL, Alert URL, or Doc URL."
            )

            if allow_clear and current_value:
                rows.append("__clear__\t🗑️  Clear value (unlink / set to empty)")
                previews_by_key["__clear__"] = f"🗑️  Clear current value ({current_value})"

            preview_file.write_text(json.dumps(previews_by_key, ensure_ascii=False), encoding="utf-8")
            q_pfile = shlex.quote(str(preview_file))

            cmd = [
                fzf_bin,
                "--ansi",
                "--delimiter=\t",
                "--with-nth=2",
                "--print-query",
                "--expect=ctrl-o",
                f"--prompt={prompt}",
                f"--header={header}",
                "--preview",
                f"{self_bin} _popup-action preview-candidate {q_pfile} {{1}}",
                "--preview-window",
                "down:55%:wrap",
            ]

            res = subprocess.run(
                cmd,
                input="\n".join(rows),
                capture_output=True,
                text=True,
                check=False,
            )
            raw_lines = res.stdout.splitlines()
            if res.returncode not in (0, 1) or not raw_lines:
                return None, None

            query_str = raw_lines[0].strip() if len(raw_lines) >= 1 else ""
            expect_key = raw_lines[1].strip() if len(raw_lines) >= 2 else ""
            selected_line = raw_lines[2].strip() if len(raw_lines) >= 3 else ""
            chosen_key = selected_line.split("\t", 1)[0].strip() if selected_line else ""

            if expect_key == "ctrl-o":
                chosen_item = by_key.get(chosen_key)
                target_open_url = ""
                if chosen_item:
                    target_open_url = str(
                        chosen_item.get("url")
                        or (chosen_item.get("resource_patch") or {}).get("url")
                        or ""
                    )
                if not target_open_url and browser_cands:
                    target_open_url = str(browser_cands[0].get("url") or "")
                if not target_open_url:
                    target_open_url = _resolve_default_jira_browse_url(cfg)
                if target_open_url:
                    open_url_target(target_open_url)
                continue

            if not chosen_key:
                if query_str:
                    return query_str, None
                return None, None

            if chosen_key == "__clear__":
                return "", None

            if chosen_key == "__manual__":
                default_init = query_str if query_str else current_value
                typed = prompt_inline_text(
                    prompt.rstrip(" >"),
                    current_value=default_init,
                    allow_clear=allow_clear,
                    hint="e.g. PROJ-123, org/repo#42, or https://...",
                )
                return typed, None

            chosen_item = by_key.get(chosen_key)
            if not chosen_item:
                return (query_str, None) if query_str else (None, None)

            if chosen_item.get("action") == "open_url" and chosen_item.get("url"):
                open_url_target(str(chosen_item["url"]))
                continue

            val = str(chosen_item.get("value") or "").strip()
            patch = chosen_item.get("resource_patch") if isinstance(chosen_item.get("resource_patch"), dict) else None
            return val, patch


def apply_resource_target_to_draft(
    draft: dict[str, Any],
    raw_target: str,
    cfg: dict[str, Any],
    paths: TaskPaths,
    task: dict[str, Any] | None = None,
    candidate_patch: dict[str, Any] | None = None,
) -> list[str]:
    clean = raw_target.strip()
    if not clean:
        draft["ref"] = ""
        return []

    old_ref = draft.get("ref") or ""
    target_changed = clean != old_ref

    classified = classify_resource(clean, cfg, fetch_remote=True)
    msgs = enrich_draft_resource_via_integrations(classified, paths, cfg, task=task)

    if isinstance(candidate_patch, dict):
        for k in ("ref", "url", "category", "provider", "label", "status", "summary"):
            if candidate_patch.get(k):
                classified[k] = candidate_patch[k]
        if isinstance(candidate_patch.get("metadata"), dict):
            classified.setdefault("metadata", {}).update(candidate_patch["metadata"])

    draft["ref"] = classified.get("ref") or clean
    draft["url"] = classified.get("url") or clean
    draft["category"] = classified.get("category") or draft.get("category") or "custom"
    draft["provider"] = classified.get("provider") or draft.get("provider") or "custom"
    draft.setdefault("metadata", {}).update(classified.get("metadata") or {})

    if target_changed or not draft.get("label") or draft.get("label") == old_ref:
        draft["label"] = classified.get("label") or draft["ref"]
    if (target_changed or not draft.get("status")) and classified.get("status"):
        draft["status"] = classified["status"]
    if (target_changed or not draft.get("summary")) and classified.get("summary"):
        draft["summary"] = classified["summary"]

    draft["enrichment_messages"] = msgs
    return msgs


def format_form_preview(state: dict[str, Any], selected_key: str, color: bool = True) -> str:
    c_bold = "\033[1m" if color else ""
    c_cyan = "\033[36m" if color else ""
    c_green = "\033[32m" if color else ""
    c_yellow = "\033[33m" if color else ""
    c_magenta = "\033[35m" if color else ""
    c_dim = "\033[2m" if color else ""
    c_reset = "\033[0m" if color else ""

    form_type = state.get("form_type") or "task"
    mode = state.get("mode") or "edit"
    draft = state.get("draft") or {}
    orig = state.get("original") or {}

    field_hints: dict[str, str] = {
        "target": "🎯 Press [Enter] to pick from My Jira Tickets, Open GitHub PRs, open Jira/GitHub in Browser, or type a key/URL.",
        "label": "🏷️  Press [Enter] to edit the display label in-place (pre-filled with current value).",
        "category": "📂 Press [Enter] to open the Category dropdown (ticket, pull_request, issue, conversation, alert, doc, custom).",
        "provider": "🔌 Press [Enter] to open the Provider dropdown (jira, github, slack, grafana, notion, ...) or type custom.",
        "status": "🚦 Press [Enter] to open the Status dropdown (populated from modular integrations e.g. Jira transitions / GitHub states).",
        "summary": "📝 Press [Enter] to edit the technical summary in-place (avoids redundant AI/MCP lookups).",
        "url": "🔗 Press [Enter] to edit the target URL in-place (or press [Ctrl-O] to open it in browser).",
        "sync_branch": "🌿 Press [Enter] to toggle switching the attached repo worktree to match this PR's branch on save.",
        "open_browser": "🌐 Press [Enter] or [Ctrl-O] to open the resource or Jira/GitHub dashboard in your browser.",
        "title": "📌 Press [Enter] to edit the task title in-place (also updates the Herdr workspace label).",
        "jira_ticket": "🎫 Press [Enter] to list your assigned Jira tickets, redirect to Jira in Browser, or enter a ticket key (auto-fills Title & ID!).",
        "pr_links": "🔀 Press [Enter] to pick from open GitHub PRs (`gh pr list`), open GitHub in Browser, or add a PR/resource link.",
        "repos": "📦 Press [Enter] to open the multi-select Repository picker (`[Tab]` to toggle multiple repos from repo_roots).",
        "default_branch": "🌿 Press [Enter] to pick a Git branch from local/remote refs or type a custom branch name.",
        "brief": "📝 Press [Enter] to edit the shared multi-repo technical brief in-place.",
        "id": "🆔 Press [Enter] to customize the task ID slug (auto-generated from Jira Ticket + Title by default).",
        "open_jira": "🌐 Press [Enter] or [Ctrl-O] to open Jira in your browser to check or search your tickets.",
        "_save_": "✅ Press [Enter] (or [Ctrl-S] from any row) to validate and save all changes.",
        "_cancel_": "❌ Press [Enter] (or [Esc]) to discard changes and return.",
    }
    if selected_key.startswith("repo_branch:"):
        r_name = selected_key.split(":", 1)[1]
        hint_text = f"🌿 Press [Enter] to open the Git branch dropdown (local & remote branches) for repository '{r_name}'."
    else:
        hint_text = field_hints.get(selected_key, "Select any field and press [Enter] to edit it, or [Ctrl-S] to save.")

    def _diff_badge(key: str) -> str:
        if mode != "edit":
            return ""
        if draft.get(key) != orig.get(key):
            return f"  {c_yellow}✨ (modified){c_reset}"
        return ""

    lines: list[str] = [
        f"{c_bold}{c_magenta}💡 Selected Field:{c_reset} {hint_text}",
        f"{c_dim}────────────────────────────────────────────────────────────────────────────{c_reset}",
    ]

    if form_type == "resource":
        cat = draft.get("category") or "custom"
        cat_icon = RESOURCE_CATEGORY_ICONS.get(cat, "🔗")
        lines.extend([
            f"{c_bold}{c_cyan}Resource Card Preview:{c_reset}",
            f"  {c_bold}Target / Ref:{c_reset}    {draft.get('ref') or '(not set)'}{_diff_badge('ref')}",
            f"  {c_bold}Display Label:{c_reset}   {draft.get('label') or '(not set)'}{_diff_badge('label')}",
            f"  {c_bold}Category:{c_reset}        {cat_icon} {cat}{_diff_badge('category')}",
            f"  {c_bold}Provider:{c_reset}        {draft.get('provider') or 'custom'}{_diff_badge('provider')}",
            f"  {c_bold}Status Badge:{c_reset}    {c_yellow}{draft.get('status') or '(none)'}{c_reset}{_diff_badge('status')}",
            f"  {c_bold}URL:{c_reset}             {c_green}{draft.get('url') or '(none)'}{c_reset}{_diff_badge('url')}",
            f"  {c_bold}Summary:{c_reset}         {draft.get('summary') or '(none)'}{_diff_badge('summary')}",
        ])
        meta = draft.get("metadata") or {}
        if meta.get("branch"):
            lines.append(f"  {c_bold}PR Branch:{c_reset}       🌿 {meta['branch']}  (repo: {meta.get('repo', '-')})")
        if draft.get("enrichment_messages"):
            lines.append(f"{c_dim}Enrichment:{c_reset}")
            for m in draft["enrichment_messages"]:
                lines.append(f"  ↳ {m}")
    else:
        st = (draft.get("status") or "in_progress").lower()
        st_icon = TASK_STATUS_ICONS.get(st, "🚧")
        lines.extend([
            f"{c_bold}{c_cyan}Task Preview Card:{c_reset}",
            f"  {c_bold}Task ID:{c_reset}          {draft.get('id') or '(auto)'}{_diff_badge('id')}",
            f"  {c_bold}Title:{c_reset}            {draft.get('title') or '(not set)'}{_diff_badge('title')}",
            f"  {c_bold}Status:{c_reset}           {st_icon} {st.upper()}{_diff_badge('status')}",
            f"  {c_bold}Jira Ticket:{c_reset}      {draft.get('jira_ticket_display') or draft.get('jira_ticket') or '(none)'}{_diff_badge('jira_ticket')}",
            f"  {c_bold}PR Links:{c_reset}         {', '.join(draft.get('pr_links') or []) or '(none)'}{_diff_badge('pr_links')}",
            f"  {c_bold}Repositories:{c_reset}     {', '.join(draft.get('repos') or []) or '(none)'}{_diff_badge('repos')}",
            f"  {c_bold}Default Branch:{c_reset}   {draft.get('default_branch') or '(auto)'}{_diff_badge('default_branch')}",
        ])
        repo_branches = draft.get("repo_branches") or {}
        for r_name in draft.get("repos") or []:
            rb = repo_branches.get(r_name) or draft.get("default_branch") or "(auto)"
            lines.append(f"    ↳ {r_name:18} : 🌿 {rb}")
        if draft.get("brief"):
            lines.append(f"  {c_bold}Brief:{c_reset}            {draft['brief']}{_diff_badge('brief')}")
        if draft.get("enrichment_messages"):
            lines.append(f"{c_dim}Auto-Enrichment:{c_reset}")
            for m in draft["enrichment_messages"]:
                lines.append(f"  ↳ {m}")

    lines.extend([
        "",
        f"{c_dim}Shortcuts: [Enter] Edit Selected Field  |  [Ctrl-S] Save & Apply  |  [Ctrl-O] Open in Browser  |  [Esc] Cancel{c_reset}",
    ])
    return "\n".join(lines)


def _format_ResourceForm_rows(draft: dict[str, Any], mode: str) -> list[str]:
    ref = draft.get("ref") or ""
    lbl = draft.get("label") or ""
    cat = draft.get("category") or "custom"
    cat_icon = RESOURCE_CATEGORY_ICONS.get(cat, "🔗")
    prov = draft.get("provider") or "custom"
    st = draft.get("status") or ""
    summary = (draft.get("summary") or "").replace("\t", " ").replace("\n", " ")
    url = draft.get("url") or ""
    meta = draft.get("metadata") or {}

    rows = [
        f"target\t🎯 Target / Reference   : {ref or '(click to pick Jira Ticket, GitHub PR, or enter URL...)'}",
        f"label\t🏷️  Display Label        : {lbl or '(auto)'}",
        f"category\t📂 Category             : {cat_icon} {cat}  [dropdown]",
        f"provider\t🔌 Provider             : {prov}  [dropdown]",
        f"status\t🚦 Status Badge         : {st or '(none)'}  [dropdown]",
        f"summary\t📝 Summary              : {(summary[:64] + '…') if len(summary) > 64 else (summary or '(none)')}",
        f"url\t🔗 URL                  : {url or '(none)'}",
    ]
    if cat == "pull_request" or meta.get("branch"):
        sync_str = "Yes" if draft.get("sync_branch") else "No"
        br_hint = f" ({meta.get('repo', 'repo')} -> {meta['branch']})" if meta.get("branch") else ""
        rows.append(f"sync_branch\t🌿 Sync Worktree Branch : {sync_str}{br_hint}  [toggle]")

    open_lbl = url if url.startswith(("http://", "https://")) else "Open Jira / GitHub Dashboard in Browser"
    rows.append(f"open_browser\t🌐 Open in Browser      : {open_lbl}")
    save_txt = "✅ Save New Resource" if mode == "add" else "✅ Save Resource Changes"
    rows.append(f"_save_\t{save_txt}  (or press [Ctrl-S])")
    rows.append("_cancel_\t❌ Cancel  (or press [Esc])")
    return rows


def interactive_resource_form(
    tid: str,
    paths: TaskPaths,
    cfg: dict[str, Any],
    resource_id: str | None = None,
    initial_target: str | None = None,
    no_herdr: bool = False,
) -> int:
    task = load_task(paths, tid)
    if not task:
        print(f"Error: task '{tid}' not found.", file=sys.stderr)
        return 1

    target_res = None
    if resource_id and resource_id not in ("_add_", "_pilot_"):
        target_res = next(
            (r for r in task.get("resources", []) if r.get("id") == resource_id or r.get("ref") == resource_id),
            None,
        )

    mode = "edit" if target_res else "add"
    if target_res:
        draft: dict[str, Any] = {
            "id": target_res.get("id"),
            "ref": target_res.get("ref") or "",
            "label": target_res.get("label") or "",
            "category": target_res.get("category") or "custom",
            "provider": target_res.get("provider") or "custom",
            "status": target_res.get("status") or "",
            "summary": target_res.get("summary") or "",
            "url": target_res.get("url") or "",
            "metadata": json.loads(json.dumps(target_res.get("metadata") or {})),
            "sync_branch": False,
            "enrichment_messages": [],
        }
    else:
        draft = {
            "id": None,
            "ref": "",
            "label": "",
            "category": "ticket",
            "provider": "jira",
            "status": "",
            "summary": "",
            "url": "",
            "metadata": {},
            "sync_branch": False,
            "enrichment_messages": [],
        }
        if initial_target:
            apply_resource_target_to_draft(draft, initial_target, cfg, paths, task=task)

    original = json.loads(json.dumps(draft))
    fzf_bin = resolve_bin("fzf", cfg, "fzf_bin")
    self_bin = resolve_bin("herdr-task")

    with tempfile.TemporaryDirectory(prefix="herdr_res_form_") as tmp_dir:
        state_file = Path(tmp_dir) / "form_state.json"

        while True:
            task = load_task(paths, tid) or task
            state_payload = {
                "form_type": "resource",
                "mode": mode,
                "task_id": tid,
                "draft": draft,
                "original": original,
            }
            state_file.write_text(json.dumps(state_payload, ensure_ascii=False), encoding="utf-8")
            q_sfile = shlex.quote(str(state_file))

            rows = _format_ResourceForm_rows(draft, mode)
            title_hdr = (
                f"Add Resource to '{tid}'  |  [Enter] Edit Field  [Ctrl-S] Save  [Ctrl-O] Browser  [Esc] Cancel"
                if mode == "add"
                else f"Edit Resource [{draft.get('ref')}]  |  [Enter] Edit Field  [Ctrl-S] Save  [Ctrl-O] Browser  [Esc] Cancel"
            )

            cmd = [
                fzf_bin,
                "--ansi",
                "--delimiter=\t",
                "--with-nth=2",
                "--expect=ctrl-s,ctrl-o",
                "--header",
                title_hdr,
                "--prompt",
                f"{'Add' if mode == 'add' else 'Edit'} Resource ({tid}) > ",
                "--preview",
                f"{self_bin} _popup-action preview-form {q_sfile} {{1}}",
                "--preview-window",
                "down:58%:wrap",
            ]

            res = subprocess.run(cmd, input="\n".join(rows), capture_output=True, text=True, check=False)
            if res.returncode != 0 or not res.stdout.strip():
                return 0

            out_lines = res.stdout.splitlines()
            expect_key = out_lines[0].strip() if len(out_lines) >= 1 else ""
            sel_line = out_lines[1].strip() if len(out_lines) >= 2 else ""
            field_key = sel_line.split("\t", 1)[0].strip() if sel_line else ""

            if expect_key == "ctrl-o" or field_key == "open_browser":
                open_url = draft.get("url") or ""
                if not open_url.startswith(("http://", "https://", "slack://")):
                    open_url = _resolve_default_jira_browse_url(cfg) or "https://github.com/pulls"
                open_url_target(open_url)
                continue

            if expect_key == "ctrl-s" or field_key == "_save_":
                if not (draft.get("ref") or draft.get("url")):
                    chosen_val, cand_patch = prompt_integration_candidate(
                        fzf_bin,
                        "resource",
                        paths,
                        cfg,
                        prompt="Select Ticket, PR, or Enter URL > ",
                        header="[Enter] Select or Type Key/URL  |  [Ctrl-O] Open in Browser  |  [Esc] Cancel",
                        current_value="",
                        task=task,
                        allow_clear=False,
                    )
                    if not chosen_val:
                        continue
                    apply_resource_target_to_draft(draft, chosen_val, cfg, paths, task=task, candidate_patch=cand_patch)

                if mode == "edit" and target_res:
                    with task_store_lock(paths):
                        task_fresh = load_task(paths, tid)
                        if not task_fresh:
                            return 1
                        for r in task_fresh.get("resources", []):
                            if r.get("id") == target_res.get("id"):
                                r["ref"] = draft["ref"] or r.get("ref") or ""
                                r["label"] = draft["label"]
                                r["category"] = draft["category"] or "custom"
                                r["provider"] = draft["provider"] or "custom"
                                r["status"] = draft["status"] or None
                                r["summary"] = draft["summary"] or None
                                r["url"] = draft["url"] or ""
                                if draft.get("metadata"):
                                    r.setdefault("metadata", {}).update(draft["metadata"])
                                r["updated_at"] = now_iso()
                                break
                        task_fresh["updated_at"] = now_iso()
                        atomic_write_json(paths.task_file(tid), task_fresh)
                    if not no_herdr:
                        HerdrClient(cfg).sync_sidebar_metadata(task_fresh)
                    return 0

                target_arg = draft.get("url") if (draft.get("url") or "").startswith(("http://", "https://", "slack://")) and draft.get("category") != "ticket" else (draft.get("ref") or draft.get("url") or "")
                return cmd_link(
                    argparse.Namespace(
                        target=target_arg,
                        task=tid,
                        label=draft.get("label") or None,
                        status=draft.get("status") or None,
                        summary=draft.get("summary") or None,
                        category=draft.get("category") or None,
                        provider=draft.get("provider") or None,
                        added_by=None,
                        metadata=None,
                        sync_branch=bool(draft.get("sync_branch")),
                        no_herdr=no_herdr,
                        json=False,
                    ),
                    paths,
                    cfg,
                )

            if field_key == "_cancel_" or not field_key:
                return 0

            if field_key == "target":
                chosen_val, cand_patch = prompt_integration_candidate(
                    fzf_bin,
                    "resource",
                    paths,
                    cfg,
                    prompt="Target (Jira Ticket, GitHub PR, or URL) > ",
                    header="[Enter] Pick Ticket/PR or Type Key/URL  |  [Ctrl-O] Open in Browser  |  [Esc] Back",
                    current_value=draft.get("ref") or "",
                    task=task,
                    allow_clear=False,
                )
                if chosen_val is not None and chosen_val != "":
                    apply_resource_target_to_draft(draft, chosen_val, cfg, paths, task=task, candidate_patch=cand_patch)

            elif field_key == "label":
                new_lbl = prompt_inline_text("Display Label", current_value=draft.get("label") or "", allow_clear=True)
                if new_lbl is not None:
                    draft["label"] = new_lbl

            elif field_key == "category":
                cat_opts = [{"value": k, "label": lbl} for k, lbl in KNOWN_RESOURCE_CATEGORIES]
                chosen_cat = prompt_fzf_dropdown(
                    fzf_bin,
                    cat_opts,
                    prompt="Resource Category > ",
                    header="Select resource category ([Esc] Cancel):",
                    current_value=draft.get("category") or "custom",
                    allow_custom=False,
                )
                if chosen_cat:
                    draft["category"] = chosen_cat
                    if chosen_cat == "ticket" and draft.get("provider") == "custom":
                        draft["provider"] = "jira"
                    elif chosen_cat in ("pull_request", "issue") and draft.get("provider") == "custom":
                        draft["provider"] = "github"

            elif field_key == "provider":
                prov_opts = [{"value": k, "label": lbl} for k, lbl in KNOWN_RESOURCE_PROVIDERS]
                chosen_prov = prompt_fzf_dropdown(
                    fzf_bin,
                    prov_opts,
                    prompt="Resource Provider > ",
                    header="Select provider or type a custom provider name ([Enter] Confirm, [Esc] Cancel):",
                    current_value=draft.get("provider") or "custom",
                    allow_custom=True,
                )
                if chosen_prov is not None and chosen_prov != "":
                    draft["provider"] = chosen_prov

            elif field_key == "status":
                status_opts = query_integration_field_options(
                    "resource.status",
                    paths,
                    cfg,
                    task=task,
                    resource=draft,
                    category=draft.get("category"),
                    provider=draft.get("provider"),
                )
                chosen_st = prompt_fzf_dropdown(
                    fzf_bin,
                    status_opts,
                    prompt=f"Status Badge ({draft.get('provider') or draft.get('category')}) > ",
                    header="Select predefined status from integration or type custom status ([Enter] Confirm, [Esc] Cancel):",
                    current_value=draft.get("status") or "",
                    allow_custom=True,
                    allow_clear=True,
                )
                if chosen_st is not None:
                    draft["status"] = chosen_st

            elif field_key == "summary":
                new_sum = prompt_inline_text("Resource Summary", current_value=draft.get("summary") or "", allow_clear=True)
                if new_sum is not None:
                    draft["summary"] = new_sum

            elif field_key == "url":
                new_url = prompt_inline_text("Resource URL", current_value=draft.get("url") or "", allow_clear=True)
                if new_url is not None:
                    draft["url"] = new_url

            elif field_key == "sync_branch":
                draft["sync_branch"] = not bool(draft.get("sync_branch"))


def _recompute_task_draft_id_and_branch(draft: dict[str, Any], mode: str) -> None:
    if mode != "create":
        return
    jk = (draft.get("jira_ticket") or "").strip()
    title = (draft.get("title") or "").strip()
    if not draft.get("custom_id"):
        if jk and title and title != "Untitled Task":
            clean_title = title[len(jk):].strip(" -:") if title.upper().startswith(jk.upper()) else title
            draft["id"] = slugify(f"{jk}-{slugify(clean_title, 24)}", 48)
        elif jk:
            draft["id"] = slugify(jk, 48)
        elif title and title != "Untitled Task":
            draft["id"] = slugify(title, 36)
        else:
            draft["id"] = ""

    if not draft.get("custom_branch"):
        comp_id = draft.get("id")
        if not comp_id:
            rand_suffix = slugify(now_iso(), 6)
            comp_id = f"task-{rand_suffix}"
        draft["default_branch"] = f"feat/{comp_id}" if jk else comp_id


def _format_task_form_rows(draft: dict[str, Any], mode: str) -> list[str]:
    title = draft.get("title") or ""
    st = (draft.get("status") or "in_progress").lower()
    st_icon = TASK_STATUS_ICONS.get(st, "🚧")
    jk_disp = draft.get("jira_ticket_display") or draft.get("jira_ticket") or ""
    pr_links = draft.get("pr_links") or []
    repos = draft.get("repos") or []
    def_branch = draft.get("default_branch") or ""
    repo_branches = draft.get("repo_branches") or {}
    brief = (draft.get("brief") or "").replace("\t", " ").replace("\n", " ")

    rows = [
        f"title\t📌 Task Title           : {title or '(click to enter title — or pick Jira Ticket below to auto-fill)'}",
        f"status\t🚦 Task Status          : {st_icon} {st.upper()}  [dropdown]",
        f"jira_ticket\t🎫 Jira Ticket          : {jk_disp or '(pick from My Jira Tickets / Open Jira in Browser...)'}  [picker]",
        f"pr_links\t🔀 GitHub PR / Links    : {', '.join(pr_links) if pr_links else '(pick from Open GitHub PRs / add URL...)'}  [picker]",
        f"repos\t📦 Repositories         : {', '.join(repos) if repos else '(none — click to multi-select repos)'}  [multi-select]",
        f"default_branch\t🌿 Default Branch       : {def_branch or '(auto)'}  [dropdown]",
    ]
    for r_name in repos:
        rb = repo_branches.get(r_name) or def_branch or f"feat/{draft.get('id') or 'task'}"
        rows.append(f"repo_branch:{r_name}\t  ↳ 🌿 Branch [{r_name}] : {rb}  [branch dropdown]")

    rows.append(f"brief\t📝 Technical Brief      : {(brief[:64] + '…') if len(brief) > 64 else (brief or '(none)')}")
    if mode == "create":
        tid_val = draft.get("id") or "(auto-generated from Ticket + Title)"
        rows.append(f"id\t🆔 Task ID              : {tid_val}")

    rows.append("open_jira\t🌐 Open Jira in Browser : Check / search your assigned tickets in Jira")
    save_label = "✅ Create Task & Provision Workspace" if mode == "create" else "✅ Save Task Changes"
    rows.append(f"_save_\t{save_label}  (or press [Ctrl-S])")
    rows.append("_cancel_\t❌ Cancel  (or press [Esc])")
    return rows


def interactive_task_form(
    paths: TaskPaths,
    cfg: dict[str, Any],
    task_id: str | None = None,
    initial_args: argparse.Namespace | None = None,
    no_herdr: bool = False,
) -> int:
    existing_task = load_task(paths, task_id) if task_id else None
    if task_id and not existing_task:
        print(f"Error: task '{task_id}' not found.", file=sys.stderr)
        return 1

    mode = "edit" if existing_task else "create"
    if existing_task:
        existing_tickets = [
            r for r in existing_task.get("resources", []) if r.get("category") == "ticket"
        ]
        first_tk = existing_tickets[0] if existing_tickets else None
        existing_prs = [
            r.get("ref") or r.get("url") or ""
            for r in existing_task.get("resources", [])
            if r.get("category") != "ticket" and (r.get("ref") or r.get("url"))
        ]
        wts = existing_task.get("worktrees") or []
        repos_init = [w["repo"] for w in wts if w.get("repo")]
        rb_init = {w["repo"]: w.get("branch") or "" for w in wts if w.get("repo")}

        draft: dict[str, Any] = {
            "id": existing_task["id"],
            "title": existing_task.get("title") or "",
            "status": existing_task.get("status") or "in_progress",
            "jira_ticket": (first_tk.get("ref") if first_tk else "") or "",
            "jira_ticket_display": (first_tk.get("label") if first_tk else "") or "",
            "pr_links": existing_prs,
            "repos": repos_init,
            "default_branch": existing_task.get("default_branch") or "",
            "repo_branches": rb_init,
            "brief": existing_task.get("brief") or "",
            "custom_id": True,
            "custom_branch": True,
            "enrichment_messages": [],
        }
    else:
        init_repos: list[str] = []
        if initial_args and getattr(initial_args, "repos", None):
            init_repos.extend([r.strip() for r in initial_args.repos.split(",") if r.strip()])
        if initial_args and getattr(initial_args, "repo_list", None):
            for rl in initial_args.repo_list:
                init_repos.extend([r.strip() for r in rl.split(",") if r.strip() and r.strip() not in init_repos])

        init_links = list(getattr(initial_args, "link", None) or []) if initial_args else []
        init_id = getattr(initial_args, "id", None) if initial_args else None
        init_branch = getattr(initial_args, "branch", None) if initial_args else None

        draft = {
            "id": init_id or "",
            "title": (getattr(initial_args, "title", None) if initial_args else None) or "",
            "status": (getattr(initial_args, "task_status", None) if initial_args else None) or "in_progress",
            "jira_ticket": "",
            "jira_ticket_display": "",
            "pr_links": init_links,
            "repos": init_repos,
            "default_branch": init_branch or "",
            "repo_branches": {},
            "brief": (getattr(initial_args, "brief", None) if initial_args else None) or "",
            "custom_id": bool(init_id),
            "custom_branch": bool(init_branch),
            "enrichment_messages": [],
        }
        _recompute_task_draft_id_and_branch(draft, mode)

    original = json.loads(json.dumps(draft))
    fzf_bin = resolve_bin("fzf", cfg, "fzf_bin")
    self_bin = resolve_bin("herdr-task")

    with tempfile.TemporaryDirectory(prefix="herdr_task_form_") as tmp_dir:
        state_file = Path(tmp_dir) / "form_state.json"

        while True:
            state_payload = {
                "form_type": "task",
                "mode": mode,
                "task_id": task_id or draft.get("id") or "new",
                "draft": draft,
                "original": original,
            }
            state_file.write_text(json.dumps(state_payload, ensure_ascii=False), encoding="utf-8")
            q_sfile = shlex.quote(str(state_file))

            rows = _format_task_form_rows(draft, mode)
            hdr = (
                "⚡ Create New Task  |  [Enter] Edit Field  [Ctrl-S] Create Task  [Ctrl-O] Open Jira  [Esc] Cancel"
                if mode == "create"
                else f"✏️  Edit Task '{task_id}'  |  [Enter] Edit Field  [Ctrl-S] Save Changes  [Ctrl-O] Open Jira  [Esc] Cancel"
            )

            cmd = [
                fzf_bin,
                "--ansi",
                "--delimiter=\t",
                "--with-nth=2",
                "--expect=ctrl-s,ctrl-o",
                "--header",
                hdr,
                "--prompt",
                f"{'Create Task' if mode == 'create' else f'Edit Task ({task_id})'} > ",
                "--preview",
                f"{self_bin} _popup-action preview-form {q_sfile} {{1}}",
                "--preview-window",
                "down:58%:wrap",
            ]

            res = subprocess.run(cmd, input="\n".join(rows), capture_output=True, text=True, check=False)
            if res.returncode != 0 or not res.stdout.strip():
                return 0

            out_lines = res.stdout.splitlines()
            expect_key = out_lines[0].strip() if len(out_lines) >= 1 else ""
            sel_line = out_lines[1].strip() if len(out_lines) >= 2 else ""
            field_key = sel_line.split("\t", 1)[0].strip() if sel_line else ""

            if expect_key == "ctrl-o" or field_key == "open_jira":
                jk = (draft.get("jira_ticket") or "").strip()
                base_browse = str(cfg.get("jira_base_url") or "").strip().rstrip("/")
                if jk and base_browse and "your-org.atlassian.net" not in base_browse:
                    if not base_browse.endswith("/browse"):
                        base_browse = f"{base_browse}/browse"
                    open_url_target(f"{base_browse}/{jk}")
                else:
                    default_jira = _resolve_default_jira_browse_url(cfg)
                    if default_jira:
                        open_url_target(default_jira)
                continue

            if expect_key == "ctrl-s" or field_key == "_save_":
                try:
                    rc = _submit_interactive_task_form(draft, original, mode, task_id, paths, cfg, no_herdr=no_herdr)
                    if rc == 0:
                        return 0
                except Exception as e:
                    prompt_inline_text(
                        f"⚠️ Error saving task: {e}\nPress Enter to return to form",
                        current_value="",
                        allow_clear=True,
                    )
                continue

            if field_key == "_cancel_" or not field_key:
                return 0

            if field_key == "title":
                new_t = prompt_inline_text("Task Title", current_value=draft.get("title") or "", allow_clear=False)
                if new_t is not None:
                    draft["title"] = new_t
                    _recompute_task_draft_id_and_branch(draft, mode)

            elif field_key == "status":
                allowed_statuses = VALID_TASK_STATUSES if mode == "edit" else VALID_TASK_STATUSES[:4]
                st_opts = [
                    {
                        "value": st,
                        "label": f"{TASK_STATUS_ICONS.get(st, '•')} {st.upper()}",
                    }
                    for st in allowed_statuses
                ]
                chosen_st = prompt_fzf_dropdown(
                    fzf_bin,
                    st_opts,
                    prompt="Task Status > ",
                    header="Select task lifecycle status (triggers modular automations e.g. Jira transition on save):",
                    current_value=draft.get("status") or "in_progress",
                    allow_custom=False,
                )
                if chosen_st:
                    draft["status"] = chosen_st

            elif field_key == "jira_ticket":
                chosen_jk, cand_patch = prompt_integration_candidate(
                    fzf_bin,
                    "ticket",
                    paths,
                    cfg,
                    prompt="Jira Ticket > ",
                    header="[Enter] Select Assigned Ticket, Open Jira in Browser, or Type Key (e.g. PROJ-123)  |  [Ctrl-O] Open Jira",
                    current_value=draft.get("jira_ticket") or "",
                    task=existing_task,
                    repos=draft.get("repos"),
                    allow_clear=True,
                )
                if chosen_jk is not None:
                    if chosen_jk == "":
                        draft["jira_ticket"] = ""
                        draft["jira_ticket_display"] = ""
                        _recompute_task_draft_id_and_branch(draft, mode)
                    else:
                        temp_res: dict[str, Any] = {"ref": chosen_jk}
                        enr_msgs = apply_resource_target_to_draft(
                            temp_res,
                            chosen_jk,
                            cfg,
                            paths,
                            task=existing_task,
                            candidate_patch=cand_patch,
                        )
                        draft["jira_ticket"] = temp_res.get("ref") or chosen_jk
                        draft["jira_ticket_display"] = temp_res.get("label") or draft["jira_ticket"]
                        draft.setdefault("enrichment_messages", []).extend(enr_msgs)

                        meta = temp_res.get("metadata") or {}
                        j_title = meta.get("jira_title") or ""
                        if not j_title and temp_res.get("label") and ":" in temp_res["label"]:
                            j_title = temp_res["label"].split(":", 1)[1].strip()
                        if j_title and (not draft.get("title") or draft.get("title") == "Untitled Task"):
                            draft["title"] = f"{draft['jira_ticket']} {j_title}"
                        if temp_res.get("summary") and not draft.get("brief"):
                            draft["brief"] = temp_res["summary"]
                        _recompute_task_draft_id_and_branch(draft, mode)

            elif field_key == "pr_links":
                chosen_pr, cand_patch = prompt_integration_candidate(
                    fzf_bin,
                    "pull_request",
                    paths,
                    cfg,
                    prompt="GitHub PR or Resource Link > ",
                    header="[Enter] Select Open PR (`gh pr list`), Open GitHub in Browser, or Type URL/repo#123  |  [Ctrl-O] Browser",
                    current_value="",
                    task=existing_task,
                    repos=draft.get("repos"),
                    allow_clear=True,
                )
                if chosen_pr is not None:
                    if chosen_pr == "":
                        draft["pr_links"] = []
                    else:
                        temp_res = {"ref": chosen_pr}
                        enr_msgs = apply_resource_target_to_draft(
                            temp_res,
                            chosen_pr,
                            cfg,
                            paths,
                            task=existing_task,
                            candidate_patch=cand_patch,
                        )
                        link_val = temp_res.get("url") or temp_res.get("ref") or chosen_pr
                        if link_val not in draft["pr_links"]:
                            draft["pr_links"].append(link_val)
                        draft.setdefault("enrichment_messages", []).extend(enr_msgs)

                        meta = temp_res.get("metadata") or {}
                        pr_repo = meta.get("repo")
                        pr_branch = meta.get("branch")
                        if pr_repo:
                            try:
                                r_short, _ = resolve_repo_path(pr_repo, cfg)
                                if r_short not in draft["repos"]:
                                    draft["repos"].append(r_short)
                                if pr_branch:
                                    draft["repo_branches"][r_short] = pr_branch
                                    if not draft.get("custom_branch"):
                                        draft["default_branch"] = pr_branch
                            except Exception:
                                pass
                        if meta.get("pr_title") and (not draft.get("title") or draft.get("title") == "Untitled Task"):
                            draft["title"] = meta["pr_title"]
                        if meta.get("jira_key") and not draft.get("jira_ticket"):
                            draft["jira_ticket"] = meta["jira_key"]
                            draft["jira_ticket_display"] = meta["jira_key"]
                        _recompute_task_draft_id_and_branch(draft, mode)

            elif field_key == "repos":
                avail_repos = discover_available_repos(cfg)
                if avail_repos and sys.stdin.isatty():
                    cur_repos = set(draft.get("repos") or [])
                    repo_lines = [
                        f"{r}\t📦 {r}" + ("  ✨ (currently selected)" if r in cur_repos else "")
                        for r in avail_repos
                    ]
                    fzf_repo = subprocess.run(
                        [
                            fzf_bin,
                            "-m",
                            "--delimiter=\t",
                            "--with-nth=2",
                            "--prompt=Select Repositories ([Tab] multi-select) > ",
                            "--header=[Tab] Toggle repo selection   [Enter] Confirm selection   [Esc] Cancel",
                        ],
                        input="\n".join(repo_lines),
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    if fzf_repo.returncode == 0 and fzf_repo.stdout.strip():
                        chosen_repos = [
                            ln.split("\t", 1)[0].strip()
                            for ln in fzf_repo.stdout.strip().splitlines()
                            if ln.strip()
                        ]
                        if mode == "edit":
                            merged = list(draft.get("repos") or [])
                            for cr in chosen_repos:
                                if cr not in merged:
                                    merged.append(cr)
                            draft["repos"] = merged
                        else:
                            draft["repos"] = chosen_repos
                        for r_name in draft["repos"]:
                            draft["repo_branches"].setdefault(
                                r_name,
                                draft.get("default_branch") or f"feat/{draft.get('id') or 'task'}",
                            )
                else:
                    raw_r = prompt_inline_text(
                        "Repositories (comma-separated)",
                        current_value=",".join(draft.get("repos") or []),
                        allow_clear=True,
                    )
                    if raw_r is not None:
                        draft["repos"] = [r.strip() for r in raw_r.split(",") if r.strip()]

            elif field_key == "default_branch":
                br_opts = query_integration_field_options(
                    "task.branch",
                    paths,
                    cfg,
                    task=existing_task,
                    repos=draft.get("repos"),
                    task_id=draft.get("id"),
                    current_branch=draft.get("default_branch"),
                )
                chosen_br = prompt_fzf_dropdown(
                    fzf_bin,
                    br_opts,
                    prompt="Default Git Branch > ",
                    header="Select a local/remote branch or type a new branch name ([Enter] Confirm, [Esc] Cancel):",
                    current_value=draft.get("default_branch") or "",
                    allow_custom=True,
                )
                if chosen_br:
                    old_def = draft.get("default_branch")
                    draft["default_branch"] = chosen_br
                    draft["custom_branch"] = True
                    for r_name in draft.get("repos") or []:
                        if draft["repo_branches"].get(r_name) in (None, "", old_def):
                            draft["repo_branches"][r_name] = chosen_br

            elif field_key.startswith("repo_branch:"):
                target_repo = field_key.split(":", 1)[1]
                cur_rb = (draft.get("repo_branches") or {}).get(target_repo) or draft.get("default_branch") or ""
                br_opts = query_integration_field_options(
                    "worktree.branch",
                    paths,
                    cfg,
                    task=existing_task,
                    repo=target_repo,
                    task_id=draft.get("id"),
                    current_branch=cur_rb,
                )
                chosen_rb = prompt_fzf_dropdown(
                    fzf_bin,
                    br_opts,
                    prompt=f"Git Branch [{target_repo}] > ",
                    header=f"Select local/remote branch for '{target_repo}' or type a new branch name:",
                    current_value=cur_rb,
                    allow_custom=True,
                )
                if chosen_rb:
                    draft.setdefault("repo_branches", {})[target_repo] = chosen_rb

            elif field_key == "brief":
                new_b = prompt_inline_text("Technical Brief", current_value=draft.get("brief") or "", allow_clear=True)
                if new_b is not None:
                    draft["brief"] = new_b

            elif field_key == "id" and mode == "create":
                new_id = prompt_inline_text("Task ID Slug", current_value=draft.get("id") or "", allow_clear=True)
                if new_id is not None:
                    if new_id == "":
                        draft["custom_id"] = False
                        _recompute_task_draft_id_and_branch(draft, mode)
                    else:
                        draft["id"] = slugify(new_id, 48)
                        draft["custom_id"] = True


def _submit_interactive_task_form(
    draft: dict[str, Any],
    original: dict[str, Any],
    mode: str,
    task_id: str | None,
    paths: TaskPaths,
    cfg: dict[str, Any],
    no_herdr: bool = False,
) -> int:
    if mode == "create":
        links: list[str] = []
        if draft.get("jira_ticket"):
            links.append(draft["jira_ticket"])
        for pl in draft.get("pr_links") or []:
            if pl and pl not in links:
                links.append(pl)

        title = (draft.get("title") or "").strip() or "Untitled Task"
        repos_list = list(draft.get("repos") or [])
        repo_branches = draft.get("repo_branches") or {}
        def_branch = (draft.get("default_branch") or "").strip() or None

        try:
            rc = cmd_create(
                argparse.Namespace(
                    id=draft.get("id") or None,
                    title=title,
                    brief=draft.get("brief") or None,
                    task_status=draft.get("status") or "in_progress",
                    repos=",".join(repos_list) if repos_list else None,
                    repo_list=None,
                    branch=def_branch,
                    link=links or None,
                    label=None,
                    status=None,
                    summary=None,
                    spec_stdin=False,
                    focus=True,
                    no_herdr=no_herdr,
                    json=False,
                    interactive=False,
                ),
                paths,
                cfg,
            )
        except Exception as e:
            prompt_inline_text(
                f"⚠️ Error creating task: {e}\nPress Enter to return to form",
                current_value="",
                allow_clear=True,
            )
            return 1

        if rc != 0:
            prompt_inline_text(
                "⚠️ Task creation failed (check error messages above).\nPress Enter to return to form",
                current_value="",
                allow_clear=True,
            )
            return rc

        created_id = resolve_current_task_id(paths, draft.get("id"))
        if created_id and repo_branches:
            rb_Pairs = [
                f"{r}:{b}"
                for r, b in repo_branches.items()
                if b and b != def_branch
            ]
            if rb_Pairs:
                cmd_update(
                    argparse.Namespace(
                        task_id=created_id,
                        task=created_id,
                        title=None,
                        brief=None,
                        status=None,
                        branch=None,
                        repo_branch=rb_Pairs,
                        sync_pr_branches=False,
                        force=False,
                        no_herdr=no_herdr,
                        json=False,
                        interactive=False,
                    ),
                    paths,
                    cfg,
                )
        return 0

    assert task_id is not None
    orig_repos = set(original.get("repos") or [])
    new_repos = [r for r in (draft.get("repos") or []) if r not in orig_repos]
    for nr in new_repos:
        target_b = (draft.get("repo_branches") or {}).get(nr) or draft.get("default_branch") or None
        cmd_worktree_add(
            argparse.Namespace(
                task=task_id,
                repo=nr,
                branch=target_b,
                label=None,
                focus=False,
                no_herdr=no_herdr,
                json=False,
            ),
            paths,
            cfg,
        )

    if draft.get("jira_ticket") and draft.get("jira_ticket") != original.get("jira_ticket"):
        cmd_link(
            argparse.Namespace(
                target=draft["jira_ticket"],
                task=task_id,
                label=None,
                status=None,
                summary=None,
                category="ticket",
                provider="jira",
                added_by=None,
                metadata=None,
                sync_branch=False,
                no_herdr=no_herdr,
                json=False,
                interactive=False,
            ),
            paths,
            cfg,
        )

    orig_prs = set(original.get("pr_links") or [])
    for pl in draft.get("pr_links") or []:
        if pl and pl not in orig_prs:
            cmd_link(
                argparse.Namespace(
                    target=pl,
                    task=task_id,
                    label=None,
                    status=None,
                    summary=None,
                    category=None,
                    provider=None,
                    added_by=None,
                    metadata=None,
                    sync_branch=False,
                    no_herdr=no_herdr,
                    json=False,
                    interactive=False,
                ),
                paths,
                cfg,
            )

    orig_rb = original.get("repo_branches") or {}
    cur_rb = draft.get("repo_branches") or {}
    rb_updates = [
        f"{r}:{b}"
        for r, b in cur_rb.items()
        if b and b != orig_rb.get(r)
    ]

    new_title = draft.get("title") if draft.get("title") != original.get("title") else None
    new_brief: str | None = None
    if draft.get("brief") != original.get("brief"):
        new_brief = "-" if not draft.get("brief") else draft["brief"]
    new_status = draft.get("status") if draft.get("status") != original.get("status") else None
    new_def_branch = draft.get("default_branch") if draft.get("default_branch") != original.get("default_branch") else None

    return cmd_update(
        argparse.Namespace(
            task_id=task_id,
            task=task_id,
            title=new_title,
            brief=new_brief,
            status=new_status,
            branch=new_def_branch,
            repo_branch=rb_updates,
            sync_pr_branches=False,
            force=False,
            no_herdr=no_herdr,
            json=False,
            interactive=False,
        ),
        paths,
        cfg,
    )
