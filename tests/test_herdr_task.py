#!/usr/bin/env python3
"""Unit tests for herdr-task."""

import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


PLUGIN_ROOT = Path(__file__).resolve().parent.parent
BIN_PATH = PLUGIN_ROOT / "bin" / "herdr-task"

loader = importlib.machinery.SourceFileLoader("herdr_task", str(BIN_PATH))
spec = importlib.util.spec_from_loader("herdr_task", loader)
assert spec is not None and spec.loader is not None
ht = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ht)


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


if __name__ == "__main__":
    unittest.main()
