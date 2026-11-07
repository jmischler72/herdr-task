---
name: herdr-task
description: Centralize multi-repository tasks, git worktrees, Jira tickets, GitHub PRs, Slack threads, alerts, and docs in Herdr via herdr-task.
---

# `herdr-task` Skill (`~/.local/bin/herdr-task`)

1 Herdr Workspace = 1 Task (`HERDR_TASK_ID` is set inside task tabs). 1 Tab = 1 Repo Worktree (4-pane cockpit: `nvim`, `copilot`, `term`, `lazygit`).

## Role 1: Pilot Agent (Creating / Tracking a New Task)
When asked to create or track a task (or when `HERDR_PILOT_MODE=new_task` / `HERDR_TASK_ID` is unset):
- **NEVER link resources to an existing task.** Always create a new task with `herdr-task create`.
- **Step 1 — Inspect provided PRs / Tickets first**:
  - For GitHub PRs, run `gh pr view <num> -R <org>/<repo> --json title,headRefName,state,isDraft,body` (or use GitHub/Jira MCP tools) to get each PR's real title, branch (`headRefName`), status, Jira ticket key, and summary.
- **Step 2 — Create the task in one command** (`herdr-task` also auto-enriches GitHub PR branches/titles/statuses):
```bash
herdr-task create \
  --id "PROJ-123-slug" \
  --title "Clear Task Title from Ticket/PR" \
  --repos "service-a,service-b" \
  --link "PROJ-123" \
  --link "https://github.com/org/service-a/pull/101" \
  --link "https://github.com/org/service-b/pull/202" \
  --brief "1-2 sentence technical brief distilled from the ticket/PRs." \
  --focus
```

## Role 2: Existing Task Pilot (`HERDR_PILOT_MODE=existing_task`) & Repo Worker Agent (`HERDR_TASK_ID` is set)
When `HERDR_TASK_ID` is set (either popped via `[Ctrl-P] Task Pilot` on an existing task or running inside a repo tab):
1. **Read current task context first** (avoid redundant MCP calls): `herdr-task status`
2. **Update task title, brief, or switch registered repo worktree branches in-place**:
   `herdr-task update [--title "<new-title>"] [--brief "<new-brief>"] [--repo-branch "<repo>:<branch>"] [--sync-pr-branches]`
   *(You can also switch a single repo worktree branch with `herdr-task worktree branch --repo <repo> --branch <branch>`)*
3. **Link or update a PR, Jira ticket, Slack thread, alert, or doc on `$HERDR_TASK_ID`**:
   `herdr-task link "<url-or-key>" --label "<label>" --status "<STATUS>" --summary "<1-line summary>" [--sync-branch]`
4. **Add another repo worktree + Cockpit tab to `$HERDR_TASK_ID`**:
   `herdr-task add <repo-name> [--branch <branch>]`
5. **Record cross-repo handoff note on `$HERDR_TASK_ID`**:
   `herdr-task note "<summary or update>"`
