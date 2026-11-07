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

        # Cleanup & archive lookup priority
        rc = ht.main(["cleanup", "PROJ-1-test", "--delete-branches", "--no-herdr"])
        self.assertEqual(rc, 0)
        self.assertIsNone(ht.load_task(self.paths, "PROJ-1-test", include_archived=False))
        archived = ht.load_task(self.paths, "PROJ-1-test", include_archived=True)
        assert archived is not None
        self.assertEqual(archived["status"], "archived")

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


if __name__ == "__main__":
    unittest.main()
