#!/usr/bin/env python3
"""
Herdr CLI & Unix domain socket client, Cockpit layout provisioning, and sidebar sync.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import time
from typing import Any

from herdr_task.backend.models import (
    extract_cmd_binary,
    resolve_bin,
)


class HerdrClient:
    def __init__(self, cfg: dict[str, Any]) -> None:
        self.herdr_bin = resolve_bin("herdr", cfg, "herdr_bin")
        self.session = os.environ.get("HERDR_SESSION") or cfg.get("herdr_session")
        if not self.session:
            default_sock = Path(os.path.expanduser("~/.config/herdr/herdr.sock"))
            sessions_dir = Path(os.path.expanduser("~/.config/herdr/sessions"))
            if not default_sock.exists() and sessions_dir.is_dir():
                for s_dir in sessions_dir.iterdir():
                    if s_dir.is_dir() and (s_dir / "herdr.sock").exists():
                        self.session = s_dir.name
                        break

    def run(self, args: list[str], timeout: float = 15.0) -> tuple[int, dict[str, Any] | None, str]:
        cmd = [self.herdr_bin]
        if self.session and "--session" not in args:
            cmd.extend(["--session", self.session])
        cmd.extend(args)
        env = dict(os.environ)
        if self.session:
            env["HERDR_SESSION"] = self.session
            s_sock = os.path.expanduser(f"~/.config/herdr/sessions/{self.session}/herdr.sock")
            if os.path.exists(s_sock):
                env["HERDR_SOCKET"] = s_sock
        try:
            res = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
                env=env,
            )
        except Exception as e:
            return 1, None, str(e)

        out = res.stdout.strip()
        err = res.stderr.strip()
        parsed: dict[str, Any] | None = None
        for stream in (out, err):
            if stream and parsed is None:
                try:
                    cand = json.loads(stream)
                    if isinstance(cand, dict):
                        parsed = cand
                except Exception:
                    pass
        return res.returncode, parsed, err or out

    def list_workspaces(self) -> list[dict[str, Any]]:
        rc, data, _ = self.run(["workspace", "list"])
        if rc == 0 and isinstance(data, dict):
            return (data.get("result") or {}).get("workspaces") or []
        return []

    def list_tabs(self, workspace_id: str) -> list[dict[str, Any]]:
        rc, data, _ = self.run(["tab", "list", "--workspace", workspace_id])
        if rc == 0 and isinstance(data, dict):
            return (data.get("result") or {}).get("tabs") or []
        return []

    def create_workspace(self, cwd: str, label: str, task_id: str, focus: bool = False) -> dict[str, Any] | None:
        cmd = [
            "workspace", "create",
            "--cwd", cwd,
            "--label", label,
            "--env", f"HERDR_TASK_ID={task_id}",
            "--focus" if focus else "--no-focus",
        ]
        rc, data, _ = self.run(cmd)
        if rc == 0 and isinstance(data, dict):
            return data.get("result") or {}
        return None

    def create_tab(self, workspace_id: str, cwd: str, label: str, task_id: str, focus: bool = False) -> dict[str, Any] | None:
        cmd = [
            "tab", "create",
            "--workspace", workspace_id,
            "--cwd", cwd,
            "--label", label,
            "--env", f"HERDR_TASK_ID={task_id}",
            "--focus" if focus else "--no-focus",
        ]
        rc, data, _ = self.run(cmd)
        if rc == 0 and isinstance(data, dict):
            return data.get("result") or {}
        return None

    def rename_tab(self, tab_id: str, label: str) -> bool:
        rc, _, _ = self.run(["tab", "rename", tab_id, label])
        return rc == 0

    def close_workspace(self, workspace_id: str, group: bool = False) -> bool:
        cmd = ["workspace", "close", workspace_id]
        if group:
            cmd.append("--group")
        rc, _, _ = self.run(cmd)
        return rc == 0

    def open_worktree_workspace(
        self,
        repo_path: str | None = None,
        workspace_id: str | None = None,
        worktree_path: str | None = None,
        branch: str | None = None,
        label: str | None = None,
        focus: bool = False,
        trust_repository: bool = True,
    ) -> dict[str, Any] | None:
        cmd = ["worktree", "open"]
        if repo_path:
            cmd.extend(["--cwd", repo_path])
        elif workspace_id:
            cmd.extend(["--workspace", workspace_id])
        if worktree_path:
            cmd.extend(["--path", worktree_path])
        elif branch:
            cmd.extend(["--branch", branch])
        if label:
            cmd.extend(["--label", label])
        if focus:
            cmd.append("--focus")
        else:
            cmd.append("--no-focus")
        if trust_repository:
            cmd.append("--trust-repository")
        rc, data, _ = self.run(cmd)
        if rc == 0 and isinstance(data, dict):
            return data.get("result") or {}
        return None

    def list_worktrees(self, workspace_id: str | None = None) -> list[dict[str, Any]]:
        cmd = ["worktree", "list"]
        if workspace_id:
            cmd.extend(["--workspace", workspace_id])
        rc, data, _ = self.run(cmd)
        if rc == 0 and isinstance(data, dict):
            return (data.get("result") or {}).get("worktrees") or []
        return []

    def rename_workspace(self, workspace_id: str, label: str) -> bool:
        rc, _, _ = self.run(["workspace", "rename", workspace_id, label])
        return rc == 0

    def focus_workspace(self, workspace_id: str) -> bool:
        rc, _, _ = self.run(["workspace", "focus", workspace_id])
        return rc == 0

    def focus_tab(self, tab_id: str) -> bool:
        rc, _, _ = self.run(["tab", "focus", tab_id])
        return rc == 0

    def split_pane(self, target_pane_id: str, direction: str, cwd: str, no_focus: bool = True) -> str | None:
        cmd = ["pane", "split", target_pane_id, "--direction", direction, "--cwd", cwd]
        if no_focus:
            cmd.append("--no-focus")
        rc, data, _ = self.run(cmd)
        if rc == 0 and isinstance(data, dict):
            return ((data.get("result") or {}).get("pane") or {}).get("pane_id")
        return None

    def rename_pane(self, pane_id: str, label: str) -> bool:
        rc, _, _ = self.run(["pane", "rename", pane_id, label])
        return rc == 0

    def run_in_pane(self, pane_id: str, command: str) -> bool:
        rc, _, _ = self.run(["pane", "run", pane_id, command])
        return rc == 0

    def provision_repo_cockpit(
        self,
        root_pane_id: str,
        cwd: str,
        agent_cmd: str | None = None,
        editor_cmd: str | None = None,
        git_cmd: str | None = None,
    ) -> dict[str, str]:
        p_editor = root_pane_id
        self.rename_pane(p_editor, "code")

        p_agent = self.split_pane(p_editor, direction="right", cwd=cwd, no_focus=True)
        if p_agent:
            self.rename_pane(p_agent, "agent")

        p_term = self.split_pane(p_editor, direction="down", cwd=cwd, no_focus=True)
        if p_term:
            self.rename_pane(p_term, "term")

        p_git = None
        if p_agent:
            p_git = self.split_pane(p_agent, direction="down", cwd=cwd, no_focus=True)
            if p_git:
                self.rename_pane(p_git, "git")

        time.sleep(0.25)

        e_cmd = (editor_cmd or os.environ.get("EDITOR") or "nvim .").strip()
        e_bin = extract_cmd_binary(e_cmd)
        if e_bin and (shutil.which(e_bin) or os.path.exists(e_bin)):
            self.run_in_pane(p_editor, e_cmd)
        elif shutil.which("vim"):
            self.run_in_pane(p_editor, "vim .")

        g_cmd = (git_cmd or "lazygit").strip()
        g_bin = extract_cmd_binary(g_cmd)
        if p_git and g_bin and (shutil.which(g_bin) or os.path.exists(g_bin)):
            self.run_in_pane(p_git, g_cmd)

        if p_agent and agent_cmd and agent_cmd.strip():
            self.run_in_pane(p_agent, agent_cmd.strip())

        return {
            "editor": p_editor,
            "agent": p_agent or "",
            "term": p_term or "",
            "git": p_git or "",
        }

    def sync_sidebar_metadata(self, task: dict[str, Any]) -> bool:
        ws_id = task.get("workspace_id")
        if not ws_id:
            return False

        tickets = [
            r.get("ref") or r.get("label")
            for r in task.get("resources", [])
            if r.get("category") == "ticket" and (r.get("ref") or r.get("label"))
        ]
        ticket_str = ", ".join(tickets[:2])

        cmd = ["workspace", "report-metadata", ws_id, "--source", "task-pilot"]
        if ticket_str:
            cmd.extend(["--token", f"tickets={ticket_str}"])
        else:
            cmd.extend(["--clear-token", "tickets"])

        for i in range(1, 5):
            cmd.extend(["--clear-token", f"proj{i}"])

        rc, _, _ = self.run(cmd)
        return rc == 0


def compute_workspace_label(task: dict[str, Any]) -> str:
    task_id = task["id"]
    t_key = next(
        (r.get("ref") for r in task.get("resources", []) if r.get("category") == "ticket" and r.get("ref")),
        None,
    )
    title_text = task.get("title") or task_id
    if t_key and not title_text.startswith(t_key) and f"[{t_key}]" not in title_text:
        return f"[{t_key}] {title_text}"[:28].strip()
    return title_text[:28].strip()


def _provision_tab_cockpit(herdr: HerdrClient, wt: dict[str, Any], root_pane: str | None, cfg: dict[str, Any]) -> None:
    wt["pane_id"] = root_pane
    if cfg.get("cockpit_layout", True) and root_pane:
        wt["cockpit_panes"] = herdr.provision_repo_cockpit(
            root_pane_id=root_pane,
            cwd=wt["worktree_path"],
            agent_cmd=cfg.get("cockpit_agent", "copilot"),
            editor_cmd=cfg.get("cockpit_editor", "nvim ."),
            git_cmd=cfg.get("cockpit_git", "lazygit"),
        )


def provision_herdr_workspace_for_task(
    herdr: HerdrClient,
    task: dict[str, Any],
    focus: bool = False,
    cfg: dict[str, Any] | None = None,
) -> bool:
    cfg = cfg or {}
    task_id = task["id"]
    wts = task.get("worktrees", [])
    ws_label = compute_workspace_label(task)
    initial_cwd = wts[0]["worktree_path"] if wts else os.path.expanduser("~")

    ws_res = herdr.create_workspace(initial_cwd, ws_label, task_id, focus=focus)
    if not ws_res:
        return False

    ws_id = (ws_res.get("workspace") or {}).get("workspace_id")
    first_tab_id = (ws_res.get("tab") or {}).get("tab_id")
    first_pane_id = (
        (ws_res.get("root_pane") or {}).get("pane_id")
        or (ws_res.get("pane") or {}).get("pane_id")
    )
    if not ws_id:
        return False

    task["workspace_id"] = ws_id

    if not wts:
        if first_tab_id:
            herdr.rename_tab(first_tab_id, "scratch")
        herdr.sync_sidebar_metadata(task)
        return True

    # Reuse the workspace's initial tab directly for the first repository
    if first_tab_id and first_pane_id:
        herdr.rename_tab(first_tab_id, wts[0].get("tab_title") or wts[0]["repo"])
        wts[0]["tab_id"] = first_tab_id
        _provision_tab_cockpit(herdr, wts[0], first_pane_id, cfg)
        remaining_wts = wts[1:]
    else:
        remaining_wts = wts

    for idx, wt in enumerate(remaining_wts):
        wt_label = wt.get("tab_title") or wt["repo"]
        t_res = herdr.create_tab(
            ws_id,
            wt["worktree_path"],
            wt_label,
            task_id,
            focus=(focus and idx == 0 and not first_pane_id),
        )
        if t_res:
            wt["tab_id"] = (t_res.get("tab") or {}).get("tab_id")
            _provision_tab_cockpit(herdr, wt, (t_res.get("root_pane") or {}).get("pane_id"), cfg)

    herdr.sync_sidebar_metadata(task)
    return True
