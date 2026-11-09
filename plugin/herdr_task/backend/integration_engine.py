#!/usr/bin/env python3
"""
Unified integration engine for external tool integrations.
Handles lifecycle events (automations), UI candidate pickers, dynamic field options, and resource enrichment.
"""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
from typing import Any

from herdr_task.backend.models import TaskPaths


def discover_integrations(paths: TaskPaths, cfg: dict[str, Any]) -> list[dict[str, Any]]:
    """
    Discovers modular integrations from `integrations/*.py` (and legacy `automations/*.py`)
    that expose `handle_event`, `provide_candidates`, `provide_field_options`, or `enrich_resource`.
    """
    discovered_by_id: dict[str, dict[str, Any]] = {}
    user_auto_cfg = cfg.get("automations") if isinstance(cfg.get("automations"), dict) else {}
    user_integ_cfg = cfg.get("integrations") if isinstance(cfg.get("integrations"), dict) else {}

    search_dirs: list[Path] = []
    for d in (*paths.integration_search_dirs(), *paths.automation_search_dirs()):
        if d not in search_dirs:
            search_dirs.append(d)

    for folder in search_dirs:
        for py_file in sorted(folder.glob("*.py")):
            if py_file.name.startswith("_"):
                continue
            mod_name = f"herdr_task_integ_{py_file.stem}"
            try:
                spec = importlib.util.spec_from_file_location(mod_name, str(py_file))
                if not spec or not spec.loader:
                    continue
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
            except Exception:
                continue

            event_fn = getattr(mod, "handle_event", None)
            cand_fn = getattr(mod, "provide_candidates", None)
            opt_fn = getattr(mod, "provide_field_options", None)
            enrich_fn = getattr(mod, "enrich_resource", None)

            if not any(callable(fn) for fn in (event_fn, cand_fn, opt_fn, enrich_fn)):
                continue

            integ_id = str(
                getattr(mod, "INTEGRATION_ID", None)
                or getattr(mod, "AUTOMATION_ID", None)
                or py_file.stem
            ).strip()
            auto_id = str(getattr(mod, "AUTOMATION_ID", integ_id)).strip()
            description = str(getattr(mod, "DESCRIPTION", "")).strip()
            events = tuple(getattr(mod, "EVENTS", ()))
            default_settings = getattr(mod, "DEFAULT_SETTINGS", {})
            settings: dict[str, Any] = (
                json.loads(json.dumps(default_settings)) if isinstance(default_settings, dict) else {}
            )

            for ovr_source in (user_auto_cfg.get(auto_id), user_auto_cfg.get(integ_id), user_integ_cfg.get(integ_id)):
                if isinstance(ovr_source, bool):
                    settings["enabled"] = ovr_source
                elif isinstance(ovr_source, dict):
                    for sk, sv in ovr_source.items():
                        if sk == "status_map" and isinstance(sv, dict) and isinstance(settings.get("status_map"), dict):
                            settings["status_map"].update(sv)
                        else:
                            settings[sk] = sv

            caps: list[str] = []
            if callable(event_fn):
                caps.append("events")
            if callable(cand_fn):
                caps.append("candidates")
            if callable(opt_fn):
                caps.append("field_options")
            if callable(enrich_fn):
                caps.append("enrich_resource")

            discovered_by_id[integ_id] = {
                "id": integ_id,
                "auto_id": auto_id,
                "description": description,
                "events": list(events),
                "capabilities": caps,
                "enabled": bool(settings.get("enabled", True)),
                "path": str(py_file),
                "settings": settings,
                "module": mod,
            }

    return list(discovered_by_id.values())


def discover_automations(paths: TaskPaths, cfg: dict[str, Any]) -> list[dict[str, Any]]:
    """
    Backwards-compatible discovery of lifecycle automations.
    """
    integrations = discover_integrations(paths, cfg)
    automations: list[dict[str, Any]] = []
    for item in integrations:
        if "events" in item.get("capabilities", []):
            automations.append({
                "id": item.get("auto_id") or item["id"],
                "description": item["description"],
                "events": item["events"],
                "enabled": item["enabled"],
                "path": item["path"],
                "settings": item["settings"],
                "module": item["module"],
            })
    return automations


def dispatch_automation_event(
    event: str,
    task: dict[str, Any],
    paths: TaskPaths,
    cfg: dict[str, Any],
    **extra_context: Any,
) -> tuple[bool, list[str]]:
    integrations = discover_integrations(paths, cfg)
    any_modified = False
    messages: list[str] = []

    context: dict[str, Any] = {
        "event": event,
        "task": task,
        "paths": paths,
        "config": cfg,
        **extra_context,
    }

    for integ in integrations:
        if not integ["enabled"]:
            continue
        if integ["events"] and event not in integ["events"]:
            continue
        handle_fn = getattr(integ["module"], "handle_event", None)
        if not callable(handle_fn):
            continue
        try:
            res = handle_fn(event, context, integ["settings"])
            display_id = integ.get("auto_id") or integ["id"]
            if isinstance(res, dict):
                if res.get("task_modified"):
                    any_modified = True
                for m in res.get("messages") or []:
                    if m:
                        messages.append(f"[{display_id}] {m}")
        except Exception as e:
            display_id = integ.get("auto_id") or integ["id"]
            messages.append(f"[{display_id}] Error handling {event}: {e}")

    return any_modified, messages


# Unified alias
dispatch_task_event = dispatch_automation_event


def query_integration_candidates(
    kind: str,
    paths: TaskPaths,
    cfg: dict[str, Any],
    **extra_context: Any,
) -> list[dict[str, Any]]:
    from herdr_task.backend.storage import list_all_tasks

    integrations = discover_integrations(paths, cfg)
    all_tasks = extra_context.pop("all_tasks", None)
    if all_tasks is None:
        all_tasks = list_all_tasks(paths, include_archived=True)

    context: dict[str, Any] = {
        "kind": kind,
        "paths": paths,
        "config": cfg,
        "all_tasks": all_tasks,
        **extra_context,
    }

    out: list[dict[str, Any]] = []
    seen_values: set[str] = set()
    for integ in integrations:
        if not integ["enabled"]:
            continue
        fn = getattr(integ["module"], "provide_candidates", None)
        if not callable(fn):
            continue
        try:
            items = fn(kind, context, integ["settings"]) or []
            for item in items:
                if not isinstance(item, dict):
                    continue
                val = str(item.get("value") or "").strip()
                if not val or val in seen_values:
                    continue
                seen_values.add(val)
                item_copy = dict(item)
                item_copy.setdefault("integration_id", integ["id"])
                out.append(item_copy)
        except Exception:
            continue
    return out


def query_integration_field_options(
    field: str,
    paths: TaskPaths,
    cfg: dict[str, Any],
    **extra_context: Any,
) -> list[dict[str, Any]]:
    integrations = discover_integrations(paths, cfg)
    context: dict[str, Any] = {
        "field": field,
        "paths": paths,
        "config": cfg,
        **extra_context,
    }

    out: list[dict[str, Any]] = []
    seen_values: set[str] = set()
    for integ in integrations:
        if not integ["enabled"]:
            continue
        fn = getattr(integ["module"], "provide_field_options", None)
        if not callable(fn):
            continue
        try:
            items = fn(field, context, integ["settings"]) or []
            for item in items:
                if not isinstance(item, dict):
                    continue
                val = str(item.get("value") or "").strip()
                if not val or val in seen_values:
                    continue
                seen_values.add(val)
                item_copy = dict(item)
                item_copy.setdefault("integration_id", integ["id"])
                out.append(item_copy)
        except Exception:
            continue
    return out


def enrich_draft_resource_via_integrations(
    res: dict[str, Any],
    paths: TaskPaths,
    cfg: dict[str, Any],
    task: dict[str, Any] | None = None,
) -> list[str]:
    integrations = discover_integrations(paths, cfg)
    context: dict[str, Any] = {
        "resource": res,
        "task": task or {},
        "paths": paths,
        "config": cfg,
    }
    messages: list[str] = []
    for integ in integrations:
        if not integ["enabled"]:
            continue
        fn = getattr(integ["module"], "enrich_resource", None)
        if not callable(fn):
            continue
        try:
            ret = fn(res, context, integ["settings"])
            if isinstance(ret, dict) and ret.get("message"):
                messages.append(f"[{integ['id']}] {ret['message']}")
        except Exception:
            continue
    return messages
