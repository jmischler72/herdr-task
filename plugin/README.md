# Herdr Task (`herdr-task`)

Lightweight multi-repository task, git worktree, 4-pane Cockpit, and modular lifecycle automation orchestrator for [Herdr](https://herdr.dev).

## Core Mental Model

* **1 Task = Primary Task Workspace (`HERDR_TASK_ID`)**: Acts as the mission control hub for cross-repo task overview, resources, notes, and lifecycle automations.
* **Repositories = Clickable Linked Worktree Workspaces (`is_linked_worktree: true`)**: Repositories appear directly in Herdr's sidebar nested under the task, allowing **unlimited tabs per repository worktree**.
* **4-Pane Cockpit Layout per Repo Worktree**: Initial tab comes with `code` (`nvim .`), `agent` (`copilot`), `term` (shell), and `git` (`lazygit`).
* **Centralized Resource Hub**: Attach Jira tickets, GitHub PRs/Issues (with zero-AI-token `gh pr view` auto-enrichment), Slack threads, Grafana/Datadog/Sentry alerts, and Notion/Confluence/Figma docs to a task, plus cross-repo handoff notes.
* **Modular Lifecycle Automations (`automations/*.py`)**: Event-driven drop-in modules triggered on `task.created`, `task.status_changed`, `resource.linked`, `task.sync`, and `task.cleanup`.
* **Modular Integrations & Interactive Form Navigator (`integrations/*.py`)**: Dropdown pickers and candidate providers for Jira tickets (live search + browser redirect), GitHub PRs (`gh pr list` + browser redirect), repositories, Git branches, and provider-specific status badges.

---

## Interactive Field-Navigator Forms & Modular Integrations

When creating or editing tasks and resources (via `herdr-task popup` or CLI `-i / --interactive`), `herdr-task` presents an interactive **Field-Navigator Form**:

1. **All fields visible at once** in a navigable list with live ANSI preview of the full card and unsaved changes.
2. Select any field with `Up/Down` (or fuzzy filter) and press `Enter` to edit **just that field**, returning immediately to the form.
3. **Dropdowns & Pickers for Fixed & Dynamic Values**:
   - **Task Status**: `📋 TODO`, `🚧 IN_PROGRESS`, `👀 IN_REVIEW`, `✅ DONE`, `📦 ARCHIVED`.
   - **Repositories**: `fzf -m` multi-select from configured `repo_roots`.
   - **Git Branches**: Local & remote branches (`git for-each-ref`) for the repository, plus linked PR branches or custom input.
   - **Jira Tickets**: Queries Jira Cloud REST API v3 for your assigned open issues (`assignee = currentUser()`), includes `🌐 Open Jira in Browser` shortcuts, and caches recent tickets offline.
   - **GitHub PRs**: Queries live open PRs via `gh pr list`, includes `🌐 Open GitHub PRs in Browser`, and caches recent PRs.
   - **Status Badges**: Populated dynamically by modular integrations (live Jira workflow transitions, GitHub PR states `OPEN`/`DRAFT`/`APPROVED`/`MERGED`, Alert states `FIRING`/`RESOLVED`, Doc states `DRAFT`/`APPROVED`).
4. **Pre-filled Inline Text Editing**: Free-text fields (`Title`, `Brief`, `Label`, `Summary`, `URL`, `Task ID`) pre-fill their current value into `readline` so you can edit in-place with arrow keys and Backspace.
5. **Keyboard Shortcuts**:
   - `[Ctrl-S]` / `✅ Save`: Save and apply all changes.
   - `[Ctrl-O]`: Open relevant Jira ticket, GitHub PR, or URL in default browser without leaving the form.
   - `[Esc]` / `❌ Cancel`: Discard changes and return.

### Writing a Custom Integration Module

Drop any `.py` file into `~/.config/herdr/tasks/integrations/my_provider.py` (or implement hooks inside an existing automation in `automations/`):

```python
INTEGRATION_ID = "my_provider"
DESCRIPTION = "Custom candidate and status badge provider"
DEFAULT_SETTINGS = {"enabled": True}

def provide_candidates(kind: str, context: dict, settings: dict) -> list[dict]:
    # Return selectable items for Jira/PR/resource pickers
    return [
        {
            "value": "PROJ-999",
            "label": "🎫 PROJ-999 — Critical bug",
            "category": "ticket",
            "provider": "jira",
            "preview": "Detailed ticket preview...",
        },
        {
            "value": "__open_url__:https://internal.portal/dashboard",
            "action": "open_url",
            "url": "https://internal.portal/dashboard",
            "label": "🌐 Open Internal Dashboard in Browser",
        },
    ]

def provide_field_options(field: str, context: dict, settings: dict) -> list[dict]:
    # Return dropdown options for fields like "resource.status", "task.branch"
    if field == "resource.status":
        return [{"value": "TRIAGED", "label": "👀 TRIAGED — Reviewed by lead"}]
    return []
```

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

# Launch interactive task creation form with Jira & GitHub pickers
herdr-task create -i

# Edit an existing task interactively (field navigator with branch & status dropdowns)
herdr-task edit -i

# Link or edit a resource interactively
herdr-task link -i

# Bidirectionally sync Jira ticket & GitHub PR statuses
herdr-task sync

# List discovered lifecycle automation modules
herdr-task automations list

# Inspect modular integration providers and query candidates / dropdown options
herdr-task integrations list
herdr-task integrations candidates ticket
herdr-task integrations options resource.status --category pull_request

# Open interactive Mission Control popup (fzf)
# Keys: [Enter] Resources | [Ctrl-S] Status | [Ctrl-R] Sync | [Ctrl-P] Pilot | [Ctrl-E] Edit | [Ctrl-X] Cleanup
herdr-task popup
```
