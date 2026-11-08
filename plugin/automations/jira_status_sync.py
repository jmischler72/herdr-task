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
from datetime import datetime, timezone
import json
import os
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
import urllib.request
from urllib.request import Request


AUTOMATION_ID = "jira_status_sync"
INTEGRATION_ID = "jira"
DESCRIPTION = "Bidirectional Jira ticket status & metadata sync + ticket/status picker integration via Jira REST API v3"
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
    "my_issues_jql": "assignee = currentUser() AND statusCategory != Done ORDER BY updated DESC",
    "recent_issues_jql": "statusCategory != Done ORDER BY updated DESC",
    "max_picker_results": 25,
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

MULTI_TICKET_STATUS_PRIORITY: dict[str, int] = {
    "in_progress": 0,
    "in_review": 1,
    "todo": 2,
    "done": 3,
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _resolve_base_url(
    settings: dict[str, Any],
    cfg: dict[str, Any],
    resource: dict[str, Any] | None = None,
) -> str:
    if isinstance(resource, dict):
        res_url = (resource.get("url") or "").strip()
        if "/browse/" in res_url and "your-org.atlassian.net" not in res_url:
            parsed = urlparse(res_url)
            if parsed.scheme in ("http", "https") and parsed.netloc:
                prefix = res_url.split("/browse/", 1)[0].rstrip("/")
                if prefix:
                    return prefix

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
    if modified:
        res["updated_at"] = _now_iso()

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


def _parse_issue_node(node: dict[str, Any], base_url: str) -> dict[str, Any] | None:
    key = (node.get("key") or "").strip()
    if not key:
        return None
    fields = node.get("fields") or {}
    issue_summary = (fields.get("summary") or "").strip()
    status_obj = fields.get("status") or {}
    status_name = (status_obj.get("name") or "").strip()
    status_cat = ((status_obj.get("statusCategory") or {}).get("key") or "").strip().lower()
    priority_name = ((fields.get("priority") or {}).get("name") or "").strip()
    issue_type = ((fields.get("issuetype") or {}).get("name") or "").strip()

    raw_desc = fields.get("description")
    desc_text = _extract_adf_text(raw_desc).strip() if raw_desc else ""
    desc_lines = [ln.strip() for ln in desc_text.splitlines() if ln.strip()]
    desc_snip = " ".join(desc_lines[:2])[:180]

    full_summary = issue_summary
    if desc_snip and desc_snip.lower() != issue_summary.lower():
        full_summary = f"{issue_summary} — {desc_snip}" if issue_summary else desc_snip

    browse_url = f"{base_url.rstrip('/')}/browse/{key}" if base_url else ""
    return {
        "key": key,
        "title": issue_summary,
        "status": status_name.upper() if status_name else "OPEN",
        "status_raw": status_name,
        "status_category": status_cat,
        "priority": priority_name,
        "issue_type": issue_type,
        "summary": full_summary,
        "description": desc_text[:600],
        "url": browse_url,
    }


def _search_jira_issues(
    base_url: str,
    auth_header: str,
    jql: str,
    max_results: int = 25,
    timeout: float = 5.0,
) -> tuple[list[dict[str, Any]], str]:
    encoded_jql = quote(jql, safe="")
    fields_param = "summary,status,description,issuetype,priority,updated"
    endpoints = [
        f"/rest/api/3/search/jql?jql={encoded_jql}&fields={fields_param}&maxResults={int(max_results)}",
        f"/rest/api/3/search?jql={encoded_jql}&fields={fields_param}&maxResults={int(max_results)}",
    ]
    last_err = ""
    for ep in endpoints:
        code, data, err = _jira_request("GET", base_url, ep, auth_header, timeout=timeout)
        if code == 200 and isinstance(data, dict) and isinstance(data.get("issues"), list):
            parsed: list[dict[str, Any]] = []
            for raw_issue in data.get("issues") or []:
                if isinstance(raw_issue, dict):
                    item = _parse_issue_node(raw_issue, base_url)
                    if item:
                        parsed.append(item)
            return parsed, ""
        last_err = err or f"HTTP {code}"
    return [], last_err


def provide_candidates(
    kind: str,
    context: dict[str, Any],
    settings: dict[str, Any],
) -> list[dict[str, Any]]:
    """
    Modular integration hook returning selectable Jira tickets and browser redirect actions
    for interactive task/resource forms.
    """
    if kind not in ("ticket", "jira", "resource"):
        return []

    cfg = context.get("config") or {}
    task = context.get("task") if isinstance(context.get("task"), dict) else {}
    all_tasks = context.get("all_tasks") if isinstance(context.get("all_tasks"), list) else []
    current_ref = str(context.get("current_ref") or "").strip()

    first_res = context.get("resource") if isinstance(context.get("resource"), dict) else None
    base_url = _resolve_base_url(settings, cfg, first_res)
    raw_cfg_browse = (cfg.get("jira_base_url") or "").strip().rstrip("/")
    browse_root = base_url or (raw_cfg_browse[:-len("/browse")] if raw_cfg_browse.endswith("/browse") else raw_cfg_browse)
    if "your-org.atlassian.net" in browse_root:
        browse_root = ""

    candidates: list[dict[str, Any]] = []

    # 1. Browser redirect options (so user can always jump to Jira to check/search tickets)
    if browse_root:
        if current_ref and "-" in current_ref and not current_ref.startswith("http"):
            candidates.append({
                "value": f"__open_url__:{browse_root}/browse/{current_ref}",
                "action": "open_url",
                "url": f"{browse_root}/browse/{current_ref}",
                "label": f"🌐 Open {current_ref} in Jira Browser ({browse_root}/browse/{current_ref})",
                "provider": "jira",
                "category": "ticket",
                "preview": (
                    f"🌐 Open Ticket {current_ref} in Browser\n\n"
                    f"URL: {browse_root}/browse/{current_ref}\n\n"
                    "Opens your default browser so you can inspect the ticket details, "
                    "then returns straight back to this picker."
                ),
            })
        my_work_url = f"{browse_root}/jira/your-work"
        jql_search_url = (
            f"{browse_root}/issues/?jql="
            + quote("assignee = currentUser() AND statusCategory != Done ORDER BY updated DESC", safe="")
        )
        candidates.append({
            "value": f"__open_url__:{my_work_url}",
            "action": "open_url",
            "url": my_work_url,
            "label": f"🌐 Open Jira in Browser — My Work / Assigned Tickets ({browse_root})",
            "provider": "jira",
            "category": "ticket",
            "preview": (
                f"🌐 Redirect to Jira — My Work\n\n"
                f"URL: {my_work_url}\n\n"
                "Opens your Jira 'Your Work' dashboard in the browser so you can check your tickets, "
                "and keeps this form open so you can select or paste the ticket key."
            ),
        })
        candidates.append({
            "value": f"__open_url__:{jql_search_url}",
            "action": "open_url",
            "url": jql_search_url,
            "label": "🌐 Open Jira in Browser — Search My Open Issues (JQL)",
            "provider": "jira",
            "category": "ticket",
            "preview": (
                f"🌐 Redirect to Jira Issue Search\n\n"
                f"URL: {jql_search_url}\n\n"
                "Opens Jira issue search filtered to your open assigned tickets."
            ),
        })

    # 2. Live Jira tickets assigned to currentUser() via Jira REST API v3
    seen_keys: set[str] = set()
    auth_header = _resolve_auth_header(settings)
    timeout = float(settings.get("timeout_sec") or 5.0)
    max_results = int(settings.get("max_picker_results") or 25)

    if base_url and auth_header:
        primary_jql = str(
            settings.get("my_issues_jql")
            or DEFAULT_SETTINGS["my_issues_jql"]
        )
        issues, _ = _search_jira_issues(base_url, auth_header, primary_jql, max_results=max_results, timeout=timeout)
        if not issues:
            fallback_jql = str(settings.get("recent_issues_jql") or DEFAULT_SETTINGS["recent_issues_jql"])
            issues, _ = _search_jira_issues(base_url, auth_header, fallback_jql, max_results=max_results, timeout=timeout)

        for iss in issues:
            k = iss["key"]
            seen_keys.add(k)
            st_badge = iss["status"]
            title = iss["title"]
            prio = f" • {iss['priority']}" if iss.get("priority") else ""
            preview_lines = [
                f"🎫 {k} [{st_badge}]{prio}",
                f"Title:   {title}",
                f"URL:     {iss['url']}",
            ]
            if iss.get("summary"):
                preview_lines.extend(["", "Summary:", f"  {iss['summary']}"])
            if iss.get("description"):
                preview_lines.extend(["", "Description:", f"  {iss['description']}"])

            candidates.append({
                "value": k,
                "label": f"🎫 [{st_badge}] {k} — {title}",
                "provider": "jira",
                "category": "ticket",
                "preview": "\n".join(preview_lines),
                "resource_patch": {
                    "ref": k,
                    "url": iss["url"],
                    "category": "ticket",
                    "provider": "jira",
                    "label": f"{k}: {title}" if title else k,
                    "status": st_badge,
                    "summary": iss["summary"] or title,
                    "metadata": {
                        "jira_title": title,
                        "jira_status_category": iss.get("status_category") or "",
                    },
                },
            })

    # 3. Cached Jira tickets from active/archived local tasks (works offline or without API token)
    task_sources = ([task] if task else []) + list(all_tasks)
    for t_obj in task_sources:
        if not isinstance(t_obj, dict):
            continue
        for r in t_obj.get("resources") or []:
            if not isinstance(r, dict):
                continue
            if r.get("category") == "ticket" and r.get("provider") == "jira":
                k = (r.get("ref") or "").strip()
                if not k or k in seen_keys:
                    continue
                seen_keys.add(k)
                st_badge = (r.get("status") or "TICKET").upper()
                lbl = r.get("label") or k
                url = r.get("url") or (f"{browse_root}/browse/{k}" if browse_root else k)
                summary = r.get("summary") or ""
                candidates.append({
                    "value": k,
                    "label": f"🎫 [{st_badge}] {lbl}  (recent task: {t_obj.get('id', '')})",
                    "provider": "jira",
                    "category": "ticket",
                    "preview": (
                        f"🎫 {lbl} [{st_badge}] (Cached from task '{t_obj.get('id', '')}')\n"
                        f"URL:     {url}\n"
                        f"Summary: {summary or '(none)'}"
                    ),
                    "resource_patch": {
                        "ref": k,
                        "url": url,
                        "category": "ticket",
                        "provider": "jira",
                        "label": lbl,
                        "status": r.get("status"),
                        "summary": summary or None,
                        "metadata": dict(r.get("metadata") or {}),
                    },
                })

    return candidates


def provide_field_options(
    field: str,
    context: dict[str, Any],
    settings: dict[str, Any],
) -> list[dict[str, Any]]:
    """
    Modular integration hook returning predefined values for form dropdowns
    (e.g., Jira workflow statuses for `resource.status`).
    """
    if field != "resource.status":
        return []

    res = context.get("resource") if isinstance(context.get("resource"), dict) else {}
    provider = (res.get("provider") or context.get("provider") or "").lower()
    category = (res.get("category") or context.get("category") or "").lower()
    if provider != "jira" and category != "ticket":
        return []

    cfg = context.get("config") or {}
    base_url = _resolve_base_url(settings, cfg, res)
    auth_header = _resolve_auth_header(settings)
    timeout = float(settings.get("timeout_sec") or 5.0)
    issue_key = (res.get("ref") or "").strip()

    options: list[dict[str, Any]] = []
    seen_upper: set[str] = set()

    # 1. If we have a specific Jira issue key and credentials, query live available workflow transitions
    if base_url and auth_header and issue_key and "-" in issue_key and not issue_key.startswith("http"):
        safe_key = quote(issue_key, safe="")
        code, data, _ = _jira_request(
            "GET",
            base_url,
            f"/rest/api/3/issue/{safe_key}/transitions",
            auth_header,
            timeout=timeout,
        )
        if code == 200 and isinstance(data, dict):
            for tr in data.get("transitions") or []:
                to_name = ((tr.get("to") or {}).get("name") or tr.get("name") or "").strip()
                if to_name and to_name.upper() not in seen_upper:
                    seen_upper.add(to_name.upper())
                    options.append({
                        "value": to_name.upper(),
                        "label": f"🎫 {to_name.upper()}  (live Jira transition for {issue_key})",
                        "provider": "jira",
                    })

    # 2. Predefined statuses from `status_map` configuration
    status_map = settings.get("status_map") or DEFAULT_SETTINGS["status_map"]
    primary_labels = {
        "todo": "To Do / Backlog",
        "in_progress": "In Progress / Active",
        "in_review": "In Review / QA",
        "done": "Done / Resolved",
    }
    for task_st, names in status_map.items():
        if task_st == "archived" or not isinstance(names, list):
            continue
        for idx, name in enumerate(names):
            clean = str(name).strip().upper()
            if not clean or clean in seen_upper:
                continue
            seen_upper.add(clean)
            # Keep the dropdown concise: include the primary status name for each category plus common ones
            if idx < 2:
                options.append({
                    "value": clean,
                    "label": f"🎫 {clean}  ({primary_labels.get(task_st, task_st)})",
                    "provider": "jira",
                })

    return options


def enrich_resource(
    res: dict[str, Any],
    context: dict[str, Any],
    settings: dict[str, Any],
) -> dict[str, Any] | None:
    """
    On-the-fly enrichment hook called by interactive forms when a Jira ticket is entered.
    """
    if res.get("category") != "ticket" and res.get("provider") != "jira":
        return None
    cfg = context.get("config") or {}
    task = context.get("task") if isinstance(context.get("task"), dict) else {}
    base_url = _resolve_base_url(settings, cfg, res)
    auth_header = _resolve_auth_header(settings)
    if not base_url or not auth_header:
        return None
    timeout = float(settings.get("timeout_sec") or 5.0)
    mod, msg = _enrich_ticket_resource(res, task, base_url, auth_header, timeout)
    return {"modified": mod, "message": msg}


def handle_event(
    event: str,
    context: dict[str, Any],
    settings: dict[str, Any],
) -> dict[str, Any] | None:
    task = context.get("task")
    cfg = context.get("config") or {}
    if not isinstance(task, dict):
        return None

    jira_resources = _iter_jira_resources(task)
    first_res = context.get("resource") if isinstance(context.get("resource"), dict) else (jira_resources[0] if jira_resources else None)
    default_base_url = _resolve_base_url(settings, cfg, first_res)
    auth_header = _resolve_auth_header(settings)
    if not default_base_url or not auth_header:
        if event == "task.sync" and jira_resources:
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
            base_url = _resolve_base_url(settings, cfg, res) or default_base_url
            mod, msg = _enrich_ticket_resource(res, task, base_url, auth_header, timeout)
            if mod:
                task_modified = True
            if msg:
                messages.append(msg)
        return {"task_modified": task_modified, "messages": messages}

    if event == "task.created":
        explicit_task_status = bool(context.get("explicit_task_status"))
        for res in jira_resources:
            base_url = _resolve_base_url(settings, cfg, res) or default_base_url
            mod, msg = _enrich_ticket_resource(res, task, base_url, auth_header, timeout)
            if mod:
                task_modified = True
            if msg:
                messages.append(msg)

            cur_task_status = task.get("status") or "in_progress"
            jira_st = res.get("status") or ""
            jira_cat = (res.get("metadata") or {}).get("jira_status_category") or ""
            mapped_status = _map_jira_status_to_task_status(jira_st, jira_cat, status_map)

            if (
                not explicit_task_status
                and settings.get("pull_task_status_from_jira", True)
                and mapped_status in ("in_review", "done")
                and cur_task_status == "in_progress"
            ):
                task["status"] = mapped_status
                task_modified = True
                messages.append(f"Adopted task status '{mapped_status}' from Jira {res['ref']} [{jira_st}]")
                continue

            if settings.get("sync_on_task_create", True):
                candidates_up = {c.strip().upper() for c in status_map.get(cur_task_status, [])}
                if jira_st.upper() not in candidates_up and (explicit_task_status or mapped_status in (None, "todo")):
                    ok, new_jira_st, tr_msg = _transition_jira_issue(
                        base_url, auth_header, res["ref"], cur_task_status, status_map, timeout
                    )
                    if ok and new_jira_st:
                        res["status"] = new_jira_st.upper()
                        res["updated_at"] = _now_iso()
                        task_modified = True
                        messages.append(tr_msg)
        return {"task_modified": task_modified, "messages": messages}

    if event == "task.status_changed":
        new_status = (context.get("new_status") or task.get("status") or "").strip()
        if not new_status:
            return None
        if new_status == "archived" and not settings.get("sync_on_task_cleanup", False):
            return None
        for res in jira_resources:
            base_url = _resolve_base_url(settings, cfg, res) or default_base_url
            candidates_up = {c.strip().upper() for c in status_map.get(new_status, [])}
            if (res.get("status") or "").upper() in candidates_up:
                continue
            ok, new_jira_st, tr_msg = _transition_jira_issue(
                base_url, auth_header, res["ref"], new_status, status_map, timeout
            )
            if ok and new_jira_st:
                res["status"] = new_jira_st.upper()
                res["updated_at"] = _now_iso()
                task_modified = True
            if tr_msg:
                messages.append(tr_msg)
        return {"task_modified": task_modified, "messages": messages}

    if event == "task.cleanup":
        if not settings.get("sync_on_task_cleanup", False):
            return None
        for res in jira_resources:
            base_url = _resolve_base_url(settings, cfg, res) or default_base_url
            ok, new_jira_st, tr_msg = _transition_jira_issue(
                base_url, auth_header, res["ref"], "archived", status_map, timeout
            )
            if ok and new_jira_st:
                res["status"] = new_jira_st.upper()
                res["updated_at"] = _now_iso()
                task_modified = True
            if tr_msg:
                messages.append(tr_msg)
        return {"task_modified": task_modified, "messages": messages}

    if event == "task.sync":
        pulled_statuses: list[tuple[str, str, str]] = []
        for res in jira_resources:
            base_url = _resolve_base_url(settings, cfg, res) or default_base_url
            issue_key = res["ref"]
            details, err = _fetch_issue_details(base_url, auth_header, issue_key, timeout=timeout)
            if not details:
                messages.append(f"Warning: Jira sync failed for {issue_key}: {err}")
                continue

            res_mod = False
            jira_st = details.get("status") or ""
            if jira_st and res.get("status") != jira_st.upper():
                res["status"] = jira_st.upper()
                res_mod = True
            if details.get("title") and (not res.get("label") or res.get("label") == issue_key):
                res["label"] = f"{issue_key}: {details['title']}"
                res_mod = True
            if details.get("summary") and not res.get("summary"):
                res["summary"] = details["summary"]
                res_mod = True
            if res_mod:
                res["updated_at"] = _now_iso()
                task_modified = True

            messages.append(f"Synced Jira {issue_key} -> [{res.get('status') or 'UNKNOWN'}]")

            mapped_status = _map_jira_status_to_task_status(
                jira_st,
                details.get("status_category") or "",
                status_map,
            )
            if mapped_status:
                pulled_statuses.append((mapped_status, issue_key, jira_st))

        if (
            pulled_statuses
            and settings.get("pull_task_status_from_jira", True)
            and task.get("status") != "archived"
        ):
            pulled_statuses.sort(key=lambda item: MULTI_TICKET_STATUS_PRIORITY.get(item[0], 99))
            best_status, best_key, best_jira_st = pulled_statuses[0]
            if best_status != task.get("status"):
                old_st = task.get("status")
                task["status"] = best_status
                task_modified = True
                messages.append(
                    f"Updated task status '{old_st}' -> '{best_status}' (from Jira {best_key} [{best_jira_st}])"
                )
        return {"task_modified": task_modified, "messages": messages}

    return None
