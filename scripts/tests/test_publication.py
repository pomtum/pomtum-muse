"""Publication guards tested with isolated Git repositories and synthetic secrets.

Run with python -m unittest discover -s scripts/tests -p test_publication.py.
No production repository, credential, or network remote is used.
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from unittest import mock


SOURCE = Path(__file__).resolve().parents[1]
GITLEAKS = shutil.which("gitleaks")


def synthetic_token():
    # Construct at runtime: never embed a complete credential-shaped literal.
    return "mgst_" + secrets.token_urlsafe(32)


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="publication-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.repo = self.root / "source"
        self.repo.mkdir()
        self.env = os.environ.copy()
        for key in list(self.env):
            if key.startswith("GIT_CONFIG_") or key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
                self.env.pop(key)
        self.env.update({
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0", "PYTHONDONTWRITEBYTECODE": "1",
            "GIT_AUTHOR_NAME": "Publication test", "GIT_COMMITTER_NAME": "Publication test",
            "GIT_AUTHOR_EMAIL": "publication@example.invalid",
            "GIT_COMMITTER_EMAIL": "publication@example.invalid",
        })
        self.env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + self.env.get("PATH", "")
        self.git("init", "-b", "main")
        self.git("config", "user.name", "Publication test")
        self.git("config", "user.email", "publication@example.invalid")
        self.git("config", "commit.gpgsign", "false")
        (self.repo / "scripts").mkdir()
        shutil.copy2(SOURCE / "check-publication.py", self.repo / "scripts/check-publication.py")
        shutil.copy2(SOURCE.parent / ".gitleaks.toml", self.repo / ".gitleaks.toml")
        shutil.copytree(SOURCE.parent / ".githooks", self.repo / ".githooks")
        for hook in (self.repo / ".githooks").iterdir():
            hook.chmod(0o755)
        (self.repo / "source.txt").write_text("public source\n", encoding="utf-8")
        self.commit("public baseline")

    def command(self, *arguments, cwd=None, input=None):
        return subprocess.run(arguments, cwd=cwd or self.repo, env=self.env,
                              input=input, text=True, capture_output=True, timeout=30)

    def git(self, *arguments, cwd=None):
        result = self.command("git", *arguments, cwd=cwd)
        self.assertEqual(result.returncode, 0, "isolated Git fixture operation failed")
        return result.stdout.strip()

    def commit(self, message):
        self.git("add", "--all")
        self.git("-c", "core.hooksPath=", "commit", "-m", message)
        return self.git("rev-parse", "HEAD")

    def scan(self, *arguments, repo=None, input=None):
        return self.command(sys.executable, "-B", "scripts/check-publication.py", *arguments,
                            cwd=repo, input=input)

    def assert_private(self, result, secret):
        self.assertFalse(secret in result.stdout + result.stderr,
                         "publication guard disclosed a synthetic secret")

    def add_deleted_history_secret(self):
        secret = synthetic_token()
        path = self.repo / "retired.txt"
        path.write_text("token=" + secret + "\n", encoding="utf-8")
        self.commit("synthetic historical credential")
        path.unlink()
        self.commit("remove credential from current tree")
        return secret

    def test_indexed_secret_is_rejected_even_after_worktree_is_sanitized(self):
        secret = synthetic_token()
        path = self.repo / "source.txt"
        path.write_text("token=" + secret + "\n", encoding="utf-8")
        self.git("add", "source.txt")
        path.write_text("public source\n", encoding="utf-8")
        result = self.scan()
        self.assert_private(result, secret)
        self.assertNotEqual(result.returncode, 0, "the actual staged object must be inspected")
        self.assertIn("source.txt:1: SDK token", result.stdout)

    def test_unstaged_content_does_not_replace_the_index_being_published(self):
        secret = synthetic_token()
        (self.repo / "source.txt").write_text("token=" + secret + "\n", encoding="utf-8")
        result = self.scan()
        self.assert_private(result, secret)
        self.assertEqual(result.returncode, 0, "clean staged bytes should remain publishable")

    @unittest.skipUnless(GITLEAKS, "Gitleaks is required for history integration")
    def test_history_automatically_runs_gitleaks_and_rejects_deleted_credentials(self):
        secret = self.add_deleted_history_secret()
        current = self.scan()
        self.assertEqual(current.returncode, 0, "the current tree fixture must be clean")
        result = self.scan("--history")
        self.assert_private(result, secret)
        self.assertNotEqual(result.returncode, 0, "deleted historical credentials must block publication")
        self.assertIn("Gitleaks/muse-sdk-token", result.stdout)

    @unittest.skipUnless(GITLEAKS, "Gitleaks is required for history integration")
    def test_shallow_clone_fails_closed_instead_of_claiming_complete_history(self):
        clone = self.root / "shallow"
        self.git("clone", "--depth=1", self.repo.as_uri(), str(clone), cwd=self.root)
        self.assertEqual(self.git("rev-parse", "--is-shallow-repository", cwd=clone), "true")
        result = self.scan("--history", repo=clone)
        self.assertNotEqual(result.returncode, 0, "a shallow history must not pass")
        self.assertIn("complete clone", result.stdout + result.stderr)

    @unittest.skipUnless(GITLEAKS, "Gitleaks is required for pre-push integration")
    def test_pre_push_stdin_covers_commit_reachable_only_by_the_pushed_sha(self):
        self.git("checkout", "-b", "temporary-branch")
        secret = synthetic_token()
        (self.repo / "pending.txt").write_text("token=" + secret + "\n", encoding="utf-8")
        dangling = self.commit("synthetic pending credential")
        self.git("checkout", "main")
        self.git("branch", "-D", "temporary-branch")
        self.assertNotIn(dangling, self.git("rev-list", "--all"))
        clean = self.scan("--history")
        self.assertEqual(clean.returncode, 0, "the dangerous SHA must not already be reachable")
        update = "refs/heads/pending " + dangling + " refs/heads/pending " + "0" * 40 + "\n"
        result = self.scan("--pre-push", input=update)
        self.assert_private(result, secret)
        self.assertNotEqual(result.returncode, 0, "pre-push must inspect revisions from stdin")
        self.assertIn("Gitleaks/muse-sdk-token", result.stdout)

    @unittest.skipUnless(GITLEAKS, "Gitleaks is required for the real local push hook")
    def test_real_pre_push_hook_blocks_local_bare_remote_without_creating_branch(self):
        secret = self.add_deleted_history_secret()
        remote = self.root / "remote.git"
        self.git("init", "--bare", str(remote), cwd=self.root)
        self.git("remote", "add", "test-only", str(remote))
        self.git("config", "core.hooksPath", ".githooks")
        result = self.command("git", "push", "test-only", "HEAD:refs/heads/publish")
        self.assert_private(result, secret)
        self.assertNotEqual(result.returncode, 0, "the installed hook must stop an actual push")
        self.assertIn("Gitleaks/muse-sdk-token", result.stdout + result.stderr)
        branch = self.command("git", "--git-dir", str(remote), "show-ref", "--verify", "refs/heads/publish")
        self.assertNotEqual(branch.returncode, 0, "the remote must not receive the rejected branch")

    @unittest.skipUnless(GITLEAKS, "Gitleaks is required for exact allowlist integration")
    def test_exact_public_vector_allowlist_does_not_exempt_new_secret_in_same_file(self):
        target = self.repo / "sdk/tests/vectors/link_pairing_v5.json"
        target.parent.mkdir(parents=True)
        shutil.copy2(SOURCE.parent / "sdk/tests/vectors/link_pairing_v5.json", target)
        self.git("add", "sdk/tests/vectors/link_pairing_v5.json")
        baseline = self.scan("--with-gitleaks")
        self.assertEqual(baseline.returncode, 0, "unchanged official synthetic vectors must pass")
        data = json.loads(target.read_text(encoding="utf-8"))
        secret = secrets.token_hex(32)
        data["added_secret"] = secret
        target.write_text(json.dumps(data, indent=2), encoding="utf-8")
        self.git("add", "sdk/tests/vectors/link_pairing_v5.json")
        result = self.scan("--with-gitleaks")
        self.assert_private(result, secret)
        self.assertNotEqual(result.returncode, 0, "the path allowlist must not cover a new secret")
        self.assertIn("Gitleaks/generic-api-key", result.stdout)

    def test_failed_gitleaks_raw_stdout_and_stderr_are_withheld(self):
        spec = importlib.util.spec_from_file_location("publication_check_under_test",
                                                     self.repo / "scripts/check-publication.py")
        checker = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(checker)
        secret = synthetic_token()
        out, err = io.StringIO(), io.StringIO()
        failed = subprocess.CompletedProcess(["fake-gitleaks"], 2, secret.encode(), secret.encode())
        with mock.patch.object(checker.shutil, "which", return_value="fake-gitleaks"), \
                mock.patch.object(checker.subprocess, "run", return_value=failed), \
                redirect_stdout(out), redirect_stderr(err), self.assertRaises(SystemExit) as exit_result:
            checker.check_gitleaks()
        self.assertFalse(secret in out.getvalue() + err.getvalue() + str(exit_result.exception),
                         "raw failing tool output must not disclose the secret")
        self.assertIn("raw output withheld", str(exit_result.exception))


if __name__ == "__main__":
    unittest.main()
