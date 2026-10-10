"""Regression tests for the AutoDL source-repository hygiene checker."""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/check_repo_hygiene.py"
spec = importlib.util.spec_from_file_location("repo_hygiene", SCRIPT)
assert spec and spec.loader
hygiene = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hygiene)


class RepoHygieneTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for relative in hygiene.REQUIRED:
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("", encoding="utf-8")

    def test_missing_deliberately_deleted_markdown_is_allowed(self) -> None:
        # PROJECT_STATUS / ROADMAP / PROTOCOL intentionally have no files.
        self.assertEqual(hygiene.check(self.root), [])

    def test_broken_link_to_absent_markdown_is_rejected(self) -> None:
        (self.root / "README.md").write_text(
            "Read [retired guide](docs/PROJECT_STATUS.md)", encoding="utf-8"
        )
        self.assertTrue(any("broken local link" in p for p in hygiene.check(self.root)))

    def test_links_are_checked_relative_to_the_repository_not_cwd(self) -> None:
        (self.root / "docs").mkdir(exist_ok=True)
        (self.root / "docs/README.md").write_text(
            "[code](../backend/main.py) [web](https://example.com) [anchor](#intro)",
            encoding="utf-8",
        )
        self.assertEqual(hygiene.check(self.root), [])

    def test_escaped_or_missing_link_is_rejected(self) -> None:
        (self.root / "README.md").write_text(
            "[escape](../../outside.md) [missing](docs/nothing.md)", encoding="utf-8"
        )
        errors = hygiene.check(self.root)
        self.assertEqual(len([e for e in errors if "escapes repository" in e]), 1)
        self.assertEqual(len([e for e in errors if "broken local link" in e]), 1)

    def test_reintroduced_docker_or_private_config_is_rejected(self) -> None:
        (self.root / "docker-compose.yml").write_text("services: {}", encoding="utf-8")
        (self.root / ".env.server").write_text("REAL_KEY=private", encoding="utf-8")
        errors = hygiene.check(self.root)
        self.assertTrue(any("docker-compose.yml" in e for e in errors))
        self.assertTrue(any(".env.server" in e for e in errors))

    def test_required_runtime_entrypoint_is_still_checked(self) -> None:
        (self.root / "scripts/model-service/start_vllm.sh").unlink()
        self.assertTrue(any("missing required path: scripts/model-service/start_vllm.sh" in p
                            for p in hygiene.check(self.root)))

    @unittest.skipUnless(shutil.which("git"), "git unavailable")
    def test_ignored_server_configs_allowed_but_tracked_secrets_blocked(self) -> None:
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        (self.root / ".gitignore").write_text(".env.server\nconfig.yml\n", encoding="utf-8")
        (self.root / ".env.server").write_text("SECRET=local", encoding="utf-8")
        (self.root / "config.yml").write_text("secrets: local", encoding="utf-8")
        self.assertEqual(hygiene.check(self.root), [])
        subprocess.run(["git", "-C", str(self.root), "add", "-f", ".env.server"], check=True)
        errors = hygiene.check(self.root)
        self.assertTrue(any(".env.server" in p for p in errors))
        self.assertFalse(any("config.yml" in p for p in errors))


if __name__ == "__main__":
    unittest.main()
