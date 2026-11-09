#!/usr/bin/env python3
"""
Data models, paths, configuration, and validation helpers for herdr_task.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shlex
import shutil
from typing import Any


VALID_TASK_STATUSES = ("todo", "in_progress", "in_review", "done", "archived")

TASK_STATUS_ICONS: dict[str, str] = {
    "todo": "📋",
    "in_progress": "🚧",
    "in_review": "👀",
    "done": "✅",
    "archived": "📦",
}

DEFAULT_CONFIG: dict[str, Any] = {
    "repo_roots": [
        os.path.expanduser("~/Projects"),
        os.path.expanduser("~/Documents"),
    ],
    "worktrees_root": os.path.expanduser("~/.herdr/worktrees"),
    "jira_base_url": "https://your-org.atlassian.net/browse",
    "herdr_bin": "/opt/homebrew/bin/herdr",
    "fzf_bin": "/opt/homebrew/bin/fzf",
    "cockpit_layout": True,
    "cockpit_editor": "nvim .",
    "cockpit_git": "lazygit",
    "cockpit_agent": "copilot",
    "integrations": {
        "jira": {
            "enabled": True,
        },
        "github": {
            "enabled": True,
        },
    },
    "automations": {
        "jira_status_sync": {
            "enabled": True,
        },
    },
    "pilot_models": [
        {
            "id": "copilot-luna",
            "label": "🤖 Copilot Light: gpt-6-luna (22x cheaper ~0.3 AIU, all tools enabled)",
            "bin": "copilot",
            "model": "gpt-6-luna",
        },
        {
            "id": "copilot-terra",
            "label": "🤖 Copilot Medium: gpt-6.1-terra (3x cheaper, all tools enabled)",
            "bin": "copilot",
            "model": "gpt-6.1-terra",
        },
        {
            "id": "copilot-sonnet",
            "label": "🤖 Copilot Sonnet: claude-sonnet-5.5 (All tools enabled)",
            "bin": "copilot",
            "model": "claude-sonnet-5.5",
        },
        {
            "id": "claude-haiku",
            "label": "🤖 Claude CLI (Lightweight Model: haiku)",
            "bin": "claude",
            "model": "haiku",
        },
    ],
}

SAFE_TASK_ID_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]{0,127}$")
INVALID_BRANCH_CHARS_RE = re.compile(r"[\x00-\x20~^:?*\[\\]")
ALLOWED_OPEN_SCHEMES = {"http", "https", "slack"}

IGNORED_JIRA_PREFIXES = {
    "UTF", "SHA", "AES", "RSA", "TLS", "SSL", "HTTP", "HTTPS", "IPV",
    "CVE", "ISO", "RFC", "PEP", "PR", "GH", "V1", "V2", "V3", "RC",
    "ARM", "AMD", "X86", "JDK", "NODE", "CPU", "GPU", "RAM", "SQL",
}


def _extract_jira_key_from_text(text: str | None) -> str | None:
    if not text:
        return None
    for m_jk in re.finditer(r"\b([A-Z][A-Z0-9_]{1,15})-(\d+)\b", text):
        prefix = m_jk.group(1)
        if prefix not in IGNORED_JIRA_PREFIXES:
            return f"{prefix}-{m_jk.group(2)}"
    return None


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def slugify(text: str, max_len: int = 48) -> str:
    s = re.sub(r"[^a-zA-Z0-9._-]+", "-", text.strip()).strip("-._")
    return s[:max_len].rstrip("-._") or "task"


def is_valid_task_id(task_id: str | None) -> bool:
    if not task_id or not isinstance(task_id, str):
        return False
    tid = task_id.strip()
    return bool(SAFE_TASK_ID_RE.match(tid) and ".." not in tid)


def validate_git_branch(branch: str) -> str:
    b = branch.strip()
    if (
        not b
        or b.startswith("-")
        or b.startswith("/")
        or b.endswith("/")
        or b.endswith(".")
        or b.endswith(".lock")
        or ".." in b
        or "@{" in b
        or "//" in b
        or INVALID_BRANCH_CHARS_RE.search(b)
    ):
        raise ValueError(f"Invalid git branch name: {branch!r}")
    return b


def resolve_bin(name: str, cfg: dict[str, Any] | None = None, cfg_key: str | None = None) -> str:
    if cfg and cfg_key:
        configured = cfg.get(cfg_key)
        if isinstance(configured, str) and configured.strip():
            cand = os.path.expanduser(configured.strip())
            if shutil.which(cand) or os.path.exists(cand):
                return cand
    found = shutil.which(name)
    if found:
        return found
    for prefix in ("/opt/homebrew/bin", "/usr/local/bin"):
        fallback = os.path.join(prefix, name)
        if os.path.exists(fallback):
            return fallback
    return name


def extract_cmd_binary(cmd_str: str) -> str | None:
    try:
        parts = shlex.split(cmd_str.strip())
    except ValueError:
        parts = cmd_str.strip().split()
    return parts[0] if parts else None


class TaskPaths:
    def __init__(self, base_dir: str | Path | None = None) -> None:
        raw = base_dir or os.environ.get("HERDR_TASKS_DIR") or "~/.config/herdr/tasks"
        self.base = Path(os.path.expanduser(str(raw))).resolve()
        self.archive = self.base / "archive"
        self.user_integrations = self.base / "integrations"
        self.user_automations = self.base / "automations"
        self.lock_file = self.base / ".lock"
        self.config_file = self.base / "config.json"

    def ensure_dirs(self) -> None:
        self.base.mkdir(parents=True, exist_ok=True)
        self.archive.mkdir(parents=True, exist_ok=True)

    def integration_search_dirs(self) -> list[Path]:
        dirs: list[Path] = []
        # Relative to current file: ../../integrations
        pkg_root = Path(__file__).resolve().parent.parent.parent
        plugin_rel = pkg_root / "integrations"
        builtin_plugin = Path(os.path.expanduser("~/.config/herdr/tasks/plugin/integrations"))
        legacy_automations = pkg_root / "automations"
        builtin_automations = Path(os.path.expanduser("~/.config/herdr/tasks/plugin/automations"))

        candidates = (
            plugin_rel,
            builtin_plugin,
            self.base / "plugin" / "integrations",
            self.user_integrations,
            legacy_automations,
            builtin_automations,
            self.base / "plugin" / "automations",
            self.user_automations,
        )
        for cand in candidates:
            try:
                resolved = cand.resolve()
                if resolved.is_dir() and resolved not in dirs:
                    dirs.append(resolved)
            except Exception:
                continue
        return dirs

    def automation_search_dirs(self) -> list[Path]:
        # Alias for backward compatibility
        return self.integration_search_dirs()

    def _safe_json_path(self, directory: Path, task_id: str) -> Path:
        tid = task_id.strip()
        if not is_valid_task_id(tid):
            raise ValueError(f"Invalid task ID: {task_id!r}")
        resolved_dir = directory.resolve()
        candidate = (resolved_dir / f"{tid}.json").resolve()
        if candidate.parent != resolved_dir:
            raise ValueError(f"Task ID escapes storage directory: {task_id!r}")
        return candidate

    def task_file(self, task_id: str) -> Path:
        return self._safe_json_path(self.base, task_id)

    def archive_file(self, task_id: str) -> Path:
        return self._safe_json_path(self.archive, task_id)


def load_config(paths: TaskPaths) -> dict[str, Any]:
    paths.ensure_dirs()
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    if paths.config_file.exists():
        try:
            user_cfg = json.loads(paths.config_file.read_text(encoding="utf-8"))
            if isinstance(user_cfg, dict):
                user_autos = user_cfg.get("automations")
                user_integs = user_cfg.get("integrations")
                for k, v in user_cfg.items():
                    if k not in ("automations", "integrations"):
                        cfg[k] = v
                if isinstance(user_autos, dict):
                    cfg_autos = cfg.setdefault("automations", {})
                    cfg_integs = cfg.setdefault("integrations", {})
                    for auto_id, auto_conf in user_autos.items():
                        if isinstance(auto_conf, dict):
                            cfg_autos.setdefault(auto_id, {}).update(auto_conf)
                            cfg_integs.setdefault(auto_id, {}).update(auto_conf)
                        else:
                            cfg_autos[auto_id] = auto_conf
                            cfg_integs[auto_id] = auto_conf
                if isinstance(user_integs, dict):
                    cfg_integs = cfg.setdefault("integrations", {})
                    for integ_id, integ_conf in user_integs.items():
                        if isinstance(integ_conf, dict):
                            cfg_integs.setdefault(integ_id, {}).update(integ_conf)
                        else:
                            cfg_integs[integ_id] = integ_conf
        except Exception:
            pass
    env_wt = os.environ.get("HERDR_WORKTREES_DIR", "").strip()
    if env_wt:
        cfg["worktrees_root"] = env_wt
    return cfg
