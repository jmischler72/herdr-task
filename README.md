# Herdr Task (`herdr-task`)

Lightweight multi-repository task, git worktree, and 4-pane Cockpit orchestrator for [Herdr](https://herdr.dev).

## Core Mental Model

* **1 Herdr Workspace = 1 Task** (`HERDR_TASK_ID` is automatically injected into every workspace tab).
* **1 Tab = 1 Repository Git Worktree** provisioned at `~/.herdr/worktrees/<task-id>/<repo>`.
* **4-Pane Cockpit Layout per Repo Tab**:
  * `code` (`nvim .`)
  * `agent` (`copilot` / `claude`)
  * `term` (shell)
  * `git` (`lazygit`)
* **Centralized Resource Hub**: Attach Jira tickets, GitHub PRs/Issues (with zero-AI-token `gh pr view` auto-enrichment), Slack threads, Grafana/Datadog/Sentry alerts, and Notion/Confluence/Figma docs to a task, plus cross-repo handoff notes.

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

### 3. Configure Repository Roots (`~/.config/herdr/tasks/config.json`)

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
  "cockpit_agent": "copilot"
}
```

### 4. Optional: Install Agent Skill (Claude Code / Gemini / Copilot)

```bash
mkdir -p ~/.agents/skills
ln -sf ~/.config/herdr/tasks/plugin/skills/herdr-task ~/.agents/skills/herdr-task
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

# View active task status, worktrees, resources, and handoff notes
herdr-task status

# Add another repository worktree + Cockpit tab to the current task
herdr-task add service-c

# Link or update a PR, Jira ticket, Slack thread, alert, or doc
herdr-task link "https://github.com/org/service-a/pull/101" --status "APPROVED" --sync-branch

# Switch a repo worktree branch in-place (preserving running pane CWDs)
herdr-task update --repo-branch "service-a:feat/new-branch"

# Record a cross-repo handoff note
herdr-task note "Updated protobuf schema in service-a; ready for service-b consumer."

# Open interactive Mission Control popup (fzf)
herdr-task popup

# Audit uncommitted changes, close workspace, remove worktrees, and archive task
herdr-task cleanup
```
