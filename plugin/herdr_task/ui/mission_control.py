#!/usr/bin/env python3
"""
Mission Control (fzf popup), resource preview, and task pilot launcher.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
from typing import Any

from herdr_task.backend.models import (
    DEFAULT_CONFIG,
    TASK_STATUS_ICONS,
    TaskPaths,
    is_valid_task_id,
    resolve_bin,
)
from herdr_task.backend.storage import (
    list_all_tasks,
    load_task,
)
from herdr_task.backend.commands import (
    cmd_add,
    cmd_cleanup,
    cmd_sync,
    cmd_unlink,
    cmd_update,
    format_fzf_task_line,
    open_url_target,
)
from herdr_task.ui.forms import (
    format_form_preview,
    interactive_resource_form,
    interactive_task_form,
)


RESOURCE_CATEGORY_ICONS: dict[str, str] = {
    "ticket": "🎫",
    "pull_request": "🔀",
    "issue": "🐛",
    "conversation": "💬",
    "alert": "🚨",
    "doc": "📄",
    "custom": "🔗",
}


def format_fzf_resource_lines(task: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    for r in task.get("resources", []):
        cat = r.get("category") or "custom"
        icon = RESOURCE_CATEGORY_ICONS.get(cat, "🔗")
        ref = r.get("ref") or r.get("id") or "res"
        label = r.get("label") or ""
        status = f" [{r['status']}]" if r.get("status") else ""
        url = r.get("url") or ""
        summary_tok = (r.get("summary") or "").replace("\t", " ").replace("\n", " ")
        display = f"{icon} [{cat}] {ref}{status} — {label}"
        if url and url != ref:
            display += f"  ({url})"
        lines.append(f"{r['id']}\t{display}\t{summary_tok}")
    lines.append("_add_\t➕ Add New Resource (Jira, GitHub PR, Slack, Alert, Doc, URL)...\tadd create new resource")
    lines.append("_pilot_\t🤖 Launch Task Pilot Agent on This Task (Link PRs/Tickets, Add Repos, Update Context)...\tpilot agent ai")
    return lines


def format_resource_preview(task: dict[str, Any], res_id: str, color: bool = True) -> str:
    c_bold = "\033[1m" if color else ""
    c_cyan = "\033[36m" if color else ""
    c_green = "\033[32m" if color else ""
    c_yellow = "\033[33m" if color else ""
    c_dim = "\033[2m" if color else ""
    c_reset = "\033[0m" if color else ""

    if res_id == "_pilot_":
        out = [
            f"{c_bold}{c_cyan}🤖 Launch Task Pilot Agent on '{task['id']}'{c_reset}",
            "",
            f"Press {c_bold}[Enter]{c_reset} or {c_bold}[Ctrl-P]{c_reset} to launch an AI Pilot Agent scoped to this task:",
            f"  • {c_bold}Active Task{c_reset}: {task.get('title', '')} ({task['id']})",
            f"  • {c_bold}Env Bound{c_reset}:   HERDR_TASK_ID={task['id']}  HERDR_PILOT_MODE=existing_task",
            "",
            f"What the Task Pilot can do for '{task['id']}':",
            f"  • Link GitHub PRs, Jira tickets, Slack threads, alerts, or docs (`herdr-task link`)",
            f"  • Add repository worktrees and provision Cockpit tabs (`herdr-task add <repo>`)",
            f"  • Fetch Jira/GitHub context and save summaries (`herdr-task link ... --summary`)",
            f"  • Record cross-repo handoff notes (`herdr-task note \"...\"`)",
        ]
        if task.get("brief"):
            out.extend(["", f"{c_bold}Brief:{c_reset} {task['brief']}"])
        return "\n".join(out)

    if res_id == "_add_" or not res_id:
        out = [
            f"{c_bold}{c_cyan}➕ Add a New Resource to '{task['id']}'{c_reset}",
            "",
            f"Press {c_bold}[Enter]{c_reset} or {c_bold}[Ctrl-N]{c_reset} to link a new resource:",
            f"  • {c_bold}Jira Ticket{c_reset}:       e.g. PROJ-123 or https://.../browse/PROJ-123",
            f"  • {c_bold}GitHub PR / Issue{c_reset}: e.g. org/repo#42 or https://github.com/org/repo/pull/42",
            f"  • {c_bold}Slack Thread{c_reset}:      https://...slack.com/archives/...",
            f"  • {c_bold}Alert / Doc{c_reset}:       Grafana, Datadog, Sentry, Notion, Figma, or any URL",
            "",
            f"{c_bold}Task Context:{c_reset} {task.get('title', '')}",
        ]
        if task.get("brief"):
            out.append(f"{c_bold}Brief:{c_reset} {task['brief']}")
        return "\n".join(out)

    target_res = next(
        (r for r in task.get("resources", []) if r.get("id") == res_id or r.get("ref") == res_id),
        None,
    )
    if not target_res:
        return f"Resource '{res_id}' not found in task '{task['id']}'."

    cat = target_res.get("category") or "custom"
    icon = RESOURCE_CATEGORY_ICONS.get(cat, "🔗")
    status_str = f" [{target_res['status']}]" if target_res.get("status") else ""
    out = [
        f"{c_bold}{c_cyan}{icon} [{cat.upper()}] {target_res.get('ref', '')}{c_yellow}{status_str}{c_reset} — {c_bold}{target_res.get('label', '')}{c_reset}",
        "",
        f"  {c_bold}ID:{c_reset}          {target_res.get('id', '')}",
        f"  {c_bold}Provider:{c_reset}    {target_res.get('provider', 'custom')}",
        f"  {c_bold}Category:{c_reset}    {cat}",
        f"  {c_bold}Status:{c_reset}      {c_yellow}{target_res.get('status') or '(none)'}{c_reset}",
        f"  {c_bold}URL:{c_reset}         {c_green}{target_res.get('url') or '(none)'}{c_reset}",
        f"  {c_bold}Added By:{c_reset}    {target_res.get('added_by', 'user')}  {c_dim}(updated: {target_res.get('updated_at', '-')}){c_reset}",
        "",
        f"{c_bold}Summary / Context:{c_reset}",
        f"  {target_res['summary']}" if target_res.get("summary") else f"  {c_dim}(No summary set — press [Ctrl-E] to add or update summary){c_reset}",
        "",
        f"{c_dim}────────────────────────────────────────────────────────────{c_reset}",
        f"{c_bold}Task:{c_reset} {task.get('title', '')} ({task['id']})",
    ]
    if task.get("brief"):
        out.append(f"{c_bold}Brief:{c_reset} {task['brief']}")
    notes = task.get("notes", [])
    if notes:
        out.append(f"{c_bold}Recent Notes ({len(notes)}):{c_reset}")
        for n in notes[-3:]:
            out.append(f"  > [{n.get('author', 'note')}] {n.get('text', '')}")
    return "\n".join(out)


def _select_pilot_choice_and_cmd(
    cfg: dict[str, Any],
    fzf_bin: str,
    prompt: str,
    header: str,
    include_quick: bool = False,
) -> tuple[str | None, list[str]]:
    models = cfg.get("pilot_models") or DEFAULT_CONFIG["pilot_models"]
    menu_items: list[str] = []
    if include_quick:
        menu_items.append("quick\t⚡ Interactive Task Form (0 Tokens / 0 AIC) — Field Navigator, Jira/PR Picker & Dropdowns")
    model_by_id: dict[str, dict[str, Any]] = {}
    for m in models:
        if isinstance(m, dict) and m.get("id") and m.get("label"):
            menu_items.append(f"{m['id']}\t{m['label']}")
            model_by_id[m["id"]] = m

    res = subprocess.run(
        [
            fzf_bin,
            "--delimiter=\t",
            "--with-nth=2",
            f"--prompt={prompt}",
            f"--header={header}",
        ],
        input="\n".join(menu_items),
        capture_output=True,
        text=True,
        check=False,
    )
    if res.returncode != 0 or not res.stdout.strip():
        return None, []

    choice = res.stdout.strip().split("\t", 1)[0]
    if choice == "quick":
        return "quick", []

    spec = model_by_id.get(choice)
    if spec:
        bin_name = spec.get("bin") or "copilot"
        c_bin = resolve_bin(bin_name)
        cmd_list = [c_bin]
        if spec.get("model"):
            cmd_list.extend(["--model", str(spec["model"])])
        return choice, cmd_list

    cand = cfg.get("default_pilot_bin") or cfg.get("default_worker_kind") or "copilot"
    c_bin = resolve_bin(str(cand))
    return choice, [c_bin]


def manage_task_resources(tid: str, paths: TaskPaths, cfg: dict[str, Any]) -> int:
    if not is_valid_task_id(tid):
        return 1
    fzf_bin = resolve_bin("fzf", cfg, "fzf_bin")
    self_bin = resolve_bin("herdr-task")
    q_tid = shlex.quote(tid)

    while True:
        task = load_task(paths, tid)
        if not task:
            return 0

        lines = format_fzf_resource_lines(task)
        header = (
            f"Task: {task.get('title', tid)}  |  "
            "[Enter] Open URL  [Ctrl-S] Status  [Ctrl-R] Sync  [Ctrl-P] Pilot  [Ctrl-N] +Add  [Ctrl-E] Edit  [Ctrl-D] Del  [Esc] Back"
        )
        fzf_cmd = [
            fzf_bin,
            "--ansi",
            "--delimiter=\t",
            "--with-nth=2",
            "--header", header,
            "--prompt", f"Resources ({tid}) > ",
            "--preview", f"{self_bin} _popup-action preview-resource {q_tid} {{1}}",
            "--preview-window", "down:64%:wrap",
            "--bind", f"ctrl-s:execute({self_bin} _popup-action set-status {q_tid})+reload({self_bin} _popup-action list-resources {q_tid})",
            "--bind", f"ctrl-r:execute({self_bin} _popup-action sync-task {q_tid})+reload({self_bin} _popup-action list-resources {q_tid})",
            "--bind", f"ctrl-p:execute({self_bin} _popup-action task-pilot {q_tid})+reload({self_bin} _popup-action list-resources {q_tid})",
            "--bind", f"ctrl-n:execute({self_bin} _popup-action add-resource {q_tid})+reload({self_bin} _popup-action list-resources {q_tid})",
            "--bind", f"ctrl-a:execute({self_bin} _popup-action add-resource {q_tid})+reload({self_bin} _popup-action list-resources {q_tid})",
            "--bind", f"ctrl-e:execute({self_bin} _popup-action edit-resource {q_tid} {{1}})+reload({self_bin} _popup-action list-resources {q_tid})",
            "--bind", f"ctrl-d:execute({self_bin} _popup-action delete-resource {q_tid} {{1}})+reload({self_bin} _popup-action list-resources {q_tid})",
            "--bind", f"ctrl-x:execute({self_bin} _popup-action delete-resource {q_tid} {{1}})+reload({self_bin} _popup-action list-resources {q_tid})",
        ]

        res = subprocess.run(fzf_cmd, input="\n".join(lines), text=True, capture_output=True, check=False)
        if res.returncode != 0 or not res.stdout.strip():
            return 0

        chosen_res_id = res.stdout.strip().splitlines()[0].split("\t", 1)[0].strip()
        if chosen_res_id == "_add_":
            cmd_popup_action(argparse.Namespace(action="add-resource", task_id=tid, resource_id=None), paths, cfg)
            continue
        if chosen_res_id == "_pilot_":
            cmd_popup_action(argparse.Namespace(action="task-pilot", task_id=tid, resource_id=None), paths, cfg)
            continue

        cmd_popup_action(argparse.Namespace(action="open-resource", task_id=tid, resource_id=chosen_res_id), paths, cfg)


def cmd_popup_action(args: argparse.Namespace, paths: TaskPaths, cfg: dict[str, Any]) -> int:
    action = args.action
    tid = args.task_id
    res_id = getattr(args, "resource_id", None)
    fzf_bin = resolve_bin("fzf", cfg, "fzf_bin")

    if action == "resources":
        return manage_task_resources(tid, paths, cfg)

    if action == "list-resources":
        task = load_task(paths, tid)
        if not task:
            return 1
        for line in format_fzf_resource_lines(task):
            print(line)
        return 0

    if action == "preview-resource":
        task = load_task(paths, tid)
        if not task:
            print(f"Task '{tid}' not found.")
            return 1
        print(format_resource_preview(task, res_id or "_add_", color=True))
        return 0

    if action == "open-resource":
        if res_id == "_pilot_":
            return cmd_popup_action(argparse.Namespace(action="task-pilot", task_id=tid, resource_id=None), paths, cfg)
        if res_id == "_add_" or not res_id:
            return cmd_popup_action(argparse.Namespace(action="add-resource", task_id=tid, resource_id=None), paths, cfg)
        task = load_task(paths, tid)
        if not task:
            return 1
        for r in task.get("resources", []):
            if r.get("id") == res_id or r.get("ref") == res_id:
                target_url = r.get("url") or r.get("ref") or ""
                if target_url and open_url_target(target_url):
                    print(f"Opened: {target_url}")
                    return 0
                return 1
        return 1

    if action == "preview-form":
        state_file = Path(tid)
        if state_file.exists():
            try:
                st_data = json.loads(state_file.read_text(encoding="utf-8"))
                print(format_form_preview(st_data, res_id or "", color=True))
                return 0
            except Exception as e:
                print(f"Error previewing form: {e}")
                return 1
        return 1

    if action == "preview-candidate":
        pfile = Path(tid)
        if pfile.exists():
            try:
                pdata = json.loads(pfile.read_text(encoding="utf-8"))
                print(pdata.get(res_id or "", "No preview available."))
                return 0
            except Exception:
                pass
        return 1

    if action == "edit-resource":
        if res_id == "_pilot_":
            return cmd_popup_action(argparse.Namespace(action="task-pilot", task_id=tid, resource_id=None), paths, cfg)
        if res_id == "_add_" or not res_id:
            return cmd_popup_action(argparse.Namespace(action="add-resource", task_id=tid, resource_id=None), paths, cfg)
        return interactive_resource_form(tid, paths, cfg, resource_id=res_id)

    if action == "delete-resource":
        if res_id in ("_add_", "_pilot_") or not res_id:
            return 0
        task = load_task(paths, tid)
        if not task:
            return 1
        target_res = next(
            (r for r in task.get("resources", []) if r.get("id") == res_id or r.get("ref") == res_id),
            None,
        )
        if not target_res:
            return 0
        print(
            f"\nDelete resource '{target_res.get('ref')} — {target_res.get('label', '')}' from task '{tid}'? [y/N]: ",
            end="",
            flush=True,
        )
        if sys.stdin.readline().strip().lower() not in ("y", "yes"):
            return 0
        return cmd_unlink(argparse.Namespace(target=target_res["id"], task=tid, no_herdr=False), paths, cfg)

    if action == "cleanup":
        print(f"Clean up task '{tid}' (closes Herdr workspace, removes worktrees, archives task)? [y/N]: ", end="", flush=True)
        if sys.stdin.readline().strip().lower() not in ("y", "yes"):
            return 0
        rc = cmd_cleanup(
            argparse.Namespace(task_id=tid, force=False, delete_branches=False, no_herdr=False, json=False),
            paths,
            cfg,
        )
        if rc != 0:
            print("\nForce cleanup despite uncommitted changes? [y/N]: ", end="", flush=True)
            if sys.stdin.readline().strip().lower() in ("y", "yes"):
                cmd_cleanup(
                    argparse.Namespace(task_id=tid, force=True, delete_branches=False, no_herdr=False, json=False),
                    paths,
                    cfg,
                )
            else:
                print("Cleanup aborted. Press Enter to return...")
                sys.stdin.readline()
        return 0

    if action == "add-repo":
        rc = cmd_add(
            argparse.Namespace(repo=None, branch=None, task=tid, focus=True, no_herdr=False, json=False),
            paths,
            cfg,
        )
        if rc != 0:
            print("\nError adding repository worktree(s). Press Enter to continue...")
            sys.stdin.readline()
        return 0

    if action == "add-resource":
        return interactive_resource_form(tid, paths, cfg, resource_id=None)

    if action == "set-status":
        task = load_task(paths, tid)
        if not task:
            return 1
        cur_st = (task.get("status") or "in_progress").lower()
        status_items = [
            f"{st}\t{TASK_STATUS_ICONS.get(st, '•')} {st.upper()}"
            + ("  (current)" if st == cur_st else "")
            for st in ("todo", "in_progress", "in_review", "done")
        ]
        res = subprocess.run(
            [
                fzf_bin,
                "--delimiter=\t",
                "--with-nth=2",
                f"--prompt=Task Status ({tid}) > ",
                f"--header=Transition '{task.get('title', tid)}' (runs lifecycle automations e.g. Jira status sync):",
            ],
            input="\n".join(status_items),
            capture_output=True,
            text=True,
            check=False,
        )
        if res.returncode != 0 or not res.stdout.strip():
            return 0
        chosen_st = res.stdout.strip().split("\t", 1)[0].strip()
        return cmd_update(
            argparse.Namespace(
                task_id=tid,
                task=tid,
                title=None,
                brief=None,
                status=chosen_st,
                branch=None,
                repo_branch=None,
                sync_pr_branches=False,
                force=False,
                no_herdr=False,
                json=False,
            ),
            paths,
            cfg,
        )

    if action == "sync-task":
        return cmd_sync(
            argparse.Namespace(task_id=tid, task=tid, no_herdr=False, json=False),
            paths,
            cfg,
        )

    if action == "pilot":
        tasks_count = len(list_all_tasks(paths))
        header_text = (
            "No active tasks yet. Choose how to initialize your new task (or Esc to cancel):"
            if tasks_count == 0
            else "Choose how to initialize your new task (or Esc to cancel):"
        )
        choice, pilot_cmd_list = _select_pilot_choice_and_cmd(
            cfg,
            fzf_bin,
            prompt="Create Task > ",
            header=header_text,
            include_quick=True,
        )
        if not choice:
            return 0

        if choice == "quick":
            return interactive_task_form(paths, cfg, task_id=None)

        pilot_env = os.environ.copy()
        pilot_env.pop("HERDR_TASK_ID", None)
        pilot_env.pop("HERDR_WORKSPACE_ID", None)
        pilot_env["HERDR_PILOT_MODE"] = "new_task"

        print(f"\n--- Launching Temporary Pilot Agent ({' '.join(pilot_cmd_list)}) ---")
        print("Tell the Pilot what task to initialize (e.g. 'Setup task for CHRON-452 on chronos-api').")
        print("When done, type 'exit' or press Ctrl-C to return to Mission Control.\n")
        subprocess.run(pilot_cmd_list, env=pilot_env, cwd=os.path.expanduser("~"), check=False)
        return 0

    if action == "task-pilot":
        task = load_task(paths, tid)
        if not task:
            print(f"Task '{tid}' not found.")
            return 1

        task_title = task.get("title", tid)
        choice, pilot_cmd_list = _select_pilot_choice_and_cmd(
            cfg,
            fzf_bin,
            prompt=f"Task Pilot ({tid}) > ",
            header=f"Select AI Pilot model to manage existing task '{tid} — {task_title}' (or Esc to cancel):",
            include_quick=False,
        )
        if not choice:
            return 0

        pilot_env = os.environ.copy()
        pilot_env["HERDR_TASK_ID"] = tid
        pilot_env["HERDR_PILOT_MODE"] = "existing_task"
        pilot_env.pop("HERDR_WORKSPACE_ID", None)

        pilot_cwd = os.path.expanduser("~")
        for wt in task.get("worktrees", []):
            wt_p = wt.get("worktree_path") or ""
            if wt_p and os.path.isdir(wt_p):
                pilot_cwd = wt_p
                break

        print(f"\n--- 🤖 Task Pilot Agent on '{tid}' ({task_title}) ---")
        print(f"Environment: HERDR_TASK_ID={tid}  HERDR_PILOT_MODE=existing_task  CWD={pilot_cwd}")
        print("Ask the Pilot to link PRs/tickets, add repo worktrees, fetch Jira/GitHub context, or add notes.")
        print("When done, type 'exit' or press Ctrl-C to return to Mission Control.\n")
        subprocess.run(pilot_cmd_list, env=pilot_env, cwd=pilot_cwd, check=False)
        return 0

    if action == "edit-task":
        return interactive_task_form(paths, cfg, task_id=tid)

    return 0


def cmd_popup(args: argparse.Namespace, paths: TaskPaths, cfg: dict[str, Any]) -> int:
    fzf_bin = resolve_bin("fzf", cfg, "fzf_bin")
    self_bin = resolve_bin("herdr-task")

    tasks = list_all_tasks(paths)
    if not tasks:
        return cmd_popup_action(
            argparse.Namespace(action="pilot", task_id="_none_", resource_id=None),
            paths,
            cfg,
        )

    header = (
        "[Enter] Resources  [Ctrl-S] Status  [Ctrl-R] Sync  "
        "[Ctrl-P] Pilot  [Ctrl-E] Edit  [Ctrl-N] New  [Ctrl-A] +Repo  [Ctrl-X] Cleanup"
    )

    while True:
        tasks = list_all_tasks(paths)
        if not tasks:
            return 0
        lines = [format_fzf_task_line(t) for t in tasks]

        fzf_cmd = [
            fzf_bin,
            "--ansi",
            "--delimiter=\t",
            "--with-nth=1,2",
            "--header", header,
            "--expect", "ctrl-n",
            "--prompt", "Task Info & Resources > ",
            "--preview", f"{self_bin} status {{1}} --color",
            "--preview-window", "down:64%:wrap",
            "--bind", f"ctrl-s:execute({self_bin} _popup-action set-status {{1}})+reload({self_bin} list --fzf)",
            "--bind", f"ctrl-r:execute({self_bin} _popup-action sync-task {{1}})+reload({self_bin} list --fzf)",
            "--bind", f"ctrl-p:execute({self_bin} _popup-action task-pilot {{1}})+reload({self_bin} list --fzf)",
            "--bind", f"ctrl-e:execute({self_bin} _popup-action edit-task {{1}})+reload({self_bin} list --fzf)",
            "--bind", f"ctrl-a:execute({self_bin} _popup-action add-repo {{1}})+reload({self_bin} list --fzf)",
            "--bind", f"ctrl-x:execute({self_bin} _popup-action cleanup {{1}})+reload({self_bin} list --fzf)",
        ]

        res = subprocess.run(fzf_cmd, input="\n".join(lines), text=True, capture_output=True, check=False)
        if not res.stdout:
            return 0

        out_lines = res.stdout.splitlines()
        key = out_lines[0].strip() if out_lines else ""
        if key == "ctrl-n":
            cmd_popup_action(
                argparse.Namespace(action="pilot", task_id="_none_", resource_id=None),
                paths,
                cfg,
            )
            continue

        if res.returncode != 0 or len(out_lines) < 2:
            return 0

        selected_line = out_lines[1]
        chosen_tid = selected_line.split("\t", 1)[0].strip()
        if not chosen_tid:
            return 0

        manage_task_resources(chosen_tid, paths, cfg)
