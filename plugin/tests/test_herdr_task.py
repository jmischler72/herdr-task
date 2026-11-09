#!/usr/bin/env python3
"""Unit and integration tests for herdr-task and modular lifecycle automations."""

import importlib.machinery
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
from typing import Any
import unittest
from unittest.mock import patch
from urllib.request import Request


PLUGIN_ROOT = Path(__file__).resolve().parent.parent
BIN_PATH = PLUGIN_ROOT / "bin" / "herdr-task"
if not BIN_PATH.exists():
    BIN_PATH = Path(os.path.expanduser("~/.local/bin/herdr-task"))

loader = importlib.machinery.SourceFileLoader("herdr_task", str(BIN_PATH))
spec = importlib.util.spec_from_loader("herdr_task", loader)
assert spec is not None and spec.loader is not None
ht = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ht)


class _FakeHTTPResponse:
    def __init__(self, status: int, body: dict[str, Any] | None = None) -> None:
        self.status = status
        self._raw = json.dumps(body).encode("utf-8") if body is not None else b""

    def getcode(self) -> int:
        return self.status

    def read(self) -> bytes:
        return self._raw

    def __enter__(self) -> "_FakeHTTPResponse":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        pass


class _MockJiraTransport:
    """In-memory mock of Jira Cloud REST API v3 (/rest/api/3/issue/{key} and /transitions)."""

    def __init__(self) -> None:
        self.base_url = "https://mock-jira.atlassian.net"
        self.issues: dict[str, dict[str, Any]] = {
            "PROJ-123": {
                "key": "PROJ-123",
                "summary": "Migrate RabbitMQ consumers to quorum queues",
                "status_name": "To Do",
                "status_category": "new",
                "description": {
                    "type": "doc",
                    "version": 1,
                    "content": [
                        {
                            "type": "paragraph",
                            "content": [{"type": "text", "text": "Switch classic queues to quorum queues."}],
                        }
                    ],
                },
            }
        }
        self.transitions = [
            {"id": "11", "name": "To Do", "to": {"name": "To Do", "statusCategory": {"key": "new"}}},
            {"id": "21", "name": "Start Progress", "to": {"name": "In Progress", "statusCategory": {"key": "indeterminate"}}},
            {"id": "31", "name": "Submit for Review", "to": {"name": "In Review", "statusCategory": {"key": "indeterminate"}}},
            {"id": "41", "name": "Resolve Issue", "to": {"name": "Done", "statusCategory": {"key": "done"}}},
        ]
        self.transition_calls: list[tuple[str, str]] = []

    def urlopen(self, req: Request, timeout: float = 5.0) -> _FakeHTTPResponse:
        url = req.full_url
        method = req.get_method()
        auth = req.headers.get("Authorization") or req.headers.get("authorization") or ""
        if not auth.startswith("Basic "):
            raise RuntimeError("Missing Basic Authorization header")

        path = url.split("?", 1)[0]
        if method == "GET" and path.endswith("/transitions"):
            issue_key = path.strip("/").split("/")[-2]
            if issue_key not in self.issues:
                return _FakeHTTPResponse(404, {})
            return _FakeHTTPResponse(200, {"transitions": self.transitions})

        if method == "GET" and "/rest/api/3/search" in path:
            issue_nodes = [
                {
                    "key": iss["key"],
                    "fields": {
                        "summary": iss["summary"],
                        "description": iss["description"],
                        "status": {
                            "name": iss["status_name"],
                            "statusCategory": {"key": iss["status_category"]},
                        },
                    },
                }
                for iss in self.issues.values()
            ]
            return _FakeHTTPResponse(200, {"issues": issue_nodes})

        if method == "GET" and "/rest/api/3/issue/" in path:
            issue_key = path.rsplit("/", 1)[-1]
            iss = self.issues.get(issue_key)
            if not iss:
                return _FakeHTTPResponse(404, {})
            return _FakeHTTPResponse(
                200,
                {
                    "key": iss["key"],
                    "fields": {
                        "summary": iss["summary"],
                        "description": iss["description"],
                        "status": {
                            "name": iss["status_name"],
                            "statusCategory": {"key": iss["status_category"]},
                        },
                    },
                },
            )

        if method == "POST" and path.endswith("/transitions"):
            issue_key = path.strip("/").split("/")[-2]
            raw_data = req.data.decode("utf-8") if req.data else "{}"
            body = json.loads(raw_data)
            tr_id = str((body.get("transition") or {}).get("id") or "")
            matched = next((t for t in self.transitions if t["id"] == tr_id), None)
            if not matched or issue_key not in self.issues:
                return _FakeHTTPResponse(400, {})
            self.issues[issue_key]["status_name"] = matched["to"]["name"]
            self.issues[issue_key]["status_category"] = matched["to"]["statusCategory"]["key"]
            self.transition_calls.append((issue_key, tr_id))
            return _FakeHTTPResponse(204, None)

        return _FakeHTTPResponse(404, {})


class HerdrTaskTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)
        self.tasks_dir = self.tmp_path / "tasks"
        self.wt_dir = self.tmp_path / "worktrees"
        self.repos_dir = self.tmp_path / "repos"
        self.repos_dir.mkdir()

        self.repo_a = self.repos_dir / "repo-a"
        self.repo_a.mkdir()
        subprocess.run(["git", "-C", str(self.repo_a), "init", "-b", "main"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(self.repo_a), "config", "user.email", "test@example.com"], check=True)
        subprocess.run(["git", "-C", str(self.repo_a), "config", "user.name", "Test"], check=True)
        (self.repo_a / "README.md").write_text("hello\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.repo_a), "add", "."], check=True)
        subprocess.run(["git", "-C", str(self.repo_a), "commit", "-m", "init"], check=True, capture_output=True)

        os.environ["HERDR_TASKS_DIR"] = str(self.tasks_dir)
        os.environ["HERDR_WORKTREES_DIR"] = str(self.wt_dir)
        self.paths = ht.TaskPaths(self.tasks_dir)
        self.paths.ensure_dirs()
        (self.tasks_dir / "config.json").write_text(
            json.dumps({"repo_roots": [str(self.repos_dir)], "worktrees_root": str(self.wt_dir)}),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_security_validations(self) -> None:
        with self.assertRaises(ValueError):
            self.paths.task_file("../../etc/passwd")
        self.assertIsNone(ht.resolve_current_task_id(self.paths, "../../etc/passwd"))

        for bad in ("-oProxyCommand=evil", "--upload-pack=foo", "a..b", "foo.lock", "bad branch"):
            with self.assertRaises(ValueError):
                ht.validate_git_branch(bad)
        self.assertEqual(ht.validate_git_branch("feat/PROJ-123_test"), "feat/PROJ-123_test")

        self.assertFalse(ht.open_url_target("file:///etc/passwd"))
        self.assertFalse(ht.open_url_target("-a Calculator"))
        self.assertFalse(ht.open_url_target("javascript:alert(1)"))

    def test_lifecycle_and_resource_upsert(self) -> None:
        rc = ht.main([
            "create",
            "--id", "PROJ-1-test",
            "--title", "Test Task",
            "--repos", "repo-a",
            "--link", "https://notion.so/my-doc",
            "--label", "Custom Doc Label",
            "--no-herdr",
        ])
        self.assertEqual(rc, 0)
        task = ht.load_task(self.paths, "PROJ-1-test")
        assert task is not None
        self.assertEqual(task["resources"][0]["label"], "Custom Doc Label")

        # Partial update without --label preserves custom label
        rc = ht.main(["link", "https://notion.so/my-doc", "--status", "APPROVED", "--task", "PROJ-1-test", "--no-herdr"])
        self.assertEqual(rc, 0)
        task = ht.load_task(self.paths, "PROJ-1-test")
        assert task is not None
        self.assertEqual(task["resources"][0]["label"], "Custom Doc Label")
        self.assertEqual(task["resources"][0]["status"], "APPROVED")

        # Idempotent repo add vs distinct second branch worktree
        rc = ht.main(["add", "repo-a", "--task", "PROJ-1-test", "--no-herdr"])
        self.assertEqual(rc, 0)
        task = ht.load_task(self.paths, "PROJ-1-test")
        assert task is not None
        self.assertEqual(len(task["worktrees"]), 1)

        rc = ht.main(["worktree", "add", "--repo", "repo-a", "--branch", "feat/second", "--task", "PROJ-1-test", "--no-herdr"])
        self.assertEqual(rc, 0)
        task = ht.load_task(self.paths, "PROJ-1-test")
        assert task is not None
        self.assertEqual(len(task["worktrees"]), 2)
        self.assertNotEqual(task["worktrees"][0]["worktree_path"], task["worktrees"][1]["worktree_path"])

        # Cleanup & archive lookup priority + clearing stale workspace/tab IDs
        task = ht.load_task(self.paths, "PROJ-1-test")
        assert task is not None
        task["workspace_id"] = "wTest"
        task["worktrees"][0]["workspace_id"] = "wChild"
        task["worktrees"][0]["tab_id"] = "wTest:t1"
        task["worktrees"][0]["pane_id"] = "wTest:p1"
        task["worktrees"][0]["cockpit_panes"] = {"editor": "wTest:p1"}
        ht.atomic_write_json(self.paths.task_file("PROJ-1-test"), task)

        rc = ht.main(["cleanup", "PROJ-1-test", "--delete-branches", "--no-herdr"])
        self.assertEqual(rc, 0)
        self.assertIsNone(ht.load_task(self.paths, "PROJ-1-test", include_archived=False))
        archived = ht.load_task(self.paths, "PROJ-1-test", include_archived=True)
        assert archived is not None
        self.assertEqual(archived["status"], "archived")
        self.assertIsNone(archived["workspace_id"])
        self.assertIsNone(archived["worktrees"][0].get("workspace_id"))
        self.assertIsNone(archived["worktrees"][0]["tab_id"])
        self.assertIsNone(archived["worktrees"][0]["pane_id"])
        self.assertNotIn("cockpit_panes", archived["worktrees"][0])

    def test_github_shorthand_and_false_positive_jira_keys(self) -> None:
        subprocess.run(
            ["git", "-C", str(self.repo_a), "remote", "add", "origin", "git@github.com:AcmeOrg/repo-a.git"],
            check=True,
        )
        cfg = ht.load_config(self.paths)

        # 1. False-positive Jira prefixes (UTF-8, SHA-256, CVE-2026, V1-2) must be ignored
        self.assertIsNone(ht._extract_jira_key_from_text("Support UTF-8 and SHA-256 for TLS-1 / V1-2 (CVE-2026)"))
        self.assertEqual(
            ht._extract_jira_key_from_text("Fix UTF-8 encoding for PROJ-456 in worker"),
            "PROJ-456",
        )

        # 2. Both org/repo#42 and repo#42 resolve to valid https://github.com/... URLs
        res_explicit = ht.classify_resource("AcmeOrg/repo-a#42", cfg, fetch_remote=False)
        self.assertEqual(res_explicit["category"], "pull_request")
        self.assertEqual(res_explicit["provider"], "github")
        self.assertEqual(res_explicit["ref"], "repo-a#42")
        self.assertEqual(res_explicit["url"], "https://github.com/AcmeOrg/repo-a/pull/42")

        res_inferred = ht.classify_resource("repo-a#99", cfg, fetch_remote=False)
        self.assertEqual(res_inferred["category"], "pull_request")
        self.assertEqual(res_inferred["ref"], "repo-a#99")
        self.assertEqual(res_inferred["url"], "https://github.com/AcmeOrg/repo-a/pull/99")

    def test_cross_task_worktree_isolation_and_dirty_branch_switch(self) -> None:
        # Task 1 on branch feat/shared
        rc1 = ht.main([
            "create", "--id", "task-one", "--title", "Task One",
            "--repos", "repo-a", "--branch", "feat/shared", "--no-herdr",
        ])
        self.assertEqual(rc1, 0)
        t1 = ht.load_task(self.paths, "task-one")
        assert t1 is not None
        wt1_path = Path(t1["worktrees"][0]["worktree_path"])

        # Task 2 on a different branch must get its own worktree path, and if Task 1's branch is
        # requested while checked out in Task 1, Task 1's clean worktree is detached rather than hijacked
        rc2 = ht.main([
            "create", "--id", "task-two", "--title", "Task Two",
            "--repos", "repo-a", "--branch", "feat/two", "--no-herdr",
        ])
        self.assertEqual(rc2, 0)
        t2 = ht.load_task(self.paths, "task-two")
        assert t2 is not None
        wt2_path = Path(t2["worktrees"][0]["worktree_path"])
        self.assertNotEqual(wt1_path.resolve(), wt2_path.resolve())

        # Make Task 2 worktree dirty and verify `update --repo-branch` blocks without --force
        (wt2_path / "dirty.txt").write_text("uncommitted\n", encoding="utf-8")
        rc_dirty = ht.main([
            "update", "task-two", "--repo-branch", "repo-a:feat/other", "--no-herdr",
        ])
        self.assertNotEqual(rc_dirty, 0)
        t2_after = ht.load_task(self.paths, "task-two")
        assert t2_after is not None
        self.assertEqual(t2_after["worktrees"][0]["branch"], "feat/two")

        # With --force, branch switch succeeds
        rc_force = ht.main([
            "update", "task-two", "--repo-branch", "repo-a:feat/other", "--force", "--no-herdr",
        ])
        self.assertEqual(rc_force, 0)
        t2_forced = ht.load_task(self.paths, "task-two")
        assert t2_forced is not None
        self.assertEqual(t2_forced["worktrees"][0]["branch"], "feat/other")

        # Updating status to 'archived' moves task file to archive/
        rc_arch = ht.main(["update", "task-one", "--status", "archived", "--no-herdr"])
        self.assertEqual(rc_arch, 0)
        self.assertIsNone(ht.load_task(self.paths, "task-one", include_archived=False))
        self.assertIsNotNone(ht.load_task(self.paths, "task-one", include_archived=True))

    def test_jira_status_sync_automation_and_custom_hook(self) -> None:
        mock_jira = _MockJiraTransport()
        # Configure mock Jira server and a custom user drop-in automation module
        (self.tasks_dir / "config.json").write_text(
            json.dumps({
                "repo_roots": [str(self.repos_dir)],
                "worktrees_root": str(self.wt_dir),
                "jira_base_url": f"{mock_jira.base_url}/browse",
                "automations": {
                    "jira_status_sync": {
                        "enabled": True,
                        "base_url": mock_jira.base_url,
                        "email": "dev@example.com",
                        "api_token": "secret-token",
                        "sync_on_task_create": True,
                        "sync_on_task_cleanup": True,
                        "pull_task_status_from_jira": True,
                    }
                },
            }),
            encoding="utf-8",
        )

        # Also drop a custom user automation in <tasks_dir>/automations/custom_audit.py
        user_auto_dir = self.tasks_dir / "automations"
        user_auto_dir.mkdir(parents=True, exist_ok=True)
        (user_auto_dir / "custom_audit.py").write_text(
            'AUTOMATION_ID = "custom_audit"\n'
            'DESCRIPTION = "Custom test hook"\n'
            'EVENTS = ("task.status_changed",)\n'
            'DEFAULT_SETTINGS = {"enabled": True}\n'
            'def handle_event(event, context, settings):\n'
            '    t = context["task"]\n'
            '    t.setdefault("notes", []).append({"timestamp": "now", "author": "custom_audit", "text": f"status->{context.get(\'new_status\')}"})\n'
            '    return {"task_modified": True, "messages": ["Recorded audit note"]}\n',
            encoding="utf-8",
        )

        with patch("urllib.request.urlopen", side_effect=mock_jira.urlopen):
            # 1. Create task linked to PROJ-123 (starts as 'To Do' in Jira, task is 'in_progress' -> auto-enriches & transitions Jira to 'In Progress'!)
            rc = ht.main([
                "create",
                "--id", "PROJ-123-rmq",
                "--link", "PROJ-123",
                "--no-herdr",
            ])
            self.assertEqual(rc, 0)
            task = ht.load_task(self.paths, "PROJ-123-rmq")
            assert task is not None
            # Title was auto-populated from Jira issue summary!
            self.assertEqual(task["title"], "PROJ-123 Migrate RabbitMQ consumers to quorum queues")
            ticket_res = task["resources"][0]
            self.assertEqual(ticket_res["label"], "PROJ-123: Migrate RabbitMQ consumers to quorum queues")
            self.assertEqual(ticket_res["status"], "IN PROGRESS")
            self.assertIn("Switch classic queues to quorum queues.", ticket_res["summary"])
            self.assertEqual(mock_jira.issues["PROJ-123"]["status_name"], "In Progress")

            # 2. Update task status in herdr-task -> 'in_review' (pushes transition to Jira AND triggers custom_audit hook)
            rc = ht.main(["update", "PROJ-123-rmq", "--status", "in_review", "--no-herdr"])
            self.assertEqual(rc, 0)
            task = ht.load_task(self.paths, "PROJ-123-rmq")
            assert task is not None
            self.assertEqual(task["status"], "in_review")
            self.assertEqual(task["resources"][0]["status"], "IN REVIEW")
            self.assertEqual(mock_jira.issues["PROJ-123"]["status_name"], "In Review")
            self.assertEqual(len(task["notes"]), 1)
            self.assertEqual(task["notes"][0]["text"], "status->in_review")

            # 3. Simulate external status change in Jira -> 'Done' and run `herdr-task sync` (pulls Jira status into task)
            mock_jira.issues["PROJ-123"]["status_name"] = "Done"
            mock_jira.issues["PROJ-123"]["status_category"] = "done"
            rc = ht.main(["sync", "PROJ-123-rmq", "--no-herdr"])
            self.assertEqual(rc, 0)
            task = ht.load_task(self.paths, "PROJ-123-rmq")
            assert task is not None
            self.assertEqual(task["status"], "done")
            self.assertEqual(task["resources"][0]["status"], "DONE")

            # 4. Creating a task linked to an 'In Review' Jira ticket (without explicit --task-status)
            # adopts 'in_review' instead of regressing the Jira ticket back to 'In Progress'
            mock_jira.issues["PROJ-200"] = {
                "key": "PROJ-200",
                "summary": "Review-stage ticket",
                "status_name": "In Review",
                "status_category": "indeterminate",
                "description": None,
            }
            rc = ht.main(["create", "--id", "PROJ-200-rev", "--link", "PROJ-200", "--no-herdr"])
            self.assertEqual(rc, 0)
            task_rev = ht.load_task(self.paths, "PROJ-200-rev")
            assert task_rev is not None
            self.assertEqual(task_rev["status"], "in_review")
            self.assertEqual(mock_jira.issues["PROJ-200"]["status_name"], "In Review")

            # 5. Multi-ticket sync keeps 'in_progress' when one ticket is 'Done' and another is 'In Progress'
            mock_jira.issues["PROJ-201"] = {
                "key": "PROJ-201",
                "summary": "Active companion ticket",
                "status_name": "In Progress",
                "status_category": "indeterminate",
                "description": None,
            }
            mock_jira.issues["PROJ-200"]["status_name"] = "Done"
            mock_jira.issues["PROJ-200"]["status_category"] = "done"
            ht.main(["link", "PROJ-201", "--task", "PROJ-200-rev", "--no-herdr"])
            rc = ht.main(["sync", "PROJ-200-rev", "--no-herdr"])
            self.assertEqual(rc, 0)
            task_multi = ht.load_task(self.paths, "PROJ-200-rev")
            assert task_multi is not None
            self.assertEqual(task_multi["status"], "in_progress")

            # 6. Failed repo provisioning during `create` rolls back without transitioning Jira
            mock_jira.issues["PROJ-999"] = {
                "key": "PROJ-999",
                "summary": "Untouched ticket on error",
                "status_name": "To Do",
                "status_category": "new",
                "description": None,
            }
            with self.assertRaises(ValueError):
                ht.main([
                    "create", "--id", "PROJ-999-fail",
                    "--repos", "non-existent-repo-xyz",
                    "--link", "PROJ-999",
                    "--no-herdr",
                ])
            self.assertEqual(mock_jira.issues["PROJ-999"]["status_name"], "To Do")
            self.assertIsNone(ht.load_task(self.paths, "PROJ-999-fail", include_archived=True))

    def test_modular_integrations_and_interactive_form_helpers(self) -> None:
        mock_jira = _MockJiraTransport()
        (self.tasks_dir / "config.json").write_text(
            json.dumps({
                "repo_roots": [str(self.repos_dir)],
                "worktrees_root": str(self.wt_dir),
                "jira_base_url": f"{mock_jira.base_url}/browse",
                "automations": {
                    "jira_status_sync": {
                        "enabled": True,
                        "base_url": mock_jira.base_url,
                        "email": "dev@example.com",
                        "api_token": "secret-token",
                    }
                },
            }),
            encoding="utf-8",
        )
        cfg = ht.load_config(self.paths)

        # 1. Discover integrations
        integs = ht.discover_integrations(self.paths, cfg)
        integ_ids = {i["id"] for i in integs}
        self.assertIn("jira", integ_ids)
        self.assertIn("github_git", integ_ids)

        with patch("urllib.request.urlopen", side_effect=mock_jira.urlopen):
            # 2. Query ticket candidates (returns browser shortcuts + live Jira tickets)
            ticket_cands = ht.query_integration_candidates("ticket", self.paths, cfg)
            c_values = [c["value"] for c in ticket_cands]
            self.assertTrue(any(v.startswith("__open_url__:") for v in c_values))
            self.assertIn("PROJ-123", c_values)
            proj_cand = next(c for c in ticket_cands if c["value"] == "PROJ-123")
            self.assertEqual(proj_cand["category"], "ticket")
            self.assertEqual(proj_cand["provider"], "jira")
            self.assertIn("Migrate RabbitMQ", proj_cand["label"])

            # 3. Query resource status options (Jira transitions + presets vs PR presets)
            jira_status_opts = ht.query_integration_field_options(
                "resource.status",
                self.paths,
                cfg,
                resource={"category": "ticket", "provider": "jira", "ref": "PROJ-123"},
            )
            jira_opt_vals = {o["value"] for o in jira_status_opts}
            self.assertIn("TO DO", jira_opt_vals)
            self.assertIn("IN PROGRESS", jira_opt_vals)
            self.assertIn("IN REVIEW", jira_opt_vals)

            pr_status_opts = ht.query_integration_field_options(
                "resource.status",
                self.paths,
                cfg,
                resource={"category": "pull_request", "provider": "github"},
            )
            pr_opt_vals = {o["value"] for o in pr_status_opts}
            self.assertIn("OPEN", pr_opt_vals)
            self.assertIn("DRAFT", pr_opt_vals)
            self.assertIn("APPROVED", pr_opt_vals)
            self.assertIn("MERGED", pr_opt_vals)

            # 4. Query branch options for repo-a
            branch_opts = ht.query_integration_field_options(
                "worktree.branch",
                self.paths,
                cfg,
                repo="repo-a",
                task_id="MY-TASK-1",
            )
            br_vals = {b["value"] for b in branch_opts}
            self.assertIn("main", br_vals)
            self.assertIn("feat/MY-TASK-1", br_vals)

            # 5. Apply resource target to draft and verify auto-enrichment
            draft_res: dict[str, Any] = {}
            ht.apply_resource_target_to_draft(draft_res, "PROJ-123", cfg, self.paths)
            self.assertEqual(draft_res["category"], "ticket")
            self.assertEqual(draft_res["provider"], "jira")
            self.assertEqual(draft_res["ref"], "PROJ-123")
            self.assertEqual(draft_res["status"], "TO DO")
            self.assertIn("Migrate RabbitMQ", draft_res["label"])

            # 6. Form preview rendering (both resource and task)
            state_res = {
                "form_type": "resource",
                "mode": "add",
                "task_id": "test-task",
                "draft": draft_res,
                "original": {},
            }
            res_preview = ht.format_form_preview(state_res, "target", color=False)
            self.assertIn("Selected Field", res_preview)
            self.assertIn("PROJ-123", res_preview)

            state_task = {
                "form_type": "task",
                "mode": "create",
                "task_id": "PROJ-123-rmq",
                "draft": {
                    "id": "PROJ-123-rmq",
                    "title": "Migrate RabbitMQ",
                    "status": "in_progress",
                    "jira_ticket": "PROJ-123",
                    "repos": ["repo-a"],
                    "default_branch": "feat/rmq",
                    "repo_branches": {"repo-a": "feat/rmq-custom"},
                    "brief": "Work on queue migration",
                },
                "original": {},
            }
            task_preview = ht.format_form_preview(state_task, "status", color=False)
            self.assertIn("Migrate RabbitMQ", task_preview)
            self.assertIn("feat/rmq-custom", task_preview)

            # 7. Non-linear interactive task form submission (Create Mode)
            draft_create = dict(state_task["draft"])
            rc_sub_create = ht._submit_interactive_task_form(
                draft_create,
                original={},
                mode="create",
                task_id=None,
                paths=self.paths,
                cfg=cfg,
                no_herdr=True,
            )
            self.assertEqual(rc_sub_create, 0)
            created = ht.load_task(self.paths, "PROJ-123-rmq")
            assert created is not None
            self.assertEqual(created["title"], "Migrate RabbitMQ")
            self.assertEqual(created["worktrees"][0]["repo"], "repo-a")
            self.assertEqual(created["worktrees"][0]["branch"], "feat/rmq-custom")
            self.assertEqual(created["resources"][0]["ref"], "PROJ-123")

            # 8. Interactive task form submission (Edit Mode - modify status, title, brief, repo branch)
            draft_edit = {
                "id": "PROJ-123-rmq",
                "title": "Migrate RabbitMQ to Quorum Queues",
                "status": "in_review",
                "jira_ticket": "PROJ-123",
                "repos": ["repo-a"],
                "default_branch": "feat/rmq",
                "repo_branches": {"repo-a": "feat/rmq-v2"},
                "brief": "Updated brief",
            }
            rc_sub_edit = ht._submit_interactive_task_form(
                draft_edit,
                original=draft_create,
                mode="edit",
                task_id="PROJ-123-rmq",
                paths=self.paths,
                cfg=cfg,
                no_herdr=True,
            )
            self.assertEqual(rc_sub_edit, 0)
            edited = ht.load_task(self.paths, "PROJ-123-rmq")
            assert edited is not None
            self.assertEqual(edited["title"], "Migrate RabbitMQ to Quorum Queues")
            self.assertEqual(edited["status"], "in_review")
            self.assertEqual(edited["brief"], "Updated brief")
            self.assertEqual(edited["worktrees"][0]["branch"], "feat/rmq-v2")

    def test_single_workspace_multi_tab_provisioning_and_focus(self) -> None:
        commands_run: list[list[str]] = []

        def fake_run(self_herdr: Any, args: list[str], timeout: float = 15.0) -> tuple[int, dict[str, Any], str]:
            commands_run.append(list(args))
            if len(args) >= 2 and args[0] == "workspace" and args[1] == "create":
                return 0, {"result": {"workspace": {"workspace_id": "wTask"}, "tab": {"tab_id": "wTask:t1"}, "root_pane": {"pane_id": "wTask:p1"}}}, ""
            if len(args) >= 2 and args[0] == "tab" and args[1] == "create":
                return 0, {"result": {"tab": {"tab_id": "wTask:t2"}, "root_pane": {"pane_id": "wTask:p5"}}}, ""
            if len(args) >= 2 and args[0] == "workspace" and args[1] == "list":
                return 0, {"result": {"workspaces": [{"workspace_id": "wTask"}]}}, ""
            if len(args) >= 2 and args[0] == "tab" and args[1] == "list":
                return 0, {"result": {"tabs": [{"tab_id": "wTask:t1"}, {"tab_id": "wTask:t2"}]}}, ""
            if len(args) >= 2 and args[0] in ("workspace", "tab", "pane"):
                return 0, {"result": {}}, ""
            return 0, {"result": {}}, ""

        with patch.object(ht.HerdrClient, "run", new=fake_run):
            # 1. Create task with Herdr workspace provisioning
            rc = ht.main([
                "create",
                "--id", "PROJ-TAB-test",
                "--title", "Tab Test",
                "--repos", "repo-a",
            ])
            self.assertEqual(rc, 0)
            task = ht.load_task(self.paths, "PROJ-TAB-test")
            assert task is not None
            self.assertEqual(task["workspace_id"], "wTask")
            self.assertEqual(len(task["worktrees"]), 1)
            self.assertEqual(task["worktrees"][0]["tab_id"], "wTask:t1")
            self.assertEqual(task["worktrees"][0]["pane_id"], "wTask:p1")

            # 2. Test focus on specific repo focuses that tab
            commands_run.clear()
            rc_foc = ht.main(["focus", "PROJ-TAB-test", "--repo", "repo-a"])
            self.assertEqual(rc_foc, 0)
            self.assertIn(["tab", "focus", "wTask:t1"], commands_run)

            # 3. Test cleanup closes single workspace
            commands_run.clear()
            rc_cl = ht.main(["cleanup", "PROJ-TAB-test", "--force"])
            self.assertEqual(rc_cl, 0)
            self.assertIn(["workspace", "close", "wTask"], commands_run)


if __name__ == "__main__":
    unittest.main()
