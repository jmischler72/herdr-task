# Herdr Task (`herdr-task`)

Lightweight multi-repository task, git worktree, 4-pane Cockpit, and modular lifecycle automation orchestrator for [Herdr](https://herdr.dev).

## Core Mental Model

* **1 Herdr Workspace = 1 Task** (`HERDR_TASK_ID` is automatically injected into every workspace tab).
* **1 Tab = 1 Repository Git Worktree** provisioned at `~/.herdr/worktrees/<task-id>/<repo>`.
* **4-Pane Cockpit Layout per Repo Tab**:
  * `code` (`nvim .`)
  * `agent` (`copilot` / `claude`)
  * `term` (shell)
  * `git` (`lazygit`)
* **Centralized Resource Hub**: Attach Jira tickets, GitHub PRs/Issues (with zero-AI-token `gh pr view` auto-enrichment), Slack threads, Grafana/Datadog/Sentry alerts, and Notion/Confluence/Figma docs to a task, plus cross-repo handoff notes.
* **Modular Lifecycle Automations (`automations/*.py`)**: Event-driven drop-in modules triggered on `task.created`, `task.status_changed`, `resource.linked`, `task.sync`, and `task.cleanup`.

---

## Modular Automations

Automations live in `automations/*.py` (built-in) or `~/.config/herdr/tasks/automations/*.py` (user custom modules/overrides) and are configured via `~/.config/herdr/tasks/config.json`.

### Built-in: `jira_status_sync`
Synchronizes task lifecycle statuses (`todo`, `in_progress`, `in_review`, `done`, `archived`) bidirectionally with linked Jira tickets via **Jira Cloud REST API v3** (0 dependencies, 0 AI tokens):
* **Push (Herdr Task $\rightarrow$ Jira)**: Running `herdr-task update --status in_review` (or pressing `[Ctrl-S]` in `herdr-task popup`) queries available Jira transitions on linked tickets and transitions them to match the new task status.
* **Pull/Enrich (Jira $\rightarrow$ Herdr Task)**: Linking a ticket (`herdr-task link PROJ-123`) auto-fetches its Jira title, status badge, and summary; running `herdr-task sync` (or `[Ctrl-R]` in `herdr-task popup`) pulls the latest Jira ticket status back into `herdr-task`.

### Writing a Custom Automation Module
Drop any `.py` file into `~/.config/herdr/tasks/automations/my_hook.py`:

```python
AUTOMATION_ID = "my_hook"
DESCRIPTION = "Custom lifecycle hook"
EVENTS = ("task.created", "task.status_changed", "resource.linked", "task.sync", "task.cleanup")
DEFAULT_SETTINGS = {"enabled": True}

def handle_event(event: str, context: dict, settings: dict) -> dict | None:
    task = context["task"]
    # Perform custom action or mutate `task` in-place
    return {"task_modified": False, "messages": [f"Handled {event} for {task['id']}"]}
```

---

## Installation

### 1. Install via Herdr Plugin CLI (or clone)

```bash
herdr plugin install jmischler72/herdr-task -y
```

Or link a local checkout:

```bash
git clone https://github.com/jmischler72/herdr-task.git ~/.config/herdr/tasks/plugin
ln -sf ~/.config/herdr/tasks/plugin/bin/herdr-task ~/.local/bin/herdr-task
herdr plugin link ~/.config/herdr/tasks/plugin
```

### 2. Configure Keybinding & Sidebar Token (`~/.config/herdr/config.toml`)

```toml
[ui.sidebar.spaces]
rows = [
  ["state_icon", "workspace"],
  ["branch", "$tickets"]
]

[[keys.command]]
key = "prefix+shift+t"
description = "task hub: mission control"
type = "popup"
command = "herdr-task popup"
width = "85%"
height = "85%"
```

### 3. Configure Repository Roots & Automations (`~/.config/herdr/tasks/config.json`)

```json
{
  "repo_roots": [
    "~/Projects",
    "~/Documents"
  ],
  "worktrees_root": "~/.herdr/worktrees",
  "jira_base_url": "https://your-org.atlassian.net/browse",
  "cockpit_layout": true,
  "cockpit_editor": "nvim .",
  "cockpit_git": "lazygit",
  "cockpit_agent": "copilot",
  "automations": {
    "jira_status_sync": {
      "enabled": true,
      "base_url": "https://your-org.atlassian.net",
      "email_env": "JIRA_EMAIL",
      "api_token_env": "JIRA_API_TOKEN",
      "sync_on_task_create": true,
      "sync_on_task_cleanup": false,
      "pull_task_status_from_jira": true,
      "status_map": {
        "todo": ["To Do", "Backlog", "Open"],
        "in_progress": ["In Progress", "In Development", "Doing"],
        "in_review": ["In Review", "Review", "Code Review", "PR Review"],
        "done": ["Done", "Closed", "Resolved"],
        "archived": ["Done", "Closed", "Resolved"]
      }
    }
  }
}
```

---

## CLI Usage

```bash
# Create a multi-repo task, provision worktrees, and open a Herdr workspace
herdr-task create \
  --id "PROJ-123-feature" \
  --title "Implement cross-service feature" \
  --repos "service-a,service-b" \
  --link "PROJ-123" \
  --link "https://github.com/org/service-a/pull/101" \
  --brief "Technical brief shared across all repo tabs." \
  --focus

# Transition task status (triggers Jira transition via jira_status_sync)
herdr-task update --status in_review

# Bidirectionally sync Jira ticket & GitHub PR statuses
herdr-task sync

# List discovered lifecycle automation modules
herdr-task automations list

# Open interactive Mission Control popup (fzf)
# Keys: [Enter] Resources | [Ctrl-S] Status | [Ctrl-R] Sync | [Ctrl-P] Pilot | [Ctrl-E] Edit | [Ctrl-X] Cleanup
herdr-task popup
```
