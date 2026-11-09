"""herdr_task - Modular Task & Worktree Orchestrator for Herdr."""

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
    open_url_target,
)
from herdr_task.backend.herdr_client import HerdrClient
from herdr_task.backend.integration_engine import (
    discover_integrations,
    dispatch_task_event,
    query_integration_candidates,
    query_integration_field_options,
)
from herdr_task.backend.models import (
    IGNORED_JIRA_PREFIXES,
    TaskPaths,
    VALID_TASK_STATUSES,
    _extract_jira_key_from_text,
    load_config,
    validate_git_branch,
)
from herdr_task.backend.storage import (
    atomic_write_json,
    classify_resource,
    load_task,
    resolve_current_task_id,
    save_task,
)
from herdr_task.backend.worktree import provision_worktree, remove_worktree, switch_worktree_branch
from herdr_task.cli import build_parser, main
from herdr_task.ui.forms import (
    apply_resource_target_to_draft,
    format_form_preview,
    interactive_resource_form,
    interactive_task_form,
    _submit_interactive_task_form,
)
from herdr_task.ui.mission_control import cmd_popup, cmd_popup_action, manage_task_resources

__all__ = [
    "TaskPaths",
    "load_config",
    "validate_git_branch",
    "IGNORED_JIRA_PREFIXES",
    "_extract_jira_key_from_text",
    "atomic_write_json",
    "load_task",
    "save_task",
    "classify_resource",
    "resolve_current_task_id",
    "provision_worktree",
    "remove_worktree",
    "switch_worktree_branch",
    "HerdrClient",
    "discover_integrations",
    "dispatch_task_event",
    "query_integration_candidates",
    "query_integration_field_options",
    "open_url_target",
    "build_parser",
    "main",
    "interactive_task_form",
    "interactive_resource_form",
    "_submit_interactive_task_form",
    "format_form_preview",
    "apply_resource_target_to_draft",
    "cmd_popup",
    "cmd_popup_action",
    "manage_task_resources",
]
