#!/usr/bin/env python3
"""CLI entry point and command-line argument parser for herdr-task."""

import argparse
import sys
from typing import Any

from herdr_task.backend.commands import (
    cmd_add,
    cmd_automations,
    cmd_cleanup,
    cmd_create,
    cmd_current,
    cmd_focus,
    cmd_integrations,
    cmd_link,
    cmd_list,
    cmd_note,
    cmd_open,
    cmd_status,
    cmd_sync,
    cmd_sync_sidebar,
    cmd_unlink,
    cmd_update,
    cmd_worktree_add,
    cmd_worktree_branch,
)
from herdr_task.backend.models import TaskPaths, VALID_TASK_STATUSES, load_config
from herdr_task.backend.storage import resolve_current_task_id
from herdr_task.ui.forms import interactive_resource_form, interactive_task_form
from herdr_task.ui.mission_control import cmd_popup, cmd_popup_action


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="herdr-task",
        description="Lightweight Task & Worktree Orchestrator for Multi-Repo Herdr Workspaces",
    )
    sub = p.add_subparsers(dest="subcommand")

    # create
    p_create = sub.add_parser("create", help="Create a multi-repo task and provision Herdr workspace/tabs")
    p_create.add_argument("-i", "--interactive", action="store_true", help="Launch interactive field-navigator task form")
    p_create.add_argument("--id", help="Explicit task ID slug (e.g. PLA-4350-rabbitmq)")
    p_create.add_argument("--title", help="Human-readable task title")
    p_create.add_argument("--brief", help="Concise technical brief")
    p_create.add_argument("--task-status", dest="task_status", choices=VALID_TASK_STATUSES, default=None, help="Initial task lifecycle status")
    p_create.add_argument("--repos", help="Comma-separated list of repository names or paths")
    p_create.add_argument("--repo", dest="repo_list", action="append", help="Repository name or path (repeatable)")
    p_create.add_argument("--branch", help="Git branch name across repositories")
    p_create.add_argument("--link", action="append", help="Resource URL or ticket key to link (repeatable)")
    p_create.add_argument("--label", help="Label for linked resource")
    p_create.add_argument("--status", help="Status badge for linked resource")
    p_create.add_argument("--summary", help="Distilled summary for linked resource")
    p_create.add_argument("--spec-stdin", action="store_true", help="Read full task creation spec as JSON from stdin")
    p_create.add_argument("--focus", action="store_true", help="Focus the newly created Herdr workspace")
    p_create.add_argument("--no-herdr", action="store_true", help="Skip Herdr socket calls")
    p_create.add_argument("--json", action="store_true", help="Output created task as JSON")

    # update / edit
    for name in ("update", "edit"):
        p_upd = sub.add_parser(name, help="Update task title, status, brief, default branch, or switch worktree branches")
        p_upd.add_argument("-i", "--interactive", action="store_true", help="Launch interactive field-navigator task form")
        p_upd.add_argument("task_id", nargs="?", help="Task ID (defaults to ambient task)")
        p_upd.add_argument("--task", help="Task ID (alternative flag)")
        p_upd.add_argument("--title", help="Update human-readable task title (also renames Herdr workspace)")
        p_upd.add_argument("--brief", help="Update technical brief (pass '-' to clear)")
        p_upd.add_argument("--status", help=f"Update task lifecycle status ({', '.join(VALID_TASK_STATUSES)}) and run automations")
        p_upd.add_argument("--branch", help="Update default_branch (and switch single-repo worktree)")
        p_upd.add_argument(
            "--repo-branch",
            dest="repo_branch",
            action="append",
            help="Switch a repo worktree to a specific branch: <repo>:<branch> (repeatable)",
        )
        p_upd.add_argument(
            "--sync-pr-branches",
            dest="sync_pr_branches",
            action="store_true",
            help="Automatically switch each attached repo worktree to match its linked GitHub PR branch",
        )
        p_upd.add_argument("--force", action="store_true", help="Force branch switch even if worktree has uncommitted changes")
        p_upd.add_argument("--no-herdr", action="store_true")
        p_upd.add_argument("--json", action="store_true")

    # sync
    p_sync_task = sub.add_parser("sync", help="Bidirectionally sync task & resource statuses (Jira, GitHub PRs)")
    p_sync_task.add_argument("task_id", nargs="?", help="Task ID (defaults to ambient task)")
    p_sync_task.add_argument("--task", help="Task ID (alternative flag)")
    p_sync_task.add_argument("--no-herdr", action="store_true")
    p_sync_task.add_argument("--json", action="store_true")

    # automations
    p_auto = sub.add_parser("automations", help="Inspect modular lifecycle automations")
    p_auto.add_argument("auto_action", nargs="?", default="list", choices=["list"], help="Automation subcommand")
    p_auto.add_argument("--json", action="store_true")

    # integrations
    p_integ = sub.add_parser("integrations", help="Inspect modular integrations, candidates, and options")
    p_integ.add_argument("integ_action", nargs="?", default="list", choices=["list", "candidates", "options"])
    p_integ.add_argument("target", nargs="?", help="Candidate kind (ticket, pull_request, resource) or field (resource.status, task.branch)")
    p_integ.add_argument("--task", help="Task ID")
    p_integ.add_argument("--category", help="Resource category")
    p_integ.add_argument("--provider", help="Resource provider")
    p_integ.add_argument("--repo", help="Repository name")
    p_integ.add_argument("--ref", help="Resource reference/key")
    p_integ.add_argument("--json", action="store_true")

    # current
    p_curr = sub.add_parser("current", help="Resolve active task ID from HERDR_TASK_ID, HERDR_WORKSPACE_ID, or PWD")
    p_curr.add_argument("--json", action="store_true")

    # list
    p_list = sub.add_parser("list", help="List tasks")
    p_list.add_argument("--all", action="store_true", help="Include archived tasks")
    p_list.add_argument("--fzf", action="store_true", help="Format output for fzf")
    p_list.add_argument("--json", action="store_true", help="Output JSON")

    # status / show
    for name in ("status", "show"):
        p_st = sub.add_parser(name, help="Show task status, worktrees, resources, summaries, and notes")
        p_st.add_argument("task_id", nargs="?", help="Task ID (defaults to ambient task)")
        p_st.add_argument("--color", action="store_true", help="ANSI color output")
        p_st.add_argument("--no-herdr", action="store_true")
        p_st.add_argument("--json", action="store_true", help="Output JSON")

    # focus / switch
    for name in ("focus", "switch"):
        p_foc = sub.add_parser(name, help="Focus task workspace/tab in Herdr (auto-reopening if closed)")
        p_foc.add_argument("task_id", nargs="?", help="Task ID (defaults to ambient task)")
        p_foc.add_argument("--repo", help="Focus specific repository tab")

    # link
    p_link = sub.add_parser("link", help="Link or update a resource (Jira, PR, Slack, Alert, Doc) on a task")
    p_link.add_argument("-i", "--interactive", action="store_true", help="Launch interactive field-navigator resource form")
    p_link.add_argument("target", nargs="?", default=None, help="URL, Jira key (PROJ-123), or GitHub shorthand (repo#123)")
    p_link.add_argument("--label", help="Short display label")
    p_link.add_argument("--status", help="Cached status badge (e.g. 'In Progress', 'OPEN')")
    p_link.add_argument("--summary", help="Distilled technical summary (avoids redundant MCP calls)")
    p_link.add_argument("--category", choices=["ticket", "pull_request", "issue", "conversation", "alert", "doc", "custom"])
    p_link.add_argument("--provider", help="Provider name (jira, github, slack, ...)")
    p_link.add_argument("--added-by", dest="added_by", help="Author/agent identifier")
    p_link.add_argument("--metadata", action="append", help="Extra key=value metadata")
    p_link.add_argument("--sync-branch", dest="sync_branch", action="store_true", help="Switch repo worktree to the linked PR's branch")
    p_link.add_argument("--task", help="Task ID (defaults to ambient task)")
    p_link.add_argument("--no-herdr", action="store_true")
    p_link.add_argument("--json", action="store_true")

    # unlink
    p_unlink = sub.add_parser("unlink", help="Remove a linked resource by ID, ref, or URL")
    p_unlink.add_argument("target", help="Resource ID, ref (e.g. CHRON-452), or URL")
    p_unlink.add_argument("--task", help="Task ID (defaults to ambient task)")
    p_unlink.add_argument("--no-herdr", action="store_true")

    # note
    p_note = sub.add_parser("note", help="Append a cross-repo handoff note to the task")
    p_note.add_argument("text", help="Handoff note text")
    p_note.add_argument("--author", help="Author identifier")
    p_note.add_argument("--task", help="Task ID (defaults to ambient task)")
    p_note.add_argument("--json", action="store_true")

    # add
    p_add = sub.add_parser("add", help="Add repository worktree(s) and Cockpit tab to the active task")
    p_add.add_argument("repo", nargs="?", help="Repository name or path (opens fzf autocomplete if omitted)")
    p_add.add_argument("--branch", help="Branch name (defaults to task default_branch)")
    p_add.add_argument("--task", help="Task ID (defaults to ambient task)")
    p_add.add_argument("--no-focus", dest="focus", action="store_false", default=True, help="Don't focus the new tab")
    p_add.add_argument("--no-herdr", action="store_true")
    p_add.add_argument("--json", action="store_true")

    # worktree (compatibility aliases)
    p_wt = sub.add_parser("worktree", help="Manage task git worktrees")
    wt_sub = p_wt.add_subparsers(dest="wt_subcommand")
    p_wt_add = wt_sub.add_parser("add", help="Add a repository worktree and Herdr tab to a task")
    p_wt_add.add_argument("--repo", required=True, help="Repository name or path")
    p_wt_add.add_argument("--label", help="Optional custom tab title")
    p_wt_add.add_argument("--branch", help="Branch name (defaults to task default_branch)")
    p_wt_add.add_argument("--task", help="Task ID (defaults to ambient task)")
    p_wt_add.add_argument("--focus", action="store_true")
    p_wt_add.add_argument("--no-herdr", action="store_true")
    p_wt_add.add_argument("--json", action="store_true")

    for wt_b_name in ("branch", "switch"):
        p_wt_br = wt_sub.add_parser(wt_b_name, help="Switch a registered repo worktree to another git branch")
        p_wt_br.add_argument("--repo", required=True, help="Repository name")
        p_wt_br.add_argument("--branch", required=True, help="Target git branch name")
        p_wt_br.add_argument("--task", help="Task ID (defaults to ambient task)")
        p_wt_br.add_argument("--force", action="store_true", help="Switch even if worktree has uncommitted changes")
        p_wt_br.add_argument("--json", action="store_true")

    # open
    p_open = sub.add_parser("open", help="Open linked resource(s) in browser or Slack desktop app")
    p_open.add_argument("selector", nargs="?", help="Category (jira, slack, pr, alert, doc) or ref/id")
    p_open.add_argument("--task", help="Task ID (defaults to ambient task)")

    # cleanup
    p_clean = sub.add_parser("cleanup", help="Audit git status, close Herdr workspace, remove worktrees, and archive task")
    p_clean.add_argument("task_id", nargs="?", help="Task ID (defaults to ambient task)")
    p_clean.add_argument("--force", action="store_true", help="Force removal even if worktrees have uncommitted changes")
    p_clean.add_argument("--delete-branches", action="store_true", help="Also delete local git branches")
    p_clean.add_argument("--no-herdr", action="store_true")
    p_clean.add_argument("--json", action="store_true")

    # sync-sidebar
    p_sync = sub.add_parser("sync-sidebar", help="Report $tickets metadata token to Herdr sidebar")
    p_sync.add_argument("--json", action="store_true")

    # popup
    sub.add_parser("popup", help="Launch interactive Mission Control fzf popup")

    # internal popup helper
    p_pa = sub.add_parser("_popup-action", help=argparse.SUPPRESS)
    p_pa.add_argument(
        "action",
        choices=[
            "cleanup",
            "add-repo",
            "pilot",
            "task-pilot",
            "edit-task",
            "set-status",
            "sync-task",
            "add-resource",
            "resources",
            "list-resources",
            "preview-resource",
            "preview-form",
            "preview-candidate",
            "open-resource",
            "edit-resource",
            "delete-resource",
        ],
    )
    p_pa.add_argument("task_id")
    p_pa.add_argument("resource_id", nargs="?", default=None)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    paths = TaskPaths()
    cfg = load_config(paths)

    cmd = args.subcommand
    if cmd == "create":
        if getattr(args, "interactive", False):
            return interactive_task_form(paths, cfg, task_id=None, initial_args=args, no_herdr=getattr(args, "no_herdr", False))
        return cmd_create(args, paths, cfg)
    if cmd in ("update", "edit"):
        if getattr(args, "interactive", False):
            tid = resolve_current_task_id(paths, getattr(args, "task_id", None) or getattr(args, "task", None))
            return interactive_task_form(paths, cfg, task_id=tid, initial_args=args, no_herdr=getattr(args, "no_herdr", False))
        return cmd_update(args, paths, cfg)
    if cmd == "sync":
        return cmd_sync(args, paths, cfg)
    if cmd == "automations":
        return cmd_automations(args, paths, cfg)
    if cmd == "integrations":
        return cmd_integrations(args, paths, cfg)
    if cmd == "current":
        return cmd_current(args, paths, cfg)
    if cmd == "list":
        return cmd_list(args, paths, cfg)
    if cmd in ("status", "show"):
        return cmd_status(args, paths, cfg)
    if cmd in ("focus", "switch"):
        return cmd_focus(args, paths, cfg)
    if cmd == "link":
        if getattr(args, "interactive", False) or not getattr(args, "target", None):
            tid = resolve_current_task_id(paths, getattr(args, "task", None))
            if not tid:
                print("Error: could not resolve active task. Pass --task <id>.", file=sys.stderr)
                return 1
            return interactive_resource_form(tid, paths, cfg, initial_target=getattr(args, "target", None), no_herdr=getattr(args, "no_herdr", False))
        return cmd_link(args, paths, cfg)
    if cmd == "unlink":
        return cmd_unlink(args, paths, cfg)
    if cmd == "note":
        return cmd_note(args, paths, cfg)
    if cmd == "add":
        return cmd_add(args, paths, cfg)
    if cmd == "worktree" and getattr(args, "wt_subcommand", None) == "add":
        return cmd_worktree_add(args, paths, cfg)
    if cmd == "worktree" and getattr(args, "wt_subcommand", None) in ("branch", "switch"):
        return cmd_worktree_branch(args, paths, cfg)
    if cmd == "open":
        return cmd_open(args, paths, cfg)
    if cmd == "cleanup":
        return cmd_cleanup(args, paths, cfg)
    if cmd == "sync-sidebar":
        return cmd_sync_sidebar(args, paths, cfg)
    if cmd == "popup":
        return cmd_popup(args, paths, cfg)
    if cmd == "_popup-action":
        return cmd_popup_action(args, paths, cfg)

    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
