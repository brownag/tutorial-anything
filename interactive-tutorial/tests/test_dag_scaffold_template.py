"""End-to-end scaffold tests using the real templates/SKILL.md.tmpl and examples/expected-dag.json.

Every test runs in isolated temporary directories with isolated Git configuration.
Standard library only; run with::

    python3 -m unittest tests.test_dag_scaffold_template -v
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parents[1]
CLI = SKILL_DIR / "scripts" / "dag_manager.py"
EXAMPLE_DAG = SKILL_DIR / "examples" / "expected-dag.json"


class RealTemplateScaffoldTest(unittest.TestCase):
    """Verify `cmd_scaffold` against the real `templates/SKILL.md.tmpl`."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="real-scaffold-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.ws = self.tmp / "ws"
        self.ws.mkdir()
        self.dest = self.tmp / "dest"
        self.env = dict(os.environ)
        self.env.update(
            {
                "HOME": str(self.tmp),
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_CONFIG_SYSTEM": os.devnull,
            }
        )

    def _run_cli(
        self, cli_path: Path, cwd: Path, *args: str, env: dict[str, str] | None = None
    ) -> tuple[int, str]:
        proc = subprocess.run(
            [sys.executable, str(cli_path)] + list(args),
            cwd=str(cwd),
            env=env or self.env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            universal_newlines=True,
        )
        return proc.returncode, proc.stdout

    def _scaffold(self, name: str = "todo-demo") -> Path:
        code, out = self._run_cli(
            CLI,
            self.ws,
            "scaffold",
            name,
            "--from",
            str(EXAMPLE_DAG),
            "--dest",
            str(self.dest),
        )
        self.assertEqual(code, 0, out)
        return self.dest / name

    def test_scaffold_real_template_outputs_and_rendered_skill_md(self) -> None:
        target = self._scaffold("todo-demo")

        for rel_path in (
            "SKILL.md",
            "dag.json",
            "scripts/dag_manager.py",
            "references/dag-schema.json",
        ):
            self.assertTrue((target / rel_path).is_file(), f"missing {rel_path}")

        text = (target / "SKILL.md").read_text(encoding="utf-8")
        self.assertNotRegex(text, r"\{\{[^}]*\}\}")
        self.assertNotIn("{{", text)
        self.assertNotIn("}}", text)

        doc = json.loads(EXAMPLE_DAG.read_text(encoding="utf-8"))
        title = doc["title"]
        self.assertIn("name: todo-demo", text)
        self.assertIn(f"# {title} Tutorial Runner", text)
        self.assertIn(f"of {len(doc['nodes'])} steps", text)
        self.assertIn("report.md", text)
        self.assertIn("`variants`", text)
        self.assertIn("node defines `variants`", text)

    def test_generated_skill_runs_init_validate_and_status(self) -> None:
        target = self._scaffold("todo-demo")
        gen_cli = target / "scripts" / "dag_manager.py"
        gen_dag = target / "dag.json"

        repo = self.tmp / "repo"
        repo.mkdir()
        git_env = dict(self.env)
        git_env.update(
            {
                "GIT_AUTHOR_NAME": "Tutorial Test",
                "GIT_AUTHOR_EMAIL": "tutorial@example.invalid",
                "GIT_COMMITTER_NAME": "Tutorial Test",
                "GIT_COMMITTER_EMAIL": "tutorial@example.invalid",
            }
        )
        subprocess.run(
            ["git", "init", "-q", "-b", "main"],
            cwd=str(repo),
            env=git_env,
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        code, out = self._run_cli(gen_cli, repo, "init", "--from", str(gen_dag), env=git_env)
        self.assertEqual(code, 0, out)

        code, out = self._run_cli(gen_cli, repo, "validate", env=git_env)
        self.assertEqual(code, 0, out)

        code, out = self._run_cli(gen_cli, repo, "status", env=git_env)
        self.assertEqual(code, 0, out)

        state = json.loads((repo / ".tutorial-dag.json").read_text(encoding="utf-8"))
        self.assertTrue(state["nodes"])
        for node in state["nodes"]:
            self.assertEqual(node["status"], "pending")
