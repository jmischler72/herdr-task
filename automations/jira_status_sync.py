#!/usr/bin/env python3
"""
jira_status_sync — Modular lifecycle automation for herdr-task.

Synchronizes task status and ticket metadata bidirectionally between herdr-task
and Jira Cloud via Jira REST API v3 (zero external dependencies, zero AI tokens).

Events handled:
  - resource.linked:      Enriches linked Jira ticket with real summary, status, and description.
  - task.created:         Enriches linked Jira tickets and optionally transitions them to match task status.
  - task.status_changed:  Transitions linked Jira tickets in Jira when herdr-task status changes.
  - task.sync:            Pulls latest Jira ticket status/summary and updates herdr-task status.
  - task.cleanup:         Optionally transitions linked Jira tickets when a task is archived.
"""

from __future__ import annotations

import base64
import json
import os
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
import urllib.request
from urllib.request import Request


AUTOMATION_ID = "jira_status_sync"
DESCRIPTION = "Bidirectional Jira ticket status & metadata sync via Jira REST API v3"
EVENTS = (
    "resource.linked",
    "task.created",
    "task.status_changed",
    "task.sync",
    "task.cleanup",
)

DEFAULT_SETTINGS: dict[str, Any] = {
    "enabled": True,
    "base_url": "",
    "email": "",
    "api_token": "",
    "email_env": "JIRA_EMAIL",
    "api_token_env": "JIRA_API_TOKEN",
    "sync_on_task_create": True,
    "sync_on_task_cleanup": False,
    "pull_task_status_from_jira": True,
    "timeout_sec": 5.0,
    "status_map": {
        "todo": ["To Do", "Backlog", "Open", "Selected for Development", "Todo"],
        "in_progress": ["In Progress", "In Development", "Doing", "Active", "WIP"],
        "in_review": ["In Review", "Review", "Code Review", "PR Review", "Testing", "QA", "In QA"],
        "done": ["Done", "Closed", "Resolved", "Complete", "Completed"],
        "archived": ["Done", "Closed", "Resolved"],
    },
}

CATEGORY_FALLBACK_TO_TASK_STATUS: dict[str, str] = {
    "new": "todo",
    "indeterminate": "in_progress",
    "done": "done",
}


def _resolve_base_url(settings: dict[str, Any], cfg: dict[str, Any]) -> str:
    raw = (settings.get("base_url") or cfg.get("jira_base_url") or "").strip().rstrip("/")
    if raw.endswith("/browse"):
        raw = raw[: -len("/browse")].rstrip("/")
    if not raw or "your-org.atlassian.net" in raw:
        return ""
    return raw


def _resolve_auth_header(settings: dict[str, Any]) -> str | None:
    bearer = (
        settings.get("bearer_token")
        or os.environ.get("JIRA_BEARER_TOKEN")
        or ""
    ).strip()
    if bearer:
        return f"Bearer {bearer}"

    email_env_key = str(settings.get("email_env") or "JIRA_EMAIL")
    token_env_key = str(settings.get("api_token_env") or "JIRA_API_TOKEN")

    email = (
        settings.get("email")
        or os.environ.get(email_env_key)
        or os.environ.get("JIRA_EMAIL")
        or os.environ.get("ATLASSIAN_EMAIL")
        or ""
    ).strip()
    token = (
        settings.get("api_token")
        or os.environ.get(token_env_key)
        or os.environ.get("JIRA_API_TOKEN")
        or os.environ.get("ATLASSIAN_API_TOKEN")
        or ""
    ).strip()

    if email and token:
        raw_pair = f"{email}:{token}".encode("utf-8")
        return f"Basic {base64.b64encode(raw_pair).decode('ascii')}"
    return None


def _jira_request(
    method: str,
    base_url: str,
    path: str,
    auth_header: str,
    payload: dict[str, Any] | None = None,
    timeout: float = 5.0,
) -> tuple[int, dict[str, Any] | None, str]:
    url = f"{base_url.rstrip('/')}/{path.lstrip('/')}"
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return 0, None, f"Unsupported scheme in Jira URL: {url}"

    headers = {
        "Accept": "application/json",
        "Authorization": auth_header,
    }
    body_bytes: bytes | None = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        body_bytes = json.dumps(payload).encode("utf-8")

    req = Request(url, data=body_bytes, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status_code = resp.getcode() or 200
            raw = resp.read().decode("utf-8", errors="replace").strip()
            data = json.loads(raw) if raw else {}
            return status_code, data if isinstance(data, dict) else {}, ""
    except HTTPError as e:
        err_body = ""
        try:
            err_body = e.read().decode("utf-8", errors="replace").strip()
        except Exception:
            pass
        return e.code, None, err_body or str(e)
    except URLError as e:
        return 0, None, str(e.reason)
    except Exception as e:
        return 0, None, str(e)


def _extract_adf_text(node: Any) -> str:
    """Extracts plain text from Atlassian Document Format (ADF) JSON."""
    if isinstance(node, str):
        return node
    if not isinstance(node, dict):
        return ""
    if node.get("type") == "text":
        return str(node.get("text") or "")
    parts: list[str] = []
    for child in node.get("content") or []:
        t = _extract_adf_text(child)
        if t:
            parts.append(t)
    sep = "\n" if node.get("type") in ("paragraph", "heading", "listItem", "doc") else " "
    return sep.join(parts)


def _fetch_issue_details(
    base_url: str,
    auth_header: str,
    issue_key: str,
    timeout: float = 5.0,
) -> tuple[dict[str, Any] | None, str]:
    safe_key = quote(issue_key.strip(), safe="")
    code, data, err = _jira_request(
        "GET",
        base_url,
        f"/rest/api/3/issue/{safe_key}?fields=summary,status,description,issuetype,priority",
        auth_header,
        timeout=timeout,
    )
    if code != 200 or not data:
        return None, err or f"HTTP {code}"

    fields = data.get("fields") or {}
    issue_summary = (fields.get("summary") or "").strip()
    status_obj = fields.get("status") or {}
    status_name = (status_obj.get("name") or "").strip()
    status_cat = ((status_obj.get("statusCategory") or {}).get("key") or "").strip().lower()

    raw_desc = fields.get("description")
    desc_text = _extract_adf_text(raw_desc).strip() if raw_desc else ""
    desc_lines = [ln.strip() for ln in desc_text.splitlines() if ln.strip()]
    desc_snip = " ".join(desc_lines[:2])[:180]

    full_summary = issue_summary
    if desc_snip and desc_snip.lower() != issue_summary.lower():
        full_summary = f"{issue_summary} — {desc_snip}" if issue_summary else desc_snip

    return {
        "key": data.get("key") or issue_key,
        "title": issue_summary,
        "status": status_name,
        "status_category": status_cat,
        "summary": full_summary,
    }, ""


def _enrich_ticket_resource(
    res: dict[str, Any],
    task: dict[str, Any],
    base_url: str,
    auth_header: str,
    timeout: float,
) -> tuple[bool, str | None]:
    issue_key = (res.get("ref") or "").strip()
    if not issue_key:
        return False, None

    details, err = _fetch_issue_details(base_url, auth_header, issue_key, timeout=timeout)
    if not details:
        return False, f"Jira fetch failed for {issue_key}: {err}"

    modified = False
    if details.get("status"):
        badge = details["status"].upper()
        if res.get("status") != badge:
            res["status"] = badge
            modified = True

    if details.get("title"):
        rich_label = f"{issue_key}: {details['title']}"
        if not res.get("label") or res.get("label") == issue_key:
            res["label"] = rich_label
            modified = True
        if task.get("title") in (None, "", "Untitled Task"):
            task["title"] = f"{issue_key} {details['title']}"
            modified = True

    if details.get("summary") and not res.get("summary"):
        res["summary"] = details["summary"]
        modified = True

    meta = res.setdefault("metadata", {})
    if details.get("status_category") and meta.get("jira_status_category") != details["status_category"]:
        meta["jira_status_category"] = details["status_category"]
        modified = True
    if details.get("title") and meta.get("jira_title") != details["title"]:
        meta["jira_title"] = details["title"]
        modified = True

    return modified, f"Synced Jira {issue_key} [{res.get('status') or 'UNKNOWN'}]"


def _map_jira_status_to_task_status(
    jira_status_name: str,
    jira_status_category: str,
    status_map: dict[str, list[str]],
) -> str | None:
    norm = jira_status_name.strip().lower()
    if norm:
        for task_st, jira_names in status_map.items():
            if task_st == "archived":
                continue
            for cand in jira_names:
                if cand.strip().lower() == norm:
                    return task_st
    if jira_status_category in CATEGORY_FALLBACK_TO_TASK_STATUS:
        return CATEGORY_FALLBACK_TO_TASK_STATUS[jira_status_category]
    return None


def _transition_jira_issue(
    base_url: str,
    auth_header: str,
    issue_key: str,
    target_task_status: str,
    status_map: dict[str, list[str]],
    timeout: float = 5.0,
) -> tuple[bool, str | None, str]:
    """
    Transitions `issue_key` to a Jira workflow state matching `target_task_status`.
    Returns `(changed, new_jira_status_name, message)`.
    """
    candidates = [c.strip().lower() for c in status_map.get(target_task_status, []) if c.strip()]
    if not candidates:
        return False, None, f"No Jira status mapping configured for '{target_task_status}'"

    safe_key = quote(issue_key.strip(), safe="")
    code, data, err = _jira_request(
        "GET",
        base_url,
        f"/rest/api/3/issue/{safe_key}/transitions",
        auth_header,
        timeout=timeout,
    )
    if code != 200 or not data:
        return False, None, f"Could not fetch transitions for {issue_key}: {err or f'HTTP {code}'}"

    transitions = data.get("transitions") or []
    if not transitions:
        return False, None, f"No workflow transitions available for {issue_key}"

    chosen: dict[str, Any] | None = None
    # 1. Exact case-insensitive match against target status name (`to.name`) or transition `name`
    for cand in candidates:
        for tr in transitions:
            to_name = ((tr.get("to") or {}).get("name") or "").strip().lower()
            tr_name = (tr.get("name") or "").strip().lower()
            if to_name == cand or tr_name == cand:
                chosen = tr
                break
        if chosen:
            break

    # 2. Substring match fallback
    if not chosen:
        for cand in candidates:
            for tr in transitions:
                to_name = ((tr.get("to") or {}).get("name") or "").strip().lower()
                tr_name = (tr.get("name") or "").strip().lower()
                if cand in to_name or cand in tr_name:
                    chosen = tr
                    break
            if chosen:
                break

    if not chosen or not chosen.get("id"):
        avail = ", ".join((t.get("to") or {}).get("name") or t.get("name") or "?" for t in transitions)
        return (
            False,
            None,
            f"No matching Jira transition on {issue_key} for '{target_task_status}' (available: {avail})",
        )

    target_jira_name = ((chosen.get("to") or {}).get("name") or chosen.get("name") or "").strip()
    post_code, _, post_err = _jira_request(
        "POST",
        base_url,
        f"/rest/api/3/issue/{safe_key}/transitions",
        auth_header,
        payload={"transition": {"id": str(chosen["id"])}},
        timeout=timeout,
    )
    if post_code not in (200, 204):
        return False, None, f"Failed to transition {issue_key} -> '{target_jira_name}': {post_err or f'HTTP {post_code}'}"

    return True, target_jira_name, f"Transitioned Jira {issue_key} -> '{target_jira_name}'"


def _iter_jira_resources(task: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        r for r in task.get("resources", [])
        if r.get("category") == "ticket" and r.get("provider") == "jira" and r.get("ref")
    ]


def handle_event(
    event: str,
    context: dict[str, Any],
    settings: dict[str, Any],
) -> dict[str, Any] | None:
    task = context.get("task")
    cfg = context.get("config") or {}
    if not isinstance(task, dict):
        return None

    base_url = _resolve_base_url(settings, cfg)
    auth_header = _resolve_auth_header(settings)
    if not base_url or not auth_header:
        if event == "task.sync" and _iter_jira_resources(task):
            return {
                "task_modified": False,
                "messages": [
                    "Jira sync skipped: configure JIRA_EMAIL and JIRA_API_TOKEN (or automations.jira_status_sync in config.json)"
                ],
            }
        return None

    timeout = float(settings.get("timeout_sec") or 5.0)
    status_map = settings.get("status_map") or DEFAULT_SETTINGS["status_map"]
    messages: list[str] = []
    task_modified = False

    if event == "resource.linked":
        res = context.get("resource")
        if isinstance(res, dict) and res.get("category") == "ticket" and res.get("provider") == "jira":
            mod, msg = _enrich_ticket_resource(res, task, base_url, auth_header, timeout)
            if mod:
                task_modified = True
            if msg:
                messages.append(msg)
        return {"task_modified": task_modified, "messages": messages}

    if event == "task.created":
        for res in _iter_jira_resources(task):
            mod, msg = _enrich_ticket_resource(res, task, base_url, auth_header, timeout)
            if mod:
                task_modified = True
            if msg:
                messages.append(msg)
            if settings.get("sync_on_task_create", True):
                cur_task_status = task.get("status") or "in_progress"
                candidates_up = {c.strip().upper() for c in status_map.get(cur_task_status, [])}
                if (res.get("status") or "").upper() not in candidates_up:
                    ok, new_jira_st, tr_msg = _transition_jira_issue(
                        base_url, auth_header, res["ref"], cur_task_status, status_map, timeout
                    )
                    if ok and new_jira_st:
                        res["status"] = new_jira_st.upper()
                        task_modified = True
                        messages.append(tr_msg)
        return {"task_modified": task_modified, "messages": messages}

    if event == "task.status_changed":
        new_status = (context.get("new_status") or task.get("status") or "").strip()
        if not new_status:
            return None
        if new_status == "archived" and not settings.get("sync_on_task_cleanup", False):
            return None
        for res in _iter_jira_resources(task):
            candidates_up = {c.strip().upper() for c in status_map.get(new_status, [])}
            if (res.get("status") or "").upper() in candidates_up:
                continue
            ok, new_jira_st, tr_msg = _transition_jira_issue(
                base_url, auth_header, res["ref"], new_status, status_map, timeout
            )
            if ok and new_jira_st:
                res["status"] = new_jira_st.upper()
                task_modified = True
            if tr_msg:
                messages.append(tr_msg)
        return {"task_modified": task_modified, "messages": messages}

    if event == "task.cleanup":
        if not settings.get("sync_on_task_cleanup", False):
            return None
        for res in _iter_jira_resources(task):
            ok, new_jira_st, tr_msg = _transition_jira_issue(
                base_url, auth_header, res["ref"], "archived", status_map, timeout
            )
            if ok and new_jira_st:
                res["status"] = new_jira_st.upper()
                task_modified = True
            if tr_msg:
                messages.append(tr_msg)
        return {"task_modified": task_modified, "messages": messages}

    if event == "task.sync":
        for res in _iter_jira_resources(task):
            issue_key = res["ref"]
            details, err = _fetch_issue_details(base_url, auth_header, issue_key, timeout=timeout)
            if not details:
                messages.append(f"Warning: Jira sync failed for {issue_key}: {err}")
                continue

            jira_st = details.get("status") or ""
            if jira_st and res.get("status") != jira_st.upper():
                res["status"] = jira_st.upper()
                task_modified = True
            if details.get("title") and (not res.get("label") or res.get("label") == issue_key):
                res["label"] = f"{issue_key}: {details['title']}"
                task_modified = True
            if details.get("summary") and not res.get("summary"):
                res["summary"] = details["summary"]
                task_modified = True

            messages.append(f"Synced Jira {issue_key} -> [{res.get('status') or 'UNKNOWN'}]")

            if settings.get("pull_task_status_from_jira", True) and task.get("status") != "archived":
                mapped_status = _map_jira_status_to_task_status(
                    jira_st,
                    details.get("status_category") or "",
                    status_map,
                )
                if mapped_status and mapped_status != task.get("status"):
                    old_st = task.get("status")
                    task["status"] = mapped_status
                    task_modified = True
                    messages.append(
                        f"Updated task status '{old_st}' -> '{mapped_status}' (from Jira {issue_key} [{jira_st}])"
                    )
        return {"task_modified": task_modified, "messages": messages}

    return None
