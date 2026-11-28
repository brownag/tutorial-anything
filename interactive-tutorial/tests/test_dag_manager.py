"""Tests for scripts/dag_manager.py.

Every test runs in its own temporary Git repository with an isolated Git
configuration, so the suite never reads or writes the developer's identity,
hooks, or ``~/.gitconfig``. Standard library only; run with::

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import contextlib
import copy
import importlib.util
import io
import json
import os
import shutil
import shlex
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

SKILL_DIR = Path(__file__).resolve().parents[1]
CLI = SKILL_DIR / "scripts" / "dag_manager.py"
EXAMPLE_DAG = SKILL_DIR / "examples" / "expected-dag.json"
STATE_NAME = ".tutorial-dag.json"

# Minimal CLI so example nodes 2-scaffold and 3-smoke can actually verify.
CLI_SOURCE = '''"""Command dispatch for the todo CLI."""

import argparse


def build_parser():
    parser = argparse.ArgumentParser(prog="todo", description="Tiny todo list.")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("add", help="Add a task")
    sub.add_parser("list", help="List tasks")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    print("running:", args.command)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''

STORE_SOURCE = '''"""JSON file persistence."""

import json
import os


def load():
    path = os.environ.get("TODO_DB", "todos.json")
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def save(tasks):
    path = os.environ.get("TODO_DB", "todos.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(tasks, handle, indent=2)
'''

RENDER_SOURCE = '''"""Fixed-width table rendering."""


def render(tasks):
    rows = ["{:<3} {}".format(i + 1, task) for i, task in enumerate(tasks)]
    return "\n".join(rows) if rows else "(no tasks)"
'''

# What each example node writes, keyed by node id.
ARTIFACTS = {
    "2-scaffold": {
        "src/todo/__init__.py": '"""Tiny todo CLI."""\n',
        "src/todo/cli.py": CLI_SOURCE,
    },
    "4-store": {"src/todo/store.py": STORE_SOURCE},
    "5-render": {"src/todo/render.py": RENDER_SOURCE},
}


class DagManagerTest(unittest.TestCase):
    """Shared fixture: temp repo + CLI runner + state readers."""

    def setUp(self):
        self.workspace = Path(tempfile.mkdtemp(prefix="dag-manager-test-"))
        self.addCleanup(shutil.rmtree, self.workspace, ignore_errors=True)
        self.env = dict(os.environ)
        self.env.update(
            {
                "HOME": str(self.workspace),
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_CONFIG_SYSTEM": os.devnull,
                "GIT_AUTHOR_NAME": "Tutorial Test",
                "GIT_AUTHOR_EMAIL": "tutorial@example.invalid",
                "GIT_COMMITTER_NAME": "Tutorial Test",
                "GIT_COMMITTER_EMAIL": "tutorial@example.invalid",
                "GIT_ADVICE_ENABLED": "false",
                "TODO_DB": "",
            }
        )
        self.env.pop("GIT_DIR", None)
        self.env.pop("GIT_WORK_TREE", None)
        self.git("init", "-q", "-b", "main")

    # -- plumbing ---------------------------------------------------------

    def git(self, *args, check=True):
        """Run git inside the temp workspace."""
        proc = subprocess.run(
            ["git"] + list(args),
            cwd=str(self.workspace),
            env=self.env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            universal_newlines=True,
        )
        if check and proc.returncode != 0:
            self.fail("git {} failed: {}".format(" ".join(args), proc.stdout))
        return proc.stdout.strip()

    def cli(self, *args, check=None):
        """Run dag_manager.py in the workspace; returns (code, stdout+stderr)."""
        proc = subprocess.run(
            [sys.executable, str(CLI)] + list(args),
            cwd=str(self.workspace),
            env=self.env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            universal_newlines=True,
        )
        output = proc.stdout or ""
        if check is not None:
            self.assertEqual(
                check,
                proc.returncode,
                "dag_manager {} exited {}:\n{}".format(
                    " ".join(args), proc.returncode, output
                ),
            )
        return proc.returncode, output

    @property
    def state_path(self) -> Path:
        return self.workspace / STATE_NAME

    def state(self) -> dict:
        """Parse the live state document."""
        self.assertTrue(self.state_path.is_file(), "no state file was written")
        return json.loads(self.state_path.read_text(encoding="utf-8"))

    def node(self, node_id: str, state=None) -> dict:
        """Fetch one node from the live state document."""
        state = self.state() if state is None else state
        for node in state["nodes"]:
            if node["id"] == node_id:
                return node
        self.fail("node {} missing from state".format(node_id))

    def head(self) -> str:
        return self.git("rev-parse", "HEAD")

    def tracked(self) -> list:
        return sorted(self.git("ls-files").split())

    def porcelain(self) -> list:
        return [line for line in self.git("status", "--porcelain").splitlines() if line]

    def write(self, relative: str, text: str) -> Path:
        """Create a workspace file, making parent directories."""
        path = self.workspace / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def materialize(self, node_id: str) -> None:
        """Write the artifacts the example DAG expects for ``node_id``."""
        for relative, text in ARTIFACTS.get(node_id, {}).items():
            self.write(relative, text)

    def seed_repo(self, message: str = "seed") -> str:
        """Give the repo one commit so rollback targets exist."""
        self.write("README.md", "# Scratch workspace\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message)
        return self.head()

    def seed_state(self, **overrides) -> dict:
        """Load the reference DAG into the workspace as live state.

        Follows the real workflow - `init` first (which records the git block and
        hides the state file from Git), then fill the `nodes` array from
        ``examples/expected-dag.json``. That example models a repository with no
        commits yet, so its recorded sha is replaced with this repo's HEAD.
        """
        self.init_command(check=0)
        state = copy.deepcopy(json.loads(EXAMPLE_DAG.read_text(encoding="utf-8")))
        live = self.state()
        state["git"] = dict(live["git"])
        state["created_at"] = live["created_at"]
        state["active_node_id"] = None
        for key, value in overrides.items():
            if key == "git":
                state["git"].update(value)
            else:
                state[key] = value
        self.state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
        return state

    def init_command(self, *extra, check=None):
        """Run `init` with the standard arguments plus overrides."""
        return self.cli(
            "init",
            "--title",
            "Todo CLI",
            "--source",
            "examples/sample-tutorial.md",
            *extra,
            check=check,
        )

    def complete(self, node_id: str, *extra, check=None):
        """Materialize a node's files, then complete it."""
        self.materialize(node_id)
        return self.cli("complete", node_id, "--yes", *extra, check=check)

    def derived(self, node_id: str) -> str:
        """The STATE column `status` shows for a node."""
        _, output = self.cli("status", check=0)
        for line in output.splitlines():
            parts = line.split()
            if parts and parts[0].lstrip("> ") == node_id and len(parts) > 2:
                return parts[1]
            if parts and parts[0] == node_id and len(parts) > 2:
                return parts[1]
        self.fail("node {} not found in status output:\n{}".format(node_id, output))


    # -- init -------------------------------------------------------------

    def test_init_records_branch_in_unborn_repo(self):
        self.init_command("--git-branch", "tutorial/x", check=0)

        self.assertEqual(self.state()["git"]["branch"], "tutorial/x")

    def test_init_writes_state_and_excludes_state_file_once(self):
        anchor = self.seed_repo()
        self.init_command("--git-branch", "tutorial/todo", check=0)

        state = self.state()
        self.assertEqual(state["mode"], "assisted")
        self.assertEqual(state["persistence"], "file")
        self.assertIsNone(state["active_node_id"])
        self.assertEqual(state["nodes"], [])
        self.assertEqual(state["git"]["initial_commit_sha"], anchor)
        self.assertEqual(state["git"]["branch"], "tutorial/todo")
        self.assertTrue(state["git"]["exclude_configured"])
        self.assertEqual(self.git("rev-parse", "--abbrev-ref", "HEAD"), "tutorial/todo")

        exclude = (
            self.workspace / self.git("rev-parse", "--git-common-dir") / "info" / "exclude"
        )
        entries = [
            line.strip()
            for line in exclude.read_text(encoding="utf-8").splitlines()
            if line.strip() == STATE_NAME
        ]
        self.assertEqual(len(entries), 1)

        self.init_command("--force", check=0)
        entries = [
            line.strip()
            for line in exclude.read_text(encoding="utf-8").splitlines()
            if line.strip() == STATE_NAME
        ]
        self.assertEqual(len(entries), 1, "exclude entry must not duplicate")
        self.assertEqual(self.porcelain(), [], "state file must stay invisible to git")

    def test_init_refuses_overwrite_without_force(self):
        self.init_command(check=0)
        code, output = self.init_command()
        self.assertEqual(code, 1)
        self.assertIn("--force", output)

    def test_init_needs_a_git_repository(self):
        shutil.rmtree(self.workspace / ".git")
        code, output = self.init_command()
        self.assertEqual(code, 1)
        self.assertIn("git init", output)

    def test_init_reports_a_dirty_workspace(self):
        self.write("loose.txt", "uncommitted\n")
        self.seed_repo()
        self.write("loose.txt", "modified\n")
        self.init_command(check=0)
        self.assertTrue(self.state()["git"]["initial_dirty"])
        self.assertIn("dirty", self.init_command("--force")[1].lower())

    # -- validate ---------------------------------------------------------

    def test_validate_rejects_cycles_by_naming_them(self):
        self.seed_repo()
        state = self.seed_state()
        by_id = {node["id"]: node for node in state["nodes"]}
        by_id["4-store"]["dependencies"] = ["5-render"]
        by_id["5-render"]["dependencies"] = ["4-store"]
        self.state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")

        code, output = self.cli("validate")
        self.assertEqual(code, 1)
        self.assertIn("cycle", output)
        self.assertIn("4-store", output)
        self.assertIn("5-render", output)

    def test_validate_rejects_unknown_dependency_and_duplicate_id(self):
        self.seed_repo()
        state = self.seed_state()
        state["nodes"][1]["dependencies"] = ["9-does-not-exist"]
        self.state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
        code, output = self.cli("validate")
        self.assertEqual(code, 1)
        self.assertIn("9-does-not-exist", output)

        state = json.loads(EXAMPLE_DAG.read_text(encoding="utf-8"))
        state["git"]["initial_commit_sha"] = self.head()
        state["nodes"].append(copy.deepcopy(state["nodes"][0]))
        self.state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
        code, output = self.cli("validate")
        self.assertEqual(code, 1)
        self.assertIn("duplicate id", output)

    def test_validate_rejects_bad_verification_shapes(self):
        self.seed_repo()
        state = self.seed_state()
        state["nodes"][3]["verification"] = {"type": "file_contains", "path": "src/todo/store.py"}
        self.state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
        code, output = self.cli("validate")
        self.assertEqual(code, 1)
        self.assertIn("pattern", output)

    def test_validate_flags_an_empty_node_list_as_unfinished(self):
        self.seed_repo()
        self.init_command(check=0)
        code, output = self.cli("validate", check=0)
        self.assertIn("decompose", output)

    def _validate_edited(self, edit):
        """Seed a valid DAG, let ``edit`` mutate it, and run validate."""
        if not hasattr(self, "_base_state"):
            self.seed_repo()
            self._base_state = self.seed_state()
        state = copy.deepcopy(self._base_state)
        edit(state)
        self.state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
        return self.cli("validate")

    def test_validate_side_effects_shapes(self):
        def node(value):
            return lambda s: s["nodes"][0].__setitem__("side_effects", value)

        code, _ = self._validate_edited(node(["repo", "user"]))
        self.assertEqual(code, 0)
        for bad in ([], ["repo", "repo"], ["cloud"], "user", [""]):
            code, output = self._validate_edited(node(bad))
            self.assertEqual(code, 1, bad)
            self.assertIn("side_effects", output)

    def test_validate_undo_shapes(self):
        def node(value):
            return lambda s: s["nodes"][0].__setitem__("undo", value)

        code, _ = self._validate_edited(node(["rm -f x"]))
        self.assertEqual(code, 0)
        for bad in ("rm -f x", [""], [3]):
            code, output = self._validate_edited(node(bad))
            self.assertEqual(code, 1, bad)
            self.assertIn("undo", output)

    def test_validate_residue_shapes(self):
        entry = {
            "node_id": "1-a",
            "scopes": ["user"],
            "undo": ["rm -f x"],
            "reason": "not-run",
            "recorded_at": "2026-11-26T00:00:00+00:00",
        }

        def residue(value):
            return lambda s: s.__setitem__("residue", value)

        code, _ = self._validate_edited(residue([entry]))
        self.assertEqual(code, 0)
        bad_entries = [
            dict(entry, reason="maybe"),
            dict(entry, scopes=["cloud"]),
            dict(entry, undo="rm -f x"),
            {k: v for k, v in entry.items() if k != "node_id"},
            dict(entry, recorded_at=None),
        ]
        for bad in bad_entries:
            code, output = self._validate_edited(residue([bad]))
            self.assertEqual(code, 1, bad)
            self.assertIn("residue", output)

    def test_validate_warns_when_side_effects_have_no_undo(self):
        def node(extra):
            def edit(s):
                s["nodes"][0]["side_effects"] = ["user", "system"]
                s["nodes"][0].pop("undo", None)
                s["nodes"][0].update(extra)
            return edit

        code, output = self._validate_edited(node({}))
        self.assertEqual(code, 0)
        self.assertIn(
            "has user, system side effects but no undo (rewinds cannot revert it)",
            output,
        )
        code, output = self._validate_edited(node({"undo": ["rm -f x"]}))
        self.assertEqual(code, 0)
        self.assertNotIn("but no undo", output)

    def test_validate_artifacts_warning_respects_side_effects(self):
        def mutate(side_effects):
            def edit(s):
                node = s["nodes"][0]
                node["action_type"] = "mutate"
                node["artifacts"] = []
                node.pop("side_effects", None)
                if side_effects is not None:
                    node["side_effects"] = side_effects
            return edit

        code, output = self._validate_edited(mutate(["user"]))
        self.assertEqual(code, 0)
        self.assertNotIn("lists no artifacts", output)

        code, output = self._validate_edited(mutate(None))
        self.assertEqual(code, 0)
        self.assertIn("is a mutate node but lists no artifacts", output)

    def test_schema_1_0_document_still_validates(self):
        for version in ("1.0", "1.1"):
            code, _ = self._validate_edited(
                lambda s, v=version: s.__setitem__("schema_version", v)
            )
            self.assertEqual(code, 0, version)
        code, _ = self._validate_edited(
            lambda s: s.__setitem__("schema_version", "2.0")
        )
        self.assertEqual(code, 1)

    # -- next / status ----------------------------------------------------

    def test_next_walks_the_linear_core(self):
        self.seed_repo()
        self.seed_state()
        self.cli("next", check=0)
        self.assertEqual(self.state()["active_node_id"], "1-design")
        self.cli("complete", "1-design", "--yes", check=0)
        self.assertEqual(
            self.state()["active_node_id"],
            "2-scaffold",
            "a single unambiguous ready node should auto-advance",
        )

    def test_next_prints_already_active_pending_node(self):
        self.seed_repo()
        self.seed_state()
        self.cli("next", check=0)
        before_state = self.state_path.read_text(encoding="utf-8")
        code, output = self.cli("next")
        self.assertEqual(code, 0, output)
        self.assertIn("active -> 1-design", output)
        self.assertIn("active: 1-design", output)
        self.assertEqual(self.state_path.read_text(encoding="utf-8"), before_state)

    def test_next_after_complete_auto_activation_prints_active_with_hints(self):
        self.seed_repo()
        state = self.seed_state()
        self.node("2-scaffold", state)["interactive"] = {"human": True}
        self.state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
        self.cli("complete", "1-design", "--yes", check=0)
        self.assertEqual(self.state()["active_node_id"], "2-scaffold")
        before_state = self.state_path.read_text(encoding="utf-8")
        code, output = self.cli("next")
        self.assertEqual(code, 0, output)
        self.assertIn("active -> 2-scaffold", output)
        self.assertIn("a person must run this step (interactive)", output)
        self.assertIn("active: 2-scaffold", output)
        self.assertEqual(self.state_path.read_text(encoding="utf-8"), before_state)

    def test_next_refuses_a_branch_point_and_lists_both(self):
        self.seed_repo()
        self.seed_state()
        for node_id in ("1-design", "2-scaffold", "3-smoke"):
            self.complete(node_id, check=0)

        code, output = self.cli("next")
        self.assertEqual(code, 1)
        self.assertIn("4-store", output)
        self.assertIn("5-render", output)
        self.assertIsNone(self.state()["active_node_id"])

    def test_completing_the_parent_unlocks_both_optional_branches(self):
        self.seed_repo()
        self.seed_state()
        self.assertEqual(self.derived("4-store"), "PENDING")
        self.assertEqual(self.derived("5-render"), "PENDING")
        for node_id in ("1-design", "2-scaffold", "3-smoke"):
            self.complete(node_id, check=0)
        self.assertEqual(self.derived("4-store"), "READY")
        self.assertEqual(self.derived("5-render"), "READY")
        _, header = self.cli("status", check=0)
        self.assertIn("4-store, 5-render", header.splitlines()[0])

    # -- verify / complete ------------------------------------------------

    def test_verify_gate_variants(self):
        self.seed_repo()
        self.seed_state()

        self.cli("verify", "2-scaffold", check=1)  # file_exists, missing
        self.materialize("2-scaffold")
        self.cli("verify", "2-scaffold", check=0)

        self.cli("verify", "4-store", check=1)  # file_contains, missing file
        self.materialize("4-store")
        self.cli("verify", "4-store", check=0)
        self.write("src/todo/store.py", "def load():\n    return []\n")
        self.cli("verify", "4-store", check=1)  # literal substring absent
        self.write("src/todo/store.py", "def save(tasks):\n    pass\n")
        self.cli("verify", "4-store", check=0)

        state = self.state()
        node = self.node("5-render", state)
        node["verification"] = {"type": "command", "command": "exit 3", "expect_exit": 0}
        self.state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
        code, output = self.cli("verify", "5-render")
        self.assertEqual(code, 1)
        self.assertIn("exit code 3", output)

    def test_confirm_gate_needs_a_human(self):
        self.seed_repo()
        self.seed_state()
        code, output = self.cli("complete", "1-design")
        self.assertEqual(code, 1)
        self.assertIn("CONFIRMATION NEEDED", output)
        self.assertEqual(self.node("1-design")["status"], "pending")
        self.cli("complete", "1-design", "--yes", check=0)
        self.assertEqual(self.node("1-design")["status"], "completed")

    def test_no_verify_records_progress_but_verification_gates_work(self):
        self.seed_repo()
        self.seed_state()
        self.cli("complete", "1-design", "--yes", check=0)
        code, _ = self.cli("complete", "2-scaffold", "--no-verify")
        self.assertEqual(code, 0)
        self.assertEqual(self.node("2-scaffold")["status"], "completed")
        self.assertIsNone(self.node("2-scaffold")["commit_sha"])

    def test_complete_refuses_unmet_dependencies(self):
        self.seed_repo()
        self.seed_state()
        self.materialize("2-scaffold")
        code, output = self.cli("complete", "2-scaffold", "--yes")
        self.assertEqual(code, 1)
        self.assertIn("1-design", output)

    def test_complete_verification_failure_hint_depends_on_mode(self):
        root = self._root_with(
            "hands-off",
            verification={"type": "command", "command": "exit 1", "expect_exit": 0},
        )
        code, output = self.cli("complete", root)
        self.assertEqual(code, 1, output)
        self.assertIn(
            "verification failed for {} - state unchanged. Fix the work and re-run.".format(root),
            output,
        )
        self.assertNotIn("--no-verify", output)

        state = self.state()
        state["mode"] = "assisted"
        self.state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
        code, output = self.cli("complete", root)
        self.assertEqual(code, 1, output)
        self.assertIn(
            "verification failed for {} - state unchanged. Fix the work, or pass --no-verify to record it anyway.".format(root),
            output,
        )

    # -- checkpoints ------------------------------------------------------

    def cli_stdout(self, *args) -> str:
        """Run the CLI with stderr discarded, for machine-readable output."""
        proc = subprocess.run(
            [sys.executable, str(CLI)] + list(args),
            cwd=str(self.workspace),
            env=self.env,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            universal_newlines=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout)
        return proc.stdout

    def run_to_store(self) -> dict:
        """Walk the reference DAG through the persistence branch.

        Leaves three commits: the seed, the scaffold checkpoint, and the store
        checkpoint, with 3-smoke completed in between.
        """
        anchor = self.seed_repo()
        self.seed_state()
        self.cli("next", check=0)
        self.complete("1-design", check=0)
        self.complete("2-scaffold", "--commit", check=0)
        scaffold_commit = self.node("2-scaffold")["commit_sha"]
        self.complete("3-smoke", check=0)
        self.complete("4-store", "--commit", check=0)
        return {
            "anchor": anchor,
            "scaffold": scaffold_commit,
            "store": self.head(),
        }

    def test_complete_commit_records_sha_and_keeps_state_untracked(self):
        shas = self.run_to_store()
        node = self.node("2-scaffold")

        self.assertEqual(node["commit_sha"], shas["scaffold"])
        self.assertRegex(node["commit_sha"], r"^[0-9a-f]{40}$")
        subject = self.git("log", "-1", "--format=%s", node["commit_sha"])
        self.assertEqual(subject, "tutorial(2-scaffold): Scaffold src/todo with the dispatch table")
        self.assertEqual(node["status"], "completed")

        self.assertNotIn(STATE_NAME, self.tracked())
        self.assertEqual(self.porcelain(), [], "state file must never be committable")
        listed = self.git("show", "--name-only", "--format=", shas["store"])
        self.assertNotIn(STATE_NAME, listed)
        self.assertIn("src/todo/store.py", listed)

    def test_complete_commit_on_clean_tree_completes_without_checkpoint(self):
        shas = self.run_to_store()
        code, output = self.cli("complete", "5-render", "--commit", "--no-verify")
        self.assertEqual(code, 0, output)
        self.assertIn("nothing to commit; completed 5-render without a checkpoint", output)
        node = self.node("5-render")
        self.assertEqual(node["status"], "completed")
        self.assertIsNone(node["commit_sha"])
        self.assertEqual(self.head(), shas["store"])

    def test_jump_refuses_unmet_ancestors_without_the_flag(self):
        self.run_to_store()
        code, output = self.cli("jump", "7-converge")
        self.assertEqual(code, 1)
        self.assertIn("5-render", output)
        self.assertIn("--skip-ancestors", output)
        self.assertNotEqual(self.state()["active_node_id"], "7-converge")

    def test_jump_skip_ancestors_fast_forwards_without_touching_git(self):
        shas = self.run_to_store()
        code, output = self.cli("jump", "7-converge", "--skip-ancestors", check=0)
        self.assertIn("marked skipped", output)

        state = self.state()
        self.assertEqual(state["active_node_id"], "7-converge")
        self.assertEqual(self.node("5-render", state)["status"], "skipped")
        self.assertEqual(self.node("6-db-path", state)["status"], "skipped")
        self.assertEqual(self.node("4-store", state)["status"], "completed")
        self.assertEqual(self.head(), shas["store"], "fast-forward must not commit")
        self.assertEqual(self.git("rev-list", "--count", "HEAD"), "3")

    def run_both_branches(self) -> dict:
        """Complete both sibling branches (4-store, then 5-render) off 3-smoke."""
        shas = self.run_to_store()
        self.complete("5-render", "--commit", check=0)
        shas["render"] = self.head()
        return shas

    def test_hard_rewind_refuses_to_discard_sibling_branch(self):
        shas = self.run_both_branches()
        before = self.state_path.read_text(encoding="utf-8")
        code, output = self.cli(
            "jump", "4-store", "--reset-descendants", "--git-rollback", "hard", "--force"
        )
        self.assertEqual(code, 1, output)
        self.assertIn("5-render", output)
        self.assertEqual(self.head(), shas["render"], "HEAD must not move")
        self.assertTrue((self.workspace / "src/todo/render.py").is_file())
        self.assertEqual(before, self.state_path.read_text(encoding="utf-8"))

    def test_branch_rewind_refuses_to_discard_sibling_branch(self):
        self.run_both_branches()
        before = self.state_path.read_text(encoding="utf-8")
        code, output = self.cli(
            "jump", "4-store", "--reset-descendants", "--git-rollback", "branch"
        )
        self.assertEqual(code, 1, output)
        self.assertIn("5-render", output)
        self.assertEqual(self.git("rev-parse", "--abbrev-ref", "HEAD"), "main")
        self.assertEqual("", self.git("branch", "--list", "tutorial/rewind-*"))
        self.assertEqual(before, self.state_path.read_text(encoding="utf-8"))

    def test_hard_rewind_to_common_ancestor_resets_both_branches(self):
        shas = self.run_both_branches()
        self.cli(
            "jump", "3-smoke", "--reset-descendants", "--git-rollback", "hard", "--force",
            check=0,
        )
        self.assertEqual(self.head(), shas["scaffold"])
        state = self.state()
        for nid in ("4-store", "5-render"):
            self.assertEqual("pending", self.node(nid, state)["status"])
        self.assertFalse((self.workspace / "src/todo/render.py").exists())

    def test_soft_rewind_moves_only_the_pointer(self):
        shas = self.run_to_store()
        before = (self.head(), (self.workspace / "src/todo/store.py").read_text())

        code, output = self.cli("jump", "3-smoke", check=0)
        self.assertIn("downstream", output)

        state = self.state()
        self.assertEqual(state["active_node_id"], "3-smoke")
        self.assertEqual(self.head(), before[0], "soft rewind must not move HEAD")
        self.assertEqual(
            (self.workspace / "src/todo/store.py").read_text(),
            before[1],
            "soft rewind must not touch files",
        )
        self.assertEqual(self.node("4-store", state)["status"], "completed")
        self.assertEqual(self.node("4-store", state)["commit_sha"], shas["store"])
        self.assertEqual(self.git("rev-parse", "--abbrev-ref", "HEAD"), "main")

    def test_hard_rollback_reverts_files_and_resets_downstream(self):
        shas = self.run_to_store()
        code, output = self.cli(
            "jump",
            "3-smoke",
            "--reset-descendants",
            "--git-rollback",
            "hard",
            "--force",
            check=0,
        )
        self.assertIn("reset --hard", output)

        state = self.state()
        self.assertEqual(self.head(), shas["scaffold"])
        self.assertTrue((self.workspace / "src/todo/cli.py").is_file())
        self.assertFalse(
            (self.workspace / "src/todo/store.py").exists(),
            "the store checkpoint's files should be reverted",
        )

        self.assertEqual(state["active_node_id"], "3-smoke")
        self.assertEqual(self.node("3-smoke", state)["status"], "pending")
        self.assertIsNone(self.node("3-smoke", state)["commit_sha"])
        self.assertEqual(self.node("4-store", state)["status"], "pending")
        self.assertIsNone(self.node("4-store", state)["commit_sha"])
        self.assertEqual(
            self.node("2-scaffold", state)["status"],
            "completed",
            "the target's own ancestor checkpoint must stay intact",
        )

        self.assertTrue(self.state_path.is_file(), ".tutorial-dag.json must survive")
        self.assertNotIn(STATE_NAME, self.tracked())
        self.assertEqual(self.porcelain(), [])

    def test_hard_rollback_refuses_a_dirty_workspace_without_force(self):
        self.run_to_store()
        self.write("notes.md", "uncommitted scribble\n")
        code, output = self.cli("jump", "3-smoke", "--reset-descendants", "--git-rollback", "hard")
        self.assertEqual(code, 1)
        self.assertIn("--force", output)
        self.assertTrue((self.workspace / "src/todo/store.py").is_file())

    def test_branch_rollback_reopens_the_target_on_a_new_branch(self):
        shas = self.run_to_store()
        code, output = self.cli(
            "jump",
            "4-store",
            "--reset-descendants",
            "--git-rollback",
            "branch",
            "--new-branch",
            "tutorial/try-again",
            check=0,
        )
        self.assertIn("tutorial/try-again", output)

        state = self.state()
        self.assertEqual(self.git("rev-parse", "--abbrev-ref", "HEAD"), "tutorial/try-again")
        self.assertEqual(self.head(), shas["scaffold"])
        self.assertFalse((self.workspace / "src/todo/store.py").exists())
        self.assertEqual(state["git"]["branch"], "tutorial/try-again")
        self.assertEqual(self.node("4-store", state)["status"], "pending")
        self.assertEqual(
            self.git("rev-parse", "--verify", "refs/heads/main"),
            shas["store"],
            "the original checkpoint history must still exist on main",
        )

    def test_rollback_requires_reset_descendants(self):
        self.run_to_store()
        code, output = self.cli("jump", "3-smoke", "--git-rollback", "hard", "--force")
        self.assertEqual(code, 1)
        self.assertIn("--reset-descendants", output)

    def _set_side_effects(self, node_id, scopes, undo):
        """Declare side effects on a live node (run_to_store leaves 2-scaffold, 3-smoke, 4-store completed)."""
        state = self.state()
        node = self.node(node_id, state)
        node["side_effects"] = scopes
        node["undo"] = undo
        self.state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")

    def test_jump_undo_runs_undo_and_leaves_no_residue(self):
        self.run_to_store()
        self._set_side_effects("4-store", ["user"], ["touch undone.flag"])
        code, output = self.cli("jump", "3-smoke", "--reset-descendants", "--undo")
        self.assertEqual(code, 0, output)
        self.assertIn("side effects: 4-store [user] undo: touch undone.flag", output)
        self.assertTrue((self.workspace / "undone.flag").exists())
        self.assertFalse(self.state().get("residue"))

    def test_jump_without_undo_records_not_run_residue(self):
        self.run_to_store()
        self._set_side_effects("4-store", ["user"], ["touch undone.flag"])
        code, output = self.cli("jump", "3-smoke", "--reset-descendants", check=0)
        self.assertFalse((self.workspace / "undone.flag").exists())
        residue = self.state()["residue"]
        self.assertEqual([(e["node_id"], e["reason"]) for e in residue], [("4-store", "not-run")])
        _, status = self.cli("status", check=0)
        self.assertIn("residue: 1", status)

    def test_failed_undo_stops_and_marks_the_rest_not_run(self):
        self.run_to_store()
        self._set_side_effects("3-smoke", ["user"], ["touch older.flag"])
        self._set_side_effects("4-store", ["user"], ["false"])
        code, output = self.cli("jump", "2-scaffold", "--reset-descendants", "--undo")
        self.assertEqual(code, 1, output)
        self.assertFalse((self.workspace / "older.flag").exists())
        reasons = {e["node_id"]: e["reason"] for e in self.state()["residue"]}
        self.assertEqual(reasons, {"4-store": "failed", "3-smoke": "not-run"})

    def test_remote_and_irreversible_nodes_are_never_undone(self):
        self.run_to_store()
        self._set_side_effects("4-store", ["remote"], ["touch remote.flag"])
        self._set_side_effects("3-smoke", ["user"], [])
        code, output = self.cli("jump", "2-scaffold", "--reset-descendants", "--undo", check=0)
        self.assertFalse((self.workspace / "remote.flag").exists())
        reasons = {e["node_id"]: e["reason"] for e in self.state()["residue"]}
        self.assertEqual(reasons, {"4-store": "remote", "3-smoke": "irreversible"})
        self.assertIn("residue: 2", self.cli("status", check=0)[1])

    def test_hands_off_skips_undo_of_unapproved_high_risk_node(self):
        self.run_to_store()
        self._set_side_effects("4-store", ["user"], ["sudo rm -f undone.flag"])
        state = self.state()
        state["mode"] = "hands-off"
        self.state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
        code, output = self.cli("jump", "3-smoke", "--reset-descendants", "--undo")
        self.assertEqual(code, 0, output)
        self.assertIn("skipped undo of 4-store", output)
        self.assertEqual(
            [("4-store", "not-run")],
            [(e["node_id"], e["reason"]) for e in self.state().get("residue") or []],
        )

    def test_undo_requires_reset_descendants(self):
        self.run_to_store()
        code, output = self.cli("jump", "3-smoke", "--undo")
        self.assertEqual(code, 2)
        self.assertIn("--reset-descendants", output)

    def test_residue_clear_removes_one_entry_or_all(self):
        self.run_to_store()
        self._set_side_effects("4-store", ["user"], ["touch undone.flag"])
        self.cli("jump", "3-smoke", "--reset-descendants", check=0)
        self.cli("residue", "clear", check=2)
        self.cli("residue", "clear", "4-store", "--all", check=2)
        self.cli("residue", "clear", "4-store", check=0)
        self.assertEqual(self.state()["residue"], [])
        state = self.state()
        state["residue"] = [
            {"node_id": "3-smoke", "scopes": ["user"], "undo": [], "reason": "irreversible",
             "recorded_at": "2026-11-26T00:00:00Z"},
            {"node_id": "4-store", "scopes": ["user"], "undo": ["true"], "reason": "not-run",
             "recorded_at": "2026-11-26T00:00:00Z"},
        ]
        self.state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
        self.cli("residue", "clear", "--all", check=0)
        self.assertEqual(self.state()["residue"], [])

    def test_status_json_round_trips(self):
        self.run_to_store()
        rendered = self.cli_stdout("status", "--json")
        document = json.loads(rendered)
        self.assertEqual(document, self.state())

        mirror = self.workspace / "in-context.json"
        mirror.write_text(rendered, encoding="utf-8")
        self.cli("--file", "in-context.json", "validate", check=0)
        code, output = self.cli("--file", "in-context.json", "status")
        self.assertEqual(code, 0)
        self.assertIn("4-store", output)

    def _root_with(self, mode, **node_fields):
        """Seed a repo and a DAG in ``mode``; patch the first root node's fields."""
        self.seed_repo()
        state = self.seed_state(mode=mode)
        root = next(n for n in state["nodes"] if not n["dependencies"])
        root.update(node_fields)
        self.state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
        return root["id"]

    def test_hands_off_blocks_high_risk_until_approved(self):
        root = self._root_with("hands-off", commands=["sudo true"])
        code, output = self.cli("next", root)
        self.assertEqual(code, 1, output)
        self.assertIn("approve {}".format(root), output)
        self.assertIsNone(self.state().get("active_node_id"))

        self.cli("approve", root, check=0)
        self.cli("next", root, check=0)
        self.assertEqual(self.state()["active_node_id"], root)

    def test_guided_mode_never_blocks_high_risk(self):
        root = self._root_with("guided", commands=["sudo true"])
        code, output = self.cli("next", root)
        self.assertEqual(code, 0, output)
        self.assertIn("risk: high (sudo)", output)

    def test_assisted_mode_never_blocks_high_risk(self):
        root = self._root_with("assisted", commands=["sudo true"])
        code, output = self.cli("next", root)
        self.assertEqual(code, 0, output)
        self.assertIn("risk: high (sudo)", output)

    def test_approval_covers_only_the_tier_it_recorded(self):
        root = self._root_with("hands-off", side_effects=["user"], commands=["ls"])
        self.cli("approve", root, check=0)
        self.assertEqual(self.node(root)["approved"]["tier"], "elevated")
        self.cli("next", root, check=0)

        state = self.state()
        self.node(root, state)["commands"] = ["sudo true"]
        self.state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
        code, output = self.cli("next", root)
        self.assertEqual(code, 1, output)
        self.assertIn("approve {}".format(root), output)

    def _risky_check(self):
        """A command check that is high risk (sudo) and leaves a marker when run."""
        return {"type": "command", "command": "sudo -n true 2>/dev/null; touch ran.marker"}

    def test_hands_off_verify_refuses_unapproved_high_risk(self):
        root = self._root_with("hands-off", verification=self._risky_check())
        code, output = self.cli("verify", root)
        self.assertEqual(code, 1, output)
        self.assertIn("approve {}".format(root), output)
        self.assertFalse((self.workspace / "ran.marker").exists(), "check must not run")

        self.cli("approve", root, check=0)
        self.cli("verify", root)
        self.assertTrue((self.workspace / "ran.marker").exists())

    def test_hands_off_jump_refuses_unapproved_high_risk(self):
        root = self._root_with("hands-off", commands=["sudo true"])
        code, output = self.cli("jump", root)
        self.assertEqual(code, 1, output)
        self.assertIn("approve {}".format(root), output)
        self.assertIsNone(self.state().get("active_node_id"))

        self.cli("approve", root, check=0)
        self.cli("jump", root, check=0)
        self.assertEqual(self.state()["active_node_id"], root)

    def test_assisted_verify_runs_high_risk_with_notice(self):
        root = self._root_with("assisted", verification=self._risky_check())
        code, output = self.cli("verify", root)
        self.assertIn("risk: high (sudo)", output)
        self.assertTrue((self.workspace / "ran.marker").exists())

    def _free_port(self):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            return probe.getsockname()[1]

    def _service_root(self, command, ready, **service_extra):
        service = dict({"command": command, "ready": ready}, **service_extra)
        node_id = self._root_with("assisted", service=service, verification=ready)
        self.addCleanup(self.cli, "teardown")
        return node_id

    def _http_server_command(self, port):
        return "{} -m http.server {} --bind 127.0.0.1".format(
            shlex.quote(sys.executable), port
        )

    def test_service_start_ready_then_stop_round_trip(self):
        port = self._free_port()
        node_id = self._service_root(
            self._http_server_command(port), {"type": "port_listening", "port": port}
        )
        self.cli("next", node_id, check=0)
        code, output = self.cli("service", "start", node_id)
        self.assertEqual(code, 0, output)
        with socket.create_connection(("127.0.0.1", port), timeout=2):
            pass
        services = json.loads(self.cli("status", "--json")[1])["services"]
        self.assertEqual(services[node_id]["pid"], services[node_id]["pgid"])
        self.assertTrue((self.workspace / ".tutorial-logs" / "{}.log".format(node_id)).is_file())
        self.assertIn("svc: 1", self.cli("status")[1])

        self.cli("service", "stop", node_id, check=0)
        with self.assertRaises(OSError):
            socket.create_connection(("127.0.0.1", port), timeout=2).close()
        self.assertFalse(json.loads(self.cli("status", "--json")[1]).get("services"))

    def test_service_start_refuses_a_running_service(self):
        port = self._free_port()
        node_id = self._service_root(
            self._http_server_command(port), {"type": "port_listening", "port": port}
        )
        self.cli("next", node_id, check=0)
        self.cli("service", "start", node_id, check=0)
        code, output = self.cli("service", "start", node_id)
        self.assertEqual(code, 1, output)
        self.assertIn("already running", output)

    def test_ready_timeout_stops_the_process_and_drops_the_record(self):
        node_id = self._service_root(
            "echo never-ready; sleep 30",
            {"type": "port_listening", "port": self._free_port()},
            ready_timeout=1,
        )
        self.cli("next", node_id, check=0)
        started = time.monotonic()
        code, output = self.cli("service", "start", node_id)
        self.assertLess(time.monotonic() - started, 20)
        self.assertEqual(code, 1, output)
        self.assertIn("did not pass within 1s", output)
        self.assertIn("log tail", output)
        self.assertIn("never-ready", output)
        self.assertFalse(json.loads(self.cli("status", "--json")[1]).get("services"))

    def test_stop_of_an_already_dead_process_is_not_an_error(self):
        node_id = self._service_root(
            "true", {"type": "port_listening", "port": self._free_port()}
        )
        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait()
        state = self.state()
        state["services"] = {
            node_id: {
                "pid": dead.pid,
                "pgid": dead.pid,
                "command": "true",
                "log": ".tutorial-logs/{}.log".format(node_id),
                "started_at": "2026-11-26T00:00:00Z",
            }
        }
        self.state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
        code, output = self.cli("service", "stop", node_id)
        self.assertEqual(code, 0, output)
        self.assertIn("was not running", output)
        self.assertFalse(self.state().get("services"))

    def test_interactive_hints_print_on_next(self):
        node_id = self._root_with(
            "assisted",
            commands=["sh ask.sh"],
            interactive={"answers": ["y", "my name"]},
            verification={"type": "confirm", "prompt": "ok?"},
        )
        code, output = self.cli("next", node_id)
        self.assertEqual(code, 0, output)
        self.assertIn("feed answers: printf '%s\\n' y 'my name' | sh ask.sh", output)

    def test_human_interactive_hint_prints_on_next(self):
        node_id = self._root_with(
            "assisted",
            interactive={"human": True},
            verification={"type": "confirm", "prompt": "ok?"},
        )
        code, output = self.cli("next", node_id)
        self.assertEqual(code, 0, output)
        self.assertIn("a person must run this step (interactive)", output)

    def test_validate_warns_on_interactive_command_without_block(self):
        node_id = self._root_with(
            "assisted",
            commands=["npm init"],
            verification={"type": "confirm", "prompt": "ok?"},
        )
        code, output = self.cli("validate")
        self.assertEqual(code, 0, output)
        self.assertIn("node '{}' command looks interactive: npm init".format(node_id), output)

    def test_service_block_validation(self):
        node_id = self._service_root(
            "true", {"type": "port_listening", "port": self._free_port()}
        )
        for bad in (
            {"command": "true", "ready": {"type": "confirm", "prompt": "ok?"}},
            {"command": "true"},
            {"command": "true", "ready": {"type": "port_listening", "port": 1}, "stop_signal": "HUP"},
        ):
            state = self.state()
            self.node(node_id, state)["service"] = bad
            self.state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
            code, output = self.cli("validate")
            self.assertEqual(code, 1, bad)
            self.assertIn("service", output)

    def test_validate_rejects_bad_risk_and_approved_values(self):
        root = self._root_with("hands-off")
        state = self.state()
        self.node(root, state)["risk"] = "critical"
        self.state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
        code, output = self.cli("validate")
        self.assertEqual(code, 1, output)
        self.assertIn("'risk'", output)

    # -- deviate (C-G8) ---------------------------------------------------

    def _node_with(self, **fields):
        self.seed_repo()
        state = self.seed_state()
        node = state["nodes"][0]
        node.update(fields)
        self.state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
        return node["id"]

    def test_deviate_check_replaces_verification_command_and_records(self):
        nid = self._node_with(verification={"type": "command", "command": "./greet --name Ada"})
        code, output = self.cli("deviate", nid, "--check", "--to", "./greet --who Ada",
                                "--reason", "v2 renamed --name to --who")
        self.assertEqual(code, 0, output)
        self.assertIn("deviation recorded: {} check".format(nid), output)
        node = self.node(nid)
        self.assertEqual(node["verification"]["command"], "./greet --who Ada")
        (record,) = node["deviations"]
        self.assertEqual(record["target"], "check")
        self.assertEqual(record["original"], "./greet --name Ada")
        self.assertEqual(record["replacement"], "./greet --who Ada")
        self.assertEqual(record["reason"], "v2 renamed --name to --who")
        self.assertTrue(record["recorded_at"].endswith("Z"))

    def test_deviate_command_n_replaces_that_command(self):
        nid = self._node_with(commands=["echo a", "echo b"])
        code, output = self.cli("deviate", nid, "--command", "1", "--to", "echo c",
                                "--reason", "fix")
        self.assertEqual(code, 0, output)
        node = self.node(nid)
        self.assertEqual(node["commands"], ["echo a", "echo c"])
        self.assertEqual(node["deviations"][0]["target"], "command:1")

    def test_deviate_out_of_range_command_exits_2(self):
        nid = self._node_with(commands=["echo a"])
        code, output = self.cli("deviate", nid, "--command", "3", "--to", "echo c",
                                "--reason", "fix")
        self.assertEqual(code, 2, output)
        self.assertNotIn("deviations", self.node(nid))

    def test_deviate_same_text_exits_2(self):
        nid = self._node_with(commands=["echo a"])
        code, output = self.cli("deviate", nid, "--command", "0", "--to", "echo a",
                                "--reason", "fix")
        self.assertEqual(code, 2, output)

    def test_deviate_check_on_non_command_check_exits_2(self):
        nid = self._node_with(verification={"type": "confirm", "prompt": "ok?"})
        code, output = self.cli("deviate", nid, "--check", "--to", "yes", "--reason", "fix")
        self.assertEqual(code, 2, output)

    def test_deviate_empty_or_blank_to_and_reason_exit_2(self):
        nid = self._node_with(commands=["echo a"])
        for args in (["--to", "  ", "--reason", "fix"], ["--to", "echo b", "--reason", "   "]):
            code, output = self.cli("deviate", nid, "--command", "0", *args)
            self.assertEqual(code, 2, output)
        self.assertNotIn("deviations", self.node(nid))

    def test_deviate_refuses_completed_node_with_reset_hint(self):
        nid = self._node_with(commands=["echo a"], status="completed", commit_sha=None)
        code, output = self.cli("deviate", nid, "--command", "0", "--to", "echo b",
                                "--reason", "fix")
        self.assertEqual(code, 1, output)
        self.assertIn("--reset-descendants", output)
        self.assertEqual(self.node(nid)["commands"], ["echo a"])

    def test_deviate_sudo_makes_tier_high(self):
        nid = self._node_with(commands=["echo a"])
        code, output = self.cli("deviate", nid, "--command", "0", "--to", "sudo true",
                                "--reason", "needs root")
        self.assertEqual(code, 0, output)
        self.assertIn("risk: high", output)

    def test_hands_off_complete_blocks_deviated_node_until_approved(self):
        root = self._root_with("hands-off", commands=["echo a"])
        self.cli("deviate", root, "--command", "0", "--to", "echo b", "--reason", "r", check=0)
        code, output = self.cli("complete", root, "--no-verify")
        self.assertEqual(code, 1, output)
        self.assertIn("approve {}".format(root), output)
        self.assertIn("deviated", output)
        self.assertNotEqual(self.node(root)["status"], "completed")

        self.cli("approve", root, check=0)
        self.cli("complete", root, "--no-verify", check=0)
        self.assertEqual(self.node(root)["status"], "completed")

    def test_second_deviation_reblocks_after_approval(self):
        root = self._root_with("hands-off", commands=["echo a"])
        self.cli("deviate", root, "--command", "0", "--to", "echo b", "--reason", "r", check=0)
        self.cli("approve", root, check=0)
        self.cli("deviate", root, "--command", "0", "--to", "echo c", "--reason", "r2", check=0)
        code, output = self.cli("complete", root, "--no-verify")
        self.assertEqual(code, 1, output)
        self.assertIn("approve {}".format(root), output)

        self.cli("approve", root, check=0)
        self.cli("complete", root, "--no-verify", check=0)

    def test_guided_never_blocks_deviated_completion(self):
        root = self._root_with("guided", commands=["echo a"])
        self.cli("deviate", root, "--command", "0", "--to", "echo b", "--reason", "r", check=0)
        self.cli("complete", root, "--no-verify", check=0)
        self.assertEqual(self.node(root)["status"], "completed")

    def test_assisted_never_blocks_deviated_completion(self):
        root = self._root_with("assisted", commands=["echo a"])
        self.cli("deviate", root, "--command", "0", "--to", "echo b", "--reason", "r", check=0)
        self.cli("complete", root, "--no-verify", check=0)
        self.assertEqual(self.node(root)["status"], "completed")

    def test_status_shows_deviation_count(self):
        root = self._root_with("guided", commands=["echo a"])
        self.cli("deviate", root, "--command", "0", "--to", "echo b", "--reason", "r", check=0)
        self.cli("next", root, check=0)
        code, output = self.cli("status", check=0)
        self.assertIn("  deviations: 1", output)
        self.assertIn("deviations  {}: 1".format(root), output)

    def test_approved_deviations_must_be_non_negative_int(self):
        root = self._root_with("guided")
        for value, ok in ((-1, False), ("1", False), (0, True), (2, True)):
            with self.subTest(value=value):
                state = self.state()
                self.node(root, state)["approved"] = {
                    "tier": "low", "at": "2026-11-26T00:00:00Z", "deviations": value,
                }
                self.state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
                code, output = self.cli("validate")
                if ok:
                    self.assertEqual(code, 0, output)
                else:
                    self.assertEqual(code, 1, output)
                    self.assertIn("approved.deviations", output)

    def test_validate_rejects_target_with_trailing_newline(self):
        self._node_with(commands=["echo a"], deviations=[{
            "target": "check\n", "original": "a", "replacement": "b",
            "reason": "r", "recorded_at": "2026-11-26T00:00:00Z",
        }])
        code, output = self.cli("validate")
        self.assertEqual(code, 1, output)

    # -- export (C-G8, 6.1) ----------------------------------------------

    def _export_node(self, source_doc, commands=None):
        nid = self._node_with(commands=commands or ["echo a"])
        state = self.state()
        state["source_doc"] = source_doc
        self.state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
        return nid

    def test_export_writes_corrected_tutorial_and_report(self):
        source = self.workspace / "tutorial.md"
        source.write_text("Run ./greet --name Ada\nDone\n", encoding="utf-8")
        nid = self._export_node(str(source), commands=["./greet --name Ada"])
        self.cli("deviate", nid, "--command", "0", "--to", "./greet --who Ada",
                 "--reason", "v2 renamed --name", check=0)
        code, output = self.cli("export")
        self.assertEqual(code, 0, output)
        out = self.workspace / ".tutorial-export"
        corrected = (out / "tutorial.corrected.md").read_text(encoding="utf-8")
        self.assertIn("./greet --who Ada", corrected)
        self.assertNotIn("./greet --name Ada", corrected)
        report = (out / "deviations.md").read_text(encoding="utf-8")
        self.assertIn("v2 renamed --name", report)
        self.assertIn(nid, report)
        self.assertIn("tutorial.corrected.md", output)
        self.assertIn("deviations.md", output)

    def test_export_removes_stale_corrected_file_when_source_unreadable(self):
        source = self.workspace / "tutorial.md"
        source.write_text("Run ./greet --name Ada\n", encoding="utf-8")
        nid = self._export_node(str(source), commands=["./greet --name Ada"])
        self.cli("deviate", nid, "--command", "0", "--to", "./greet --who Ada",
                 "--reason", "v2 renamed --name", check=0)
        self.cli("export", check=0)
        out = self.workspace / ".tutorial-export"
        self.assertTrue((out / "tutorial.corrected.md").exists())
        source.unlink()
        code, output = self.cli("export")
        self.assertEqual(code, 0, output)
        self.assertFalse((out / "tutorial.corrected.md").exists())
        report = (out / "deviations.md").read_text(encoding="utf-8")
        self.assertIn("v2 renamed --name", report)

    def test_export_without_deviations_reports_none_and_no_corrected_file(self):
        source = self.workspace / "tutorial.md"
        source.write_text("Run echo a\n", encoding="utf-8")
        self._export_node(str(source))
        code, output = self.cli("export")
        self.assertEqual(code, 0, output)
        out = self.workspace / ".tutorial-export"
        self.assertIn("No deviations recorded.",
                      (out / "deviations.md").read_text(encoding="utf-8"))
        self.assertFalse((out / "tutorial.corrected.md").exists())

    def test_export_with_url_source_writes_report_only(self):
        nid = self._export_node("https://example.invalid/tutorial.md")
        self.cli("deviate", nid, "--command", "0", "--to", "echo b", "--reason", "fix", check=0)
        code, output = self.cli("export")
        self.assertEqual(code, 0, output)
        out = self.workspace / ".tutorial-export"
        self.assertTrue((out / "deviations.md").exists())
        self.assertFalse((out / "tutorial.corrected.md").exists())

    def test_export_lists_unmatched_originals_under_not_applied(self):
        source = self.workspace / "tutorial.md"
        source.write_text("Step two runs echo c\n", encoding="utf-8")
        nid = self._export_node(str(source), commands=["echo a", "echo c"])
        self.cli("deviate", nid, "--command", "0", "--to", "echo x", "--reason", "r1", check=0)
        self.cli("deviate", nid, "--command", "1", "--to", "echo d", "--reason", "r2", check=0)
        code, output = self.cli("export")
        self.assertEqual(code, 0, output)
        out = self.workspace / ".tutorial-export"
        corrected = (out / "tutorial.corrected.md").read_text(encoding="utf-8")
        self.assertIn("echo d", corrected)
        report = (out / "deviations.md").read_text(encoding="utf-8")
        self.assertIn("## Not applied", report)
        self.assertIn("echo a", report.split("## Not applied", 1)[1])

    def test_export_honours_out_dir_relative_and_absolute(self):
        nid = self._export_node("https://example.invalid/t.md")
        self.cli("deviate", nid, "--command", "0", "--to", "echo b", "--reason", "fix", check=0)
        self.cli("export", "--out", "rel/dir", check=0)
        self.assertTrue((self.workspace / "rel" / "dir" / "deviations.md").exists())
        absolute = self.workspace / "abs-out"
        self.cli("export", "--out", str(absolute), check=0)
        self.assertTrue((absolute / "deviations.md").exists())
        self.assertFalse((self.workspace / ".tutorial-export").exists())

    def test_export_default_dir_is_git_excluded(self):
        nid = self._export_node("https://example.invalid/t.md")
        self.cli("deviate", nid, "--command", "0", "--to", "echo b", "--reason", "fix", check=0)
        self.cli("export", check=0)
        exclude = (self.workspace / ".git" / "info" / "exclude").read_text(encoding="utf-8")
        self.assertIn(".tutorial-export/", exclude.splitlines())
        self.assertNotIn(".tutorial-export", self.git("status", "--porcelain"))

    def test_export_report_metadata_topological_nodes_and_empty_sections(self):
        self.cli("init", "--title", "Report Demo", "--source", "guide.md",
                 "--mode", "assisted", "--persistence", "file", check=0)
        state = self.state()
        state["nodes"] = [
            {
                "id": "2-second",
                "title": "Second step",
                "summary": "Depends on first.",
                "dependencies": ["1-first"],
                "action_type": "read-only",
                "status": "pending",
                "commit_sha": None,
                "artifacts": [],
                "commands": [],
                "verification": {"type": "confirm", "prompt": "ok?"},
            },
            {
                "id": "3-third",
                "title": "Third step",
                "summary": "Depends on second.",
                "dependencies": ["2-second"],
                "action_type": "read-only",
                "status": "pending",
                "commit_sha": None,
                "artifacts": [],
                "commands": [],
                "verification": {"type": "confirm", "prompt": "ok?"},
            },
            {
                "id": "1-first",
                "title": "First step",
                "summary": "Creates out.txt.",
                "dependencies": [],
                "action_type": "mutate",
                "status": "pending",
                "commit_sha": None,
                "artifacts": ["out.txt"],
                "commands": ["touch out.txt"],
                "verification": {"type": "file_exists", "path": "out.txt"},
            },
        ]
        self.state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
        self.cli("next", "1-first", check=0)
        (self.workspace / "out.txt").write_text("hi\n", encoding="utf-8")
        self.cli("complete", "1-first", "--commit", check=0)
        sha = self.node("1-first")["commit_sha"]
        self.assertIsNotNone(sha)
        self.cli("jump", "3-third", "--skip-ancestors", check=0)

        code, output = self.cli("export")
        self.assertEqual(code, 0, output)
        self.assertIn("report.md", output)
        report = (self.workspace / ".tutorial-export" / "report.md").read_text(encoding="utf-8")

        self.assertIn("# Report Demo", report)
        self.assertIn("source_doc: `guide.md`", report)
        self.assertIn("mode: `assisted`", report)
        self.assertIn("persistence: `file`", report)
        self.assertIn("git.branch: `main`", report)
        self.assertIn("1 completed / 1 skipped / 3 total", report)
        self.assertNotIn("## Preflight", report)
        self.assertNotIn("## Visual Artifacts", report)

        nodes_section = report.split("## Nodes", 1)[1].split("## Deviations", 1)[0]
        pos1 = nodes_section.index("| 1-first |")
        pos2 = nodes_section.index("| 2-second |")
        pos3 = nodes_section.index("| 3-third |")
        self.assertLess(pos1, pos2)
        self.assertLess(pos2, pos3)
        self.assertIn(
            "| 1-first | First step | COMPLETED | {} | out.txt | 0 |".format(sha[:7]),
            nodes_section,
        )
        self.assertIn("| 2-second | Second step | SKIPPED | \u2014 | \u2014 | 0 |", nodes_section)
        self.assertIn("| 3-third | Third step | ACTIVE | \u2014 | \u2014 | 0 |", nodes_section)

        deviations_section = report.split("## Deviations", 1)[1].split("## Residue", 1)[0]
        residue_section = report.split("## Residue", 1)[1]
        self.assertIn("None.", deviations_section)
        self.assertIn("None.", residue_section)

    def test_export_report_visual_artifacts_relative_embeds(self):
        self.cli("init", "--title", "Visuals", "--source", "v.md", check=0)
        (self.workspace / "docs").mkdir()
        (self.workspace / "notes").mkdir()
        for rel in ("docs/arch.png", "docs/diagram.SVG", "notes/photo.jpg", "notes/pic.jpeg", "README.txt"):
            (self.workspace / rel).write_bytes(b"data")
        state = self.state()
        state["nodes"] = [
            {
                "id": "1-draw",
                "title": "Draw",
                "summary": "Create images.",
                "dependencies": [],
                "action_type": "mutate",
                "status": "completed",
                "commit_sha": None,
                "artifacts": ["docs/arch.png", "docs/diagram.SVG", "docs/missing.png", "README.txt"],
                "commands": [],
                "verification": {"type": "confirm", "prompt": "ok?"},
            },
            {
                "id": "2-more",
                "title": "More",
                "summary": "Add photos.",
                "dependencies": ["1-draw"],
                "action_type": "mutate",
                "status": "pending",
                "commit_sha": None,
                "artifacts": ["docs/arch.png", "notes/photo.jpg", "notes/pic.jpeg"],
                "commands": [],
                "verification": {"type": "confirm", "prompt": "ok?"},
            },
        ]
        self.state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")

        self.cli("export", check=0)
        report = (self.workspace / ".tutorial-export" / "report.md").read_text(encoding="utf-8")
        self.assertIn("## Visual Artifacts", report)
        visual_section = report.split("## Visual Artifacts", 1)[1].split("## Deviations", 1)[0]
        self.assertIn("![docs/arch.png](../docs/arch.png)", visual_section)
        self.assertIn("![docs/diagram.SVG](../docs/diagram.SVG)", visual_section)
        self.assertIn("![notes/photo.jpg](../notes/photo.jpg)", visual_section)
        self.assertIn("![notes/pic.jpeg](../notes/pic.jpeg)", visual_section)
        self.assertEqual(visual_section.count("docs/arch.png"), 2)  # alt + link once
        self.assertNotIn("missing.png", visual_section)
        self.assertNotIn("README.txt", visual_section)

        self.cli("export", "--out", "custom/nested", check=0)
        nested_report = (self.workspace / "custom" / "nested" / "report.md").read_text(encoding="utf-8")
        self.assertIn("![docs/arch.png](../../docs/arch.png)", nested_report)

    def test_export_report_preflight_deviations_and_residue(self):
        nid = self._export_node("https://example.invalid/t.md", commands=["echo old"])
        state = self.state()
        state["requires"] = {
            "tools": {"python3": ">=3.0", "zz-missing-tool-9": "*"},
            "os": ["linux", "darwin", "windows"],
        }
        state["residue"] = [
            {
                "node_id": nid,
                "scopes": ["user"],
                "undo": ["rm ~/.demo"],
                "reason": "not-run",
                "recorded_at": "2026-11-27T00:00:00Z",
            }
        ]
        self.state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
        code, _ = self.cli("doctor")
        self.assertEqual(code, 1)
        self.cli("deviate", nid, "--command", "0", "--to", "echo new", "--reason", "updated", check=0)

        self.cli("export", check=0)
        report = (self.workspace / ".tutorial-export" / "report.md").read_text(encoding="utf-8")

        self.assertIn("## Preflight", report)
        preflight_section = report.split("## Preflight", 1)[1].split("## Nodes", 1)[0]
        self.assertIn("status: failed", preflight_section)
        self.assertIn("missing: zz-missing-tool-9", preflight_section)
        self.assertIn("| python3 | ok |", preflight_section)
        self.assertIn("| zz-missing-tool-9 | missing |", preflight_section)
        self.assertIn("| os | ok |", preflight_section)

        nodes_section = report.split("## Nodes", 1)[1].split("## Deviations", 1)[0]
        self.assertIn("| {} |".format(nid), nodes_section)
        self.assertTrue(any(
            line.startswith("| {} |".format(nid)) and line.endswith("| 1 |")
            for line in nodes_section.splitlines()
        ))

        deviations_section = report.split("## Deviations", 1)[1].split("## Residue", 1)[0]
        self.assertNotIn("None.", deviations_section)
        self.assertIn("`{}` (`command:0`): `echo old` -> `echo new`".format(nid), deviations_section)
        self.assertIn("updated", deviations_section)

        residue_section = report.split("## Residue", 1)[1]
        self.assertNotIn("None.", residue_section)
        self.assertIn("`{}` [user]: not-run (undo: rm ~/.demo)".format(nid), residue_section)

    def test_hands_off_high_risk_active_node_needs_approval_to_complete(self):
        root = self._root_with("hands-off", commands=["sudo true"])
        state = self.state()
        state["active_node_id"] = root
        self.state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
        before_state = self.state_path.read_text(encoding="utf-8")
        before_head = self.git("rev-parse", "HEAD")
        code, output = self.cli("complete", root)
        self.assertEqual(code, 1, output)
        self.assertIn("approve {}".format(root), output)
        self.assertIn("is high risk", output)
        self.assertEqual(self.state_path.read_text(encoding="utf-8"), before_state)
        self.assertEqual(self.git("rev-parse", "HEAD"), before_head)

        self.cli("approve", root, check=0)
        self.cli("complete", root, "--no-verify", check=0)
        self.assertEqual(self.node(root)["status"], "completed")

    def test_validate_rejects_bad_deviation_target(self):
        self._node_with(commands=["echo a"], deviations=[{
            "target": "check2", "original": "a", "replacement": "b",
            "reason": "r", "recorded_at": "2026-11-26T00:00:00Z",
        }])
        code, output = self.cli("validate")
        self.assertEqual(code, 1, output)
        self.assertIn("deviations", output)


def load_cli_module():
    """Import scripts/dag_manager.py so pure helpers can be tested directly."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("dag_manager", CLI)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class RiskTierTest(unittest.TestCase):
    """Computed and declared risk tiers (C-G6)."""

    @classmethod
    def setUpClass(cls):
        cls.dm = load_cli_module()

    def tier(self, **node):
        node.setdefault("id", "n")
        return self.dm.risk_tier(node)

    def test_each_high_pattern_matches_and_near_misses_stay_low(self):
        cases = {
            "sudo": ("sudo usermod -aG docker x", "pseudo-tool --x", "sudo"),
            "pipe-to-shell": (
                "curl -fsSL https://example.invalid/i.sh | bash",
                "curl -fsSL https://example.invalid/i.sh -o i.sh",
                "pipe-to-shell",
            ),
            "recursive-delete": ("rm -rf /", "rm -rf build/", "recursive-delete"),
            "chmod-777": ("chmod -R 777 /srv", "chmod 755 bin/tool", "chmod-777"),
            "system-path-write": (
                "echo x > /etc/app.conf",
                "echo x > ./etc/app.conf",
                "system-path-write",
            ),
            "mkfs": ("mkfs.ext4 /dev/sdb1", "make fs-check", "mkfs"),
            "dd": ("dd if=/dev/zero of=disk.img bs=1M count=1", "dd-notes.txt", "dd"),
        }
        for label, (hit, miss, expected) in cases.items():
            tier, labels = self.tier(commands=[hit])
            self.assertEqual(("high", [expected]), (tier, labels), label)
            tier, labels = self.tier(commands=[miss])
            self.assertEqual(("low", []), (tier, labels), label)

    def test_high_pattern_is_read_from_undo_and_command_checks(self):
        self.assertEqual("high", self.tier(undo=["sudo rm x"])[0])
        verification = {"type": "command", "command": "sudo true"}
        self.assertEqual("high", self.tier(verification=verification)[0])
        other = {"type": "confirm", "prompt": "sudo said yes?"}
        self.assertEqual("low", self.tier(verification=other)[0])

    def test_scope_raises_to_elevated_and_declared_risk_never_lowers(self):
        self.assertEqual("low", self.tier(commands=["ls"])[0])
        self.assertEqual("elevated", self.tier(side_effects=["user"])[0])
        self.assertEqual("elevated", self.tier(side_effects=["repo", "remote"])[0])
        self.assertEqual("high", self.tier(commands=["sudo ls"], risk="low")[0])
        self.assertEqual("high", self.tier(commands=["ls"], risk="high")[0])
        self.assertEqual("elevated", self.tier(side_effects=["user"], risk="low")[0])

    def test_validate_rejects_bad_risk_and_approved_shapes(self):
        validate = self.dm.validate_node
        for node in (
            {"id": "a", "risk": "critical"},
            {"id": "a", "approved": {"tier": "high"}},
            {"id": "a", "approved": {"tier": "urgent", "at": "2026-11-26T00:00:00Z"}},
        ):
            problems = [p for p in validate(dict(node, title="t", summary="s",
                        dependencies=[], action_type="instruct", status="pending",
                        commit_sha=None, artifacts=[], commands=[],
                        verification={"type": "confirm", "prompt": "ok"}), 0, {})]
            self.assertTrue(any("'risk'" in p or "'approved'" in p for p in problems), node)


class InteractiveStepsTest(unittest.TestCase):
    """Interactive detection and command-check stdin/timeout (C-G11)."""

    @classmethod
    def setUpClass(cls):
        cls.dm = load_cli_module()

    def matches(self, command):
        return any(pattern.search(command) for pattern in self.dm.INTERACTIVE_PATTERNS)

    def test_each_interactive_pattern_has_a_positive_and_a_negative(self):
        positives = [
            "npm init",
            "yarn init",
            "npx create-react-app app",
            "vim notes.md",
            "python3",
            "node",
            "git rebase -i HEAD~2",
            "git add -i",
            "ssh-keygen -t ed25519",
            "passwd",
            "read name",
        ]
        negatives = [
            "npm init -y",
            "npx create-react-app app --yes",
            "view notes.md",
            "python3 -m http.server 8000",
            "git rebase main",
            "ssh-keygen -t ed25519 -N '' -f key",
            "echo thread",
            "cat notes.md",
        ]
        for command in positives:
            self.assertTrue(self.matches(command), command)
        for command in negatives:
            self.assertFalse(self.matches(command), command)

    def test_command_check_gets_declared_stdin_and_defaults_to_eof(self):
        fed = {
            "type": "command",
            "command": "python3 -c \"import sys; assert sys.stdin.read().strip() == 'yes'\"",
            "stdin": "yes\n",
        }
        passed, lines = self.dm.run_verification({"id": "n", "verification": fed}, Path("."), False)
        self.assertTrue(passed, lines)
        eof = {
            "type": "command",
            "command": "python3 -c \"import sys; sys.exit(0 if sys.stdin.read() == '' else 1)\"",
        }
        passed, lines = self.dm.run_verification({"id": "n", "verification": eof}, Path("."), False)
        self.assertTrue(passed, lines)

    def test_command_check_honours_its_own_timeout(self):
        slow = {"type": "command", "command": "sleep 3", "timeout": 1}
        passed, lines = self.dm.run_verification({"id": "n", "verification": slow}, Path("."), False)
        self.assertFalse(passed)
        self.assertIn("timed out after 1s", "\n".join(lines))


class PreflightTest(unittest.TestCase):
    """Version parsing, comparison, overrides, and OS checks for doctor (C-G4)."""

    @classmethod
    def setUpClass(cls):
        cls.dm = load_cli_module()

    def test_version_is_the_first_dotted_number_in_the_output(self):
        match = self.dm.VERSION_FIND_RE.search("Docker version 24.0.7, build afdd53b")
        self.assertEqual("24.0.7", match.group(0))
        self.assertEqual((24, 0, 7), self.dm.version_tuple("24.0.7"))
        self.assertEqual((3, 0, 0), self.dm.version_tuple("3"))

    def test_comparison_pads_short_versions(self):
        ok = self.dm.check_tool("py-ok", {"min": "3.0", "version_command": "echo 'v3.0.0'"})
        self.assertEqual("ok", ok["status"])
        old = self.dm.check_tool("py-old", {"min": "3.11", "version_command": "echo 'v3.10.9'"})
        self.assertEqual("too-old", old["status"])

    def test_version_command_override_is_used(self):
        row = self.dm.check_tool(
            "anything", {"min": "2.0", "version_command": "echo 'tool 2.4.1'"}
        )
        self.assertEqual(("ok", "2.4.1"), (row["status"], row["found"]))

    def test_star_passes_a_tool_that_runs_even_without_a_version(self):
        row = self.dm.check_tool("zz-missing-tool-9", "*")
        self.assertEqual("missing", row["status"])
        self.assertFalse(row["ok"])
        row = self.dm.check_tool("quiet", {"min": "1.0", "version_command": "true"})
        self.assertEqual("unknown-version", row["status"])
        self.assertFalse(row["ok"])

    def test_os_mismatch_fails_the_run(self):
        passed, rows, missing = self.dm.run_checks({"os": ["plan9"]})
        self.assertFalse(passed)
        self.assertEqual([], missing)
        self.assertEqual("mismatch", rows[-1]["status"])

    def test_validate_tool_spec_shapes(self):
        for good in ("*", ">=3", ">=3.0.1", {"min": "1.2"}, {"min": "1", "version_command": "x"}):
            self.assertEqual([], self.dm.validate_tool_spec(good, "t"), good)
        for bad in ("3.0", ">3", {"min": "x"}, {"max": "1"}, 7):
            self.assertTrue(self.dm.validate_tool_spec(bad, "t"), bad)


class VerificationTypesTest(unittest.TestCase):
    """http and port_listening checks against loopback services (C-G3)."""

    @classmethod
    def setUpClass(cls):
        import http.server
        import threading

        cls.dm = load_cli_module()

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                code = 200 if self.path == "/" else 404
                body = b"hello from the test server"
                self.send_response(code)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.port = cls.server.server_address[1]
        thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        thread.start()
        cls.addClassCleanup(cls.server.server_close)
        cls.addClassCleanup(cls.server.shutdown)

    def run_check(self, verification):
        node = {"id": "n", "verification": verification}
        return self.dm.run_verification(node, Path("."), False)

    def closed_port(self):
        import socket

        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            return probe.getsockname()[1]

    def test_http_passes_on_expected_status_and_pattern(self):
        base = "http://127.0.0.1:{}".format(self.port)
        passed, _ = self.run_check({"type": "http", "url": base + "/"})
        self.assertTrue(passed)
        passed, _ = self.run_check({"type": "http", "url": base + "/missing", "expect_status": 404})
        self.assertTrue(passed)
        passed, _ = self.run_check({"type": "http", "url": base + "/", "pattern": "hello"})
        self.assertTrue(passed)

    def test_http_fails_on_status_mismatch_pattern_miss_and_closed_port(self):
        base = "http://127.0.0.1:{}".format(self.port)
        passed, lines = self.run_check({"type": "http", "url": base + "/missing"})
        self.assertFalse(passed)
        self.assertIn("expected 200", "\n".join(lines))
        passed, lines = self.run_check({"type": "http", "url": base + "/", "pattern": "absent"})
        self.assertFalse(passed)
        self.assertIn("did not match", "\n".join(lines))
        passed, _ = self.run_check(
            {"type": "http", "url": "http://127.0.0.1:{}/".format(self.closed_port()), "timeout": 2}
        )
        self.assertFalse(passed)

    def test_port_listening_passes_on_open_port_and_fails_on_closed_one(self):
        import socket

        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            passed, _ = self.run_check({"type": "port_listening", "port": listener.getsockname()[1]})
            self.assertTrue(passed)
        passed, lines = self.run_check({"type": "port_listening", "port": self.closed_port()})
        self.assertFalse(passed)
        self.assertIn("nothing accepts connections", "\n".join(lines))

    def test_validate_accepts_the_new_shapes(self):
        ok = [
            {"type": "http", "url": "http://127.0.0.1:8000/"},
            {"type": "http", "url": "https://example.invalid/", "expect_status": 301, "pattern": "x", "timeout": 3},
            {"type": "port_listening", "port": 8000},
            {"type": "port_listening", "port": 65535, "host": "127.0.0.1", "timeout": 1},
        ]
        for verification in ok:
            self.assertEqual([], self.dm.validate_verification(verification, "node 'n'"), verification)

    def test_validate_rejects_bad_scheme_port_status_and_fields(self):
        bad = [
            {"type": "http", "url": "ftp://example.invalid/"},
            {"type": "http", "url": "http://127.0.0.1/", "expect_status": 42},
            {"type": "http", "url": "http://127.0.0.1/", "pattern": "("},
            {"type": "http", "url": "http://127.0.0.1/", "host": "x"},
            {"type": "port_listening"},
            {"type": "port_listening", "port": "8000"},
            {"type": "port_listening", "port": 0},
            {"type": "port_listening", "port": 65536},
            {"type": "port_listening", "port": True},
            {"type": "port_listening", "port": 80, "timeout": 0},
        ]
        for verification in bad:
            self.assertTrue(self.dm.validate_verification(verification, "node 'n'"), verification)


class DraftTest(unittest.TestCase):
    """`draft <path>` turns a Markdown tutorial into skeleton nodes (C-G10)."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="draft-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.env = dict(os.environ)
        self.env.update({"HOME": str(self.tmp), "GIT_CONFIG_GLOBAL": os.devnull,
                         "GIT_CONFIG_SYSTEM": os.devnull})

    def draft(self, text, name="tutorial.md"):
        path = self.tmp / name
        path.write_text(text, encoding="utf-8")
        proc = subprocess.run(
            [sys.executable, str(CLI), "draft", str(path)],
            cwd=str(self.tmp), env=self.env, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, universal_newlines=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout)

    def test_headings_become_linear_nodes_with_source_ref(self):
        doc = self.draft("# Hello Tutorial\n\nIntro text.\n\n## Install tool\n\nGet it.\n\n"
                         "### Check it\n\nRun the check.\n")
        self.assertEqual(doc["title"], "Hello Tutorial")
        nodes = doc["nodes"]
        self.assertEqual([n["id"] for n in nodes], ["1-install-tool", "2-check-it"])
        self.assertEqual(nodes[0]["dependencies"], [])
        self.assertEqual(nodes[1]["dependencies"], ["1-install-tool"])
        self.assertEqual(nodes[0]["source_ref"], {"line": 5, "heading": "Install tool"})
        self.assertEqual(nodes[1]["source_ref"], {"line": 9, "heading": "Check it"})
        self.assertEqual(nodes[0]["summary"], "Get it.")
        self.assertEqual(nodes[0]["artifacts"], [])
        self.assertEqual(nodes[0]["status"], "pending")
        self.assertIsNone(nodes[0]["commit_sha"])
        self.assertEqual(nodes[0]["verification"]["type"], "confirm")
        self.assertTrue(nodes[0]["verification"]["prompt"].startswith("DRAFT: replace with a real check for "))

    def test_title_falls_back_to_file_stem(self):
        self.assertEqual(self.draft("## Only\n", name="my-guide.md")["title"], "my-guide")

    def test_text_before_first_section_is_ignored(self):
        doc = self.draft("preamble line\n\n## A step\n")
        self.assertEqual([n["title"] for n in doc["nodes"]], ["A step"])

    def test_numbered_list_fallback(self):
        doc = self.draft("# Title\n\nIntro.\n\n1. First thing\n2. Second thing\n")
        self.assertEqual([n["title"] for n in doc["nodes"]], ["First thing", "Second thing"])
        self.assertEqual(doc["nodes"][1]["source_ref"], {"line": 6, "heading": "Second thing"})
        self.assertEqual(doc["nodes"][1]["dependencies"], ["1-first-thing"])

    def test_indented_sh_fence_under_numbered_item(self):
        text = ("1. Make a file\n\n   ```sh\n   $ echo one\n   echo two\n   ```\n\n"
                "2. Done\n")
        nodes = self.draft(text)["nodes"]
        self.assertEqual(nodes[0]["action_type"], "execute")
        self.assertEqual(nodes[0]["commands"], ["echo one\necho two"])
        self.assertEqual(nodes[0]["title"], "Make a file")

    def test_shell_fence_is_execute_with_prompts_stripped(self):
        doc = self.draft("## Run\n\n```bash\n$ make all\nmake test\n```\n\n```sh\nls\n```\n")
        node = doc["nodes"][0]
        self.assertEqual(node["action_type"], "execute")
        self.assertEqual(node["commands"], ["make all\nmake test", "ls"])

    def test_python_fence_is_mutate_without_commands(self):
        node = self.draft("## Code\n\n```python\nprint(1)\n```\n")["nodes"][0]
        self.assertEqual(node["action_type"], "mutate")
        self.assertEqual(node["commands"], [])

    def test_empty_info_fence_without_prompts_is_mutate(self):
        node = self.draft("## Blob\n\n```\nraw text\n```\n")["nodes"][0]
        self.assertEqual(node["action_type"], "mutate")
        self.assertEqual(node["commands"], [])

    def test_no_fences_is_read_only(self):
        node = self.draft("## Read\n\nJust prose.\n")["nodes"][0]
        self.assertEqual(node["action_type"], "read-only")
        self.assertEqual(node["commands"], [])

    def test_headings_inside_fences_are_not_headings(self):
        text = "## Real\n\n```md\n## Fake\n```\n\n~~~\n# Also fake\n~~~\n"
        self.assertEqual([n["title"] for n in self.draft(text)["nodes"]], ["Real"])

    def test_unclosed_fence_runs_to_eof(self):
        text = "## Real\n\n```sh\necho hi\n## Hidden\nstill code\n"
        nodes = self.draft(text)["nodes"]
        self.assertEqual([n["title"] for n in nodes], ["Real"])
        self.assertEqual(nodes[0]["commands"], ["echo hi\n## Hidden\nstill code"])

    def test_symbol_only_heading_gets_step_slug(self):
        self.assertEqual(self.draft("## !!! ???\n")["nodes"][0]["id"], "1-step")

    def test_long_slug_is_truncated_without_trailing_dash(self):
        heading = "## " + "abcdefghij " * 5  # slug would be 54 chars
        node = self.draft(heading + "\n")["nodes"][0]
        self.assertLessEqual(len(node["id"].split("-", 1)[1]), 40)
        self.assertFalse(node["id"].endswith("-"))
        self.assertRegex(node["id"], dm_id_re())

    def test_duplicate_headings_get_distinct_ids(self):
        ids = [n["id"] for n in self.draft("## Same\n\n## Same\n")["nodes"]]
        self.assertEqual(ids, ["1-same", "2-same"])

    def test_every_id_matches_id_re(self):
        text = "## Ünïcode & Symbols!\n\n## Step: two (2)\n\n## 3. Three\n"
        for node in self.draft(text)["nodes"]:
            self.assertRegex(node["id"], dm_id_re())

    def test_missing_file_exits_1(self):
        proc = subprocess.run(
            [sys.executable, str(CLI), "draft", str(self.tmp / "nope.md")],
            cwd=str(self.tmp), env=self.env, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, universal_newlines=True,
        )
        self.assertEqual(proc.returncode, 1)
        self.assertIn("nope.md", proc.stderr)

    def test_summary_collapses_whitespace_and_truncates(self):
        long_para = "word " * 80  # > 200 chars after collapsing
        node = self.draft("## Long\n\n" + long_para + "\n")["nodes"][0]
        self.assertLessEqual(len(node["summary"]), 200)
        self.assertTrue(node["summary"].endswith("..."))
        short = self.draft("## Short\n\none   two\nthree\n")["nodes"][0]
        self.assertEqual(short["summary"], "one two three")

    def test_summary_falls_back_to_title_without_paragraph(self):
        self.assertEqual(self.draft("## Bare\n\n```sh\nls\n```\n")["nodes"][0]["summary"], "Bare")


class DraftWriteTest(unittest.TestCase):
    """`draft <path> --write` fills an empty state; validate checks source_ref (C-G10)."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="draft-write-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.env = dict(os.environ)
        self.env.update({"HOME": str(self.tmp), "GIT_CONFIG_GLOBAL": os.devnull,
                         "GIT_CONFIG_SYSTEM": os.devnull, "GIT_ADVICE_ENABLED": "false",
                         "GIT_AUTHOR_NAME": "Tutorial Test",
                         "GIT_AUTHOR_EMAIL": "tutorial@example.invalid",
                         "GIT_COMMITTER_NAME": "Tutorial Test",
                         "GIT_COMMITTER_EMAIL": "tutorial@example.invalid"})
        self.ws = self.tmp / "repo"
        self.ws.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=str(self.ws), env=self.env,
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        (self.ws / "tutorial.md").write_text("# Hello\n\n## Install tool\n\nGet it.\n\n"
                                             "## Check it\n\nRun it.\n", encoding="utf-8")
        self.state_path = self.ws / ".tutorial-dag.json"

    def cli(self, *args):
        """Run the CLI in the workspace; returns (exit code, stdout, stderr)."""
        proc = subprocess.run(
            [sys.executable, str(CLI)] + list(args), cwd=str(self.ws), env=self.env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True,
        )
        return proc.returncode, proc.stdout, proc.stderr

    def init(self):
        code, out, err = self.cli("init", "--title", "Hello", "--source", "tutorial.md")
        self.assertEqual(code, 0, out + err)

    def state(self):
        return json.loads(self.state_path.read_text(encoding="utf-8"))

    def write_state(self, state):
        self.state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")

    def test_write_fills_empty_nodes_and_prints_header_only(self):
        self.init()
        code, out, err = self.cli("draft", "tutorial.md", "--write")
        self.assertEqual(code, 0, out + err)
        self.assertTrue(out.startswith("["), out)
        self.assertNotIn('"nodes"', out)
        self.assertEqual([n["id"] for n in self.state()["nodes"]],
                         ["1-install-tool", "2-check-it"])
        code, out, err = self.cli("validate")
        self.assertEqual(code, 0, out + err)

    def test_write_refuses_non_empty_nodes(self):
        self.init()
        self.assertEqual(self.cli("draft", "tutorial.md", "--write")[0], 0)
        before = self.state_path.read_text(encoding="utf-8")
        code, out, err = self.cli("draft", "tutorial.md", "--write")
        self.assertEqual(code, 1, out + err)
        self.assertIn("nodes already present", out + err)
        self.assertEqual(self.state_path.read_text(encoding="utf-8"), before)

    def test_write_without_state_file_is_missing_state_error(self):
        code, out, err = self.cli("draft", "tutorial.md", "--write")
        self.assertEqual(code, 1, out + err)
        self.assertIn(".tutorial-dag.json", out + err)
        self.assertFalse(self.state_path.exists())

    def test_draft_check_warning_appears_in_validate(self):
        self.init()
        self.assertEqual(self.cli("draft", "tutorial.md", "--write")[0], 0)
        code, out, err = self.cli("validate")
        self.assertEqual(code, 0, out + err)
        self.assertIn("node '1-install-tool' still has a draft check", out + err)

    def test_source_ref_shape_is_validated(self):
        self.init()
        self.assertEqual(self.cli("draft", "tutorial.md", "--write")[0], 0)
        good = {"line": 3, "heading": "Install tool"}
        bad_refs = [
            {"line": 0, "heading": "Install tool"},
            {"line": "3", "heading": "Install tool"},
            dict(good, extra=1),
            {"line": 3},
        ]
        for ref in bad_refs:
            with self.subTest(ref=ref):
                state = self.state()
                state["nodes"][0]["source_ref"] = ref
                self.write_state(state)
                code, out, err = self.cli("validate")
                self.assertEqual(code, 1, out + err)
                self.assertIn("source_ref", out + err)
        state = self.state()
        state["nodes"][0]["source_ref"] = good
        self.write_state(state)
        self.assertEqual(self.cli("validate")[0], 0)


def dm_id_re():
    """Return the ID pattern the CLI uses, read from the script."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("dm_for_tests", str(CLI))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.ID_RE.pattern


class InitFromTest(unittest.TestCase):
    """`init --from <dag.json>` starts a session from a prebuilt DAG (C-G12)."""

    def setUp(self):
        self.ws = Path(tempfile.mkdtemp(prefix="init-from-test-"))
        self.addCleanup(shutil.rmtree, self.ws, ignore_errors=True)
        self.env = dict(os.environ)
        self.env.update({"HOME": str(self.ws), "GIT_CONFIG_GLOBAL": os.devnull,
                         "GIT_CONFIG_SYSTEM": os.devnull,
                         "GIT_AUTHOR_NAME": "Tutorial Test",
                         "GIT_AUTHOR_EMAIL": "tutorial@example.invalid",
                         "GIT_COMMITTER_NAME": "Tutorial Test",
                         "GIT_COMMITTER_EMAIL": "tutorial@example.invalid"})
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=str(self.ws), env=self.env,
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.state_path = self.ws / STATE_NAME
        self.src_dir = Path(tempfile.mkdtemp(prefix="init-from-src-"))
        self.addCleanup(shutil.rmtree, self.src_dir, ignore_errors=True)
        self.source = self.src_dir / "prebuilt.json"

    def cli(self, *args):
        """Run the CLI in the workspace; returns (exit code, stdout+stderr)."""
        proc = subprocess.run(
            [sys.executable, str(CLI)] + list(args), cwd=str(self.ws), env=self.env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True,
        )
        return proc.returncode, proc.stdout

    def example(self):
        return json.loads(EXAMPLE_DAG.read_text(encoding="utf-8"))

    def write_source(self, doc):
        self.source.write_text(json.dumps(doc, indent=2), encoding="utf-8")
        return str(self.source)

    def dirty_source(self):
        """The example DAG with progress, runtime fields and a foreign git block."""
        doc = self.example()
        for i, node in enumerate(doc["nodes"]):
            node["status"] = "completed" if i == 0 else "deviated"
            node["commit_sha"] = "a" * 40
            node["approved"] = True
            node["deviations"] = [{"reason": "old"}]
        doc["services"] = {}
        doc["residue"] = []
        doc["preflight"] = {"ok": True}
        doc["active_node_id"] = doc["nodes"][0]["id"]
        doc["git"] = {"branch": "old-branch", "initial_commit_sha": "deadbeef" * 5,
                      "initial_dirty": True, "exclude_configured": False}
        return doc

    def state(self):
        return json.loads(self.state_path.read_text(encoding="utf-8"))

    def test_from_without_title_takes_title_and_source_from_file(self):
        path = self.write_source(self.example())
        code, out = self.cli("init", "--from", path)
        self.assertEqual(code, 0, out)
        state = self.state()
        self.assertEqual(state["title"], "Build a tiny todo CLI")
        self.assertEqual(state["source_doc"], "examples/sample-tutorial.md")
        self.assertEqual(state["mode"], "assisted")
        self.assertTrue(state["nodes"])
        for node in state["nodes"]:
            self.assertEqual(node["status"], "pending")
            self.assertIsNone(node["commit_sha"])
            self.assertNotIn("approved", node)
            self.assertNotIn("deviations", node)

    def test_from_strips_progress_runtime_fields_and_git_block(self):
        source = self.dirty_source()
        ids = [n["id"] for n in source["nodes"]]
        path = self.write_source(source)
        code, out = self.cli("init", "--from", path)
        self.assertEqual(code, 0, out)
        state = self.state()
        for key in ("services", "residue", "preflight"):
            self.assertNotIn(key, state)
        self.assertIsNone(state["active_node_id"])
        self.assertEqual([n["id"] for n in state["nodes"]], ids)
        for node in state["nodes"]:
            self.assertEqual(node["status"], "pending")
            self.assertIsNone(node["commit_sha"])
            self.assertNotIn("approved", node)
            self.assertNotIn("deviations", node)
        self.assertEqual(state["nodes"][0]["summary"], source["nodes"][0]["summary"])
        self.assertEqual(state["nodes"][0]["commands"], source["nodes"][0]["commands"])
        self.assertNotEqual(state["git"]["branch"], "old-branch")
        self.assertNotEqual(state["git"]["initial_commit_sha"], "deadbeef" * 5)
        self.assertFalse(state["git"]["initial_dirty"])

    def test_from_keeps_source_ref_and_uses_current_schema_version(self):
        source = self.example()
        source["schema_version"] = "1.0"
        source["persistence"] = "in-context"
        source["nodes"][0]["source_ref"] = {"line": 3, "heading": "Install"}
        path = self.write_source(source)
        code, out = self.cli("init", "--from", path)
        self.assertEqual(code, 0, out)
        state = self.state()
        text = CLI.read_text(encoding="utf-8")
        current = text.split('SCHEMA_VERSION = "', 1)[1].split('"', 1)[0]
        self.assertEqual(state["schema_version"], current)
        self.assertEqual(state["persistence"], "file")
        self.assertEqual(state["nodes"][0]["source_ref"], {"line": 3, "heading": "Install"})

    def test_mode_flag_overrides_file_mode(self):
        source = self.example()
        source["mode"] = "hands-off"
        path = self.write_source(source)
        code, out = self.cli("init", "--from", path)
        self.assertEqual(code, 0, out)
        self.assertEqual(self.state()["mode"], "hands-off")
        self.state_path.unlink()
        code, out = self.cli("init", "--from", path, "--mode", "guided")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.state()["mode"], "guided")

    def test_invalid_source_exits_1_and_writes_nothing(self):
        source = self.dirty_source()
        source["mode"] = "bogus"
        path = self.write_source(source)
        code, out = self.cli("init", "--from", path)
        self.assertEqual(code, 1, out)
        self.assertFalse(self.state_path.exists())

    def test_invalid_source_with_git_branch_touches_no_git_state(self):
        source = self.dirty_source()
        source["mode"] = "bogus"
        path = self.write_source(source)
        code, out = self.cli("init", "--from", path, "--git-branch", "tutorial/x")
        self.assertEqual(code, 1, out)
        self.assertFalse(self.state_path.exists())
        probe = subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", "refs/heads/tutorial/x"],
            cwd=str(self.ws), env=self.env, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.assertNotEqual(probe.returncode, 0, "branch tutorial/x was created")
        exclude = (self.ws / ".git" / "info" / "exclude")
        self.assertFalse(exclude.exists() and STATE_NAME in exclude.read_text(encoding="utf-8"))

    def test_missing_title_in_file_exits_1_with_hint(self):
        source = self.example()
        del source["title"]
        path = self.write_source(source)
        code, out = self.cli("init", "--from", path)
        self.assertEqual(code, 1, out)
        self.assertIn("--title", out)
        self.assertFalse(self.state_path.exists())

    def test_no_from_and_no_title_is_usage_error(self):
        code, out = self.cli("init", "--source", "tutorial.md")
        self.assertEqual(code, 2, out)
        self.assertIn("--title", out)
        self.assertFalse(self.state_path.exists())

    def test_no_from_and_no_source_is_usage_error(self):
        code, out = self.cli("init", "--title", "Hello")
        self.assertEqual(code, 2, out)
        self.assertIn("--source", out)
        self.assertFalse(self.state_path.exists())

    def test_missing_from_file_exits_1(self):
        code, out = self.cli("init", "--from", str(self.src_dir / "nope.json"))
        self.assertEqual(code, 1, out)
        self.assertFalse(self.state_path.exists())

    def test_invalid_json_from_exits_1(self):
        self.source.write_text("{not json", encoding="utf-8")
        code, out = self.cli("init", "--from", str(self.source))
        self.assertEqual(code, 1, out)
        self.assertFalse(self.state_path.exists())

    def test_from_with_existing_state_needs_force(self):
        path = self.write_source(self.example())
        self.assertEqual(self.cli("init", "--from", path)[0], 0)
        code, out = self.cli("init", "--from", path)
        self.assertEqual(code, 1, out)
        self.assertEqual(self.cli("init", "--from", path, "--force")[0], 0)

    def test_strip_progress_returns_copy_and_leaves_input_alone(self):
        spec = importlib.util.spec_from_file_location("dag_manager_under_test", str(CLI))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        source = self.dirty_source()
        before = copy.deepcopy(source)
        stripped = module.strip_progress(source)
        self.assertEqual(source, before)
        self.assertIsNot(stripped, source)
        self.assertNotIn("git", stripped)
        self.assertNotIn("services", stripped)
        self.assertNotIn("active_node_id", stripped)
        for node in stripped["nodes"]:
            self.assertEqual(node["status"], "pending")
            self.assertIsNone(node["commit_sha"])
            self.assertNotIn("approved", node)
            self.assertNotIn("deviations", node)


class ScaffoldTest(unittest.TestCase):
    """`scaffold <name> --from <dag.json>` packages a DAG as its own skill (C-G12)."""

    TEMPLATE = (
        "# {{name}}\n\n{{title}}\n\n{{description}}\n\nnodes: {{node_count}}\n"
    )

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="scaffold-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.src_skill = self.tmp / "source-skill"
        self.src_skill.mkdir()
        for name in ("scripts", "references"):
            shutil.copytree(str(SKILL_DIR / name), str(self.src_skill / name),
                            ignore=shutil.ignore_patterns("__pycache__"))
        (self.src_skill / "templates").mkdir()
        (self.src_skill / "templates" / "SKILL.md.tmpl").write_text(
            self.TEMPLATE, encoding="utf-8")
        self.cli_path = self.src_skill / "scripts" / "dag_manager.py"
        self.dest = self.tmp / "dest"
        self.env = dict(os.environ)
        self.env.update({"HOME": str(self.tmp), "GIT_CONFIG_GLOBAL": os.devnull,
                         "GIT_CONFIG_SYSTEM": os.devnull})
        self.ws = self.tmp / "ws"
        self.ws.mkdir()

    def run_cli(self, cli_path, cwd, *args):
        proc = subprocess.run(
            [sys.executable, str(cli_path)] + list(args), cwd=str(cwd), env=self.env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True,
        )
        return proc.returncode, proc.stdout

    def scaffold(self, name="demo", *extra, cli_path=None):
        return self.run_cli(cli_path or self.cli_path, self.ws, "scaffold", name,
                            "--from", str(EXAMPLE_DAG), "--dest", str(self.dest), *extra)

    def leftovers(self):
        if not self.dest.exists():
            return []
        return [p.name for p in self.dest.iterdir() if p.name.startswith(".")]

    def test_bad_name_exits_2(self):
        for bad in ("Bad Name", "../escape", "-lead"):
            code, out = self.scaffold(bad)
            self.assertEqual(code, 2, out)
        self.assertFalse(self.dest.exists() and any(self.dest.iterdir()))

    def test_existing_target_without_force_exits_1_and_is_untouched(self):
        target = self.dest / "demo"
        target.mkdir(parents=True)
        (target / "keep.txt").write_text("mine", encoding="utf-8")
        code, out = self.scaffold("demo")
        self.assertEqual(code, 1, out)
        self.assertEqual((target / "keep.txt").read_text(encoding="utf-8"), "mine")
        self.assertEqual(self.leftovers(), [])

    def test_symlink_target_exits_1_and_is_untouched(self):
        real = self.tmp / "real"
        real.mkdir()
        self.dest.mkdir(parents=True)
        link = self.dest / "demo"
        os.symlink(str(real), str(link), target_is_directory=True)
        code, out = self.scaffold("demo", "--force")
        self.assertEqual(code, 1, out)
        self.assertTrue(os.path.islink(str(link)))
        self.assertTrue(real.is_dir())
        self.assertEqual(self.leftovers(), [])

    def test_failed_swap_restores_old_target(self):
        target = self.dest / "demo"
        target.mkdir(parents=True)
        (target / "keep.txt").write_text("mine", encoding="utf-8")
        spec = importlib.util.spec_from_file_location("dag_manager_swap", str(self.cli_path))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        real_rename = os.rename

        def fake_rename(src, dst):
            if (os.path.basename(str(src)).startswith(".demo.tmp-")
                    and str(dst) == str(target)):
                raise OSError("injected rename failure")
            return real_rename(src, dst)

        argv = ["--cwd", str(self.ws), "scaffold", "demo",
                "--from", str(EXAMPLE_DAG), "--dest", str(self.dest), "--force"]
        with mock.patch.object(os, "rename", side_effect=fake_rename):
            code = module.main(argv)
        self.assertEqual(code, 1)
        self.assertEqual((target / "keep.txt").read_text(encoding="utf-8"), "mine")
        self.assertEqual(self.leftovers(), [])

    def test_force_replaces_existing_target(self):
        target = self.dest / "demo"
        target.mkdir(parents=True)
        (target / "stale.txt").write_text("old", encoding="utf-8")
        code, out = self.scaffold("demo", "--force")
        self.assertEqual(code, 0, out)
        self.assertFalse((target / "stale.txt").exists())
        self.assertTrue((target / "SKILL.md").is_file())
        self.assertEqual(self.leftovers(), [])

    def test_output_layout_and_stripped_dag(self):
        code, out = self.scaffold("demo")
        self.assertEqual(code, 0, out)
        target = self.dest / "demo"
        self.assertIn(str(target), out)
        self.assertTrue((target / "scripts" / "dag_manager.py").is_file())
        self.assertTrue((target / "references").is_dir())
        self.assertFalse((target / "templates").exists())
        self.assertFalse((target / "tests").exists())
        self.assertFalse(list(target.rglob("__pycache__")))
        doc = json.loads((target / "dag.json").read_text(encoding="utf-8"))
        for key in ("git", "services", "residue", "preflight", "active_node_id"):
            self.assertNotIn(key, doc)
        self.assertTrue(doc["nodes"])
        for node in doc["nodes"]:
            self.assertEqual(node["status"], "pending")
            self.assertIsNone(node["commit_sha"])

    def test_rendered_skill_md(self):
        code, out = self.scaffold("demo")
        self.assertEqual(code, 0, out)
        text = (self.dest / "demo" / "SKILL.md").read_text(encoding="utf-8")
        doc = json.loads(EXAMPLE_DAG.read_text(encoding="utf-8"))
        title = doc["title"]
        self.assertIn("demo", text)
        self.assertIn(title, text)
        self.assertIn("Run the {} tutorial step by step with checks and rewinds. "
                      "Use when the user wants to work through {}.".format(title, title), text)
        self.assertIn("nodes: {}".format(len(doc["nodes"])), text)
        self.assertNotIn("{{", text)

    def test_invalid_source_exits_1_and_writes_nothing(self):
        bad = self.tmp / "bad.json"
        doc = json.loads(EXAMPLE_DAG.read_text(encoding="utf-8"))
        doc["schema_version"] = "9.9"
        bad.write_text(json.dumps(doc), encoding="utf-8")
        proc_args = ["scaffold", "demo", "--from", str(bad), "--dest", str(self.dest)]
        code, out = self.run_cli(self.cli_path, self.ws, *proc_args)
        self.assertEqual(code, 1, out)
        self.assertFalse((self.dest / "demo").exists())
        self.assertEqual(self.leftovers(), [])

    def test_generated_skill_runs_init_and_status(self):
        code, out = self.scaffold("demo")
        self.assertEqual(code, 0, out)
        gen_cli = self.dest / "demo" / "scripts" / "dag_manager.py"
        repo = self.tmp / "repo"
        repo.mkdir()
        git_env = dict(self.env)
        git_env.update({"GIT_AUTHOR_NAME": "Tutorial Test",
                        "GIT_AUTHOR_EMAIL": "tutorial@example.invalid",
                        "GIT_COMMITTER_NAME": "Tutorial Test",
                        "GIT_COMMITTER_EMAIL": "tutorial@example.invalid"})
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=str(repo), env=git_env,
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        code, out = self.run_cli(gen_cli, repo, "init", "--from",
                                 str(self.dest / "demo" / "dag.json"))
        self.assertEqual(code, 0, out)
        code, out = self.run_cli(gen_cli, repo, "status")
        self.assertEqual(code, 0, out)
        state = json.loads((repo / ".tutorial-dag.json").read_text(encoding="utf-8"))
        self.assertTrue(state["nodes"])
        for node in state["nodes"]:
            self.assertEqual(node["status"], "pending")

    def test_missing_template_exits_1_with_message(self):
        shutil.rmtree(str(self.src_skill / "templates"))
        code, out = self.scaffold("demo", cli_path=self.cli_path)
        self.assertEqual(code, 1, out)
        self.assertIn("scaffold needs templates/SKILL.md.tmpl (run it from the source skill)",
                      out)
        self.assertFalse((self.dest / "demo").exists())
        self.assertEqual(self.leftovers(), [])


class SubdirWorkspaceTest(unittest.TestCase):
    """A workspace (--cwd) inside a larger repository: Git writes stay inside it."""

    def setUp(self):
        self.repo = Path(tempfile.mkdtemp(prefix="dag-manager-subdir-"))
        self.addCleanup(shutil.rmtree, self.repo, ignore_errors=True)
        self.workspace = self.repo / "sandbox"
        self.workspace.mkdir()
        self.env = dict(os.environ)
        self.env.update(
            {
                "HOME": str(self.repo),
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_CONFIG_SYSTEM": os.devnull,
                "GIT_AUTHOR_NAME": "Tutorial Test",
                "GIT_AUTHOR_EMAIL": "tutorial@example.invalid",
                "GIT_COMMITTER_NAME": "Tutorial Test",
                "GIT_COMMITTER_EMAIL": "tutorial@example.invalid",
                "GIT_ADVICE_ENABLED": "false",
            }
        )
        self.env.pop("GIT_DIR", None)
        self.env.pop("GIT_WORK_TREE", None)
        self.git("init", "-q", "-b", "main")
        (self.repo / "notes.txt").write_text("important\n", encoding="utf-8")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "seed")

        node = {
            "summary": "s", "action_type": "mutate", "status": "pending",
            "commit_sha": None, "commands": [],
        }
        nodes = [
            dict(node, id="1-one", title="one", dependencies=[], artifacts=["x.txt"],
                 verification={"type": "file_exists", "path": "x.txt"}),
            dict(node, id="2-two", title="two", dependencies=["1-one"],
                 artifacts=["new.txt"],
                 verification={"type": "file_exists", "path": "new.txt"}),
        ]
        dag = self.repo / "dag.json"
        dag.write_text(json.dumps({"title": "t", "source_doc": "t.md", "mode": "assisted",
                                   "nodes": nodes}), encoding="utf-8")
        self.cli("init", "--from", str(dag), check=0)

    def git(self, *args, cwd=None):
        proc = subprocess.run(
            ["git"] + list(args), cwd=str(cwd or self.repo), env=self.env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True,
        )
        if proc.returncode != 0:
            self.fail("git {} failed: {}".format(" ".join(args), proc.stdout))
        return proc.stdout.strip()

    def cli(self, *args, check=None):
        proc = subprocess.run(
            [sys.executable, str(CLI), "--cwd", str(self.workspace)] + list(args),
            cwd=str(self.repo), env=self.env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True,
        )
        if check is not None:
            self.assertEqual(check, proc.returncode, proc.stdout)
        return proc.returncode, proc.stdout

    def put(self, relative, text, base=None):
        path = (base or self.workspace) / relative
        path.write_text(text, encoding="utf-8")

    def committed_paths(self, rev="HEAD"):
        return sorted(self.git("show", "--name-only", "--format=", rev).split())

    def complete_one_and_two(self):
        """Node 1 writes x.txt and gone.txt; an outside commit; node 2 edits them."""
        self.cli("next", check=0)
        self.put("x.txt", "one\n")
        self.put("gone.txt", "g\n")
        self.cli("complete", "--commit", check=0)
        one = self.git("rev-parse", "HEAD")
        self.put("other.txt", "outside\n", base=self.repo)
        self.git("add", "other.txt")
        self.git("commit", "-q", "-m", "outside work")
        self.put("x.txt", "two\n")
        self.put("new.txt", "n\n")
        (self.workspace / "gone.txt").unlink()
        self.cli("complete", "--commit", check=0)
        return one, self.git("rev-parse", "HEAD")

    def test_checkpoint_commits_only_workspace_paths(self):
        self.put("notes.txt", "important\nuser WIP\n", base=self.repo)
        self.cli("next", check=0)
        self.put("x.txt", "one\n")
        self.cli("complete", "--commit", check=0)
        self.assertEqual(["sandbox/x.txt"], self.committed_paths())
        self.assertEqual("M notes.txt", self.git("status", "--porcelain", "--", "notes.txt"))

    def test_hard_rewind_force_only_needed_for_workspace_changes(self):
        self.complete_one_and_two()
        self.put("notes.txt", "important\nuser WIP\n", base=self.repo)
        self.cli("jump", "2-two", "--reset-descendants", "--git-rollback", "hard", check=0)

        self.put("x.txt", "edited in workspace\n")
        code, output = self.cli(
            "jump", "1-one", "--reset-descendants", "--git-rollback", "hard"
        )
        self.assertEqual(1, code, output)
        self.assertIn("--force", output)

    def test_hard_rewind_restores_workspace_and_keeps_outside_work(self):
        one, two = self.complete_one_and_two()
        self.put("notes.txt", "important\nuser WIP\n", base=self.repo)
        self.cli(
            "jump", "2-two", "--reset-descendants", "--git-rollback", "hard", "--force",
            check=0,
        )
        self.assertEqual("one\n", (self.workspace / "x.txt").read_text(encoding="utf-8"))
        self.assertTrue((self.workspace / "gone.txt").is_file())
        self.assertFalse((self.workspace / "new.txt").exists())
        # Outside the workspace: the later commit and the uncommitted edit survive.
        self.assertEqual("outside\n", (self.repo / "other.txt").read_text(encoding="utf-8"))
        self.assertIn("user WIP", (self.repo / "notes.txt").read_text(encoding="utf-8"))
        # HEAD moves forward with a rewind commit that touches only the workspace.
        self.assertEqual(two, self.git("rev-parse", "HEAD~1"))
        self.assertTrue(self.git("log", "-1", "--format=%s").startswith("tutorial(rewind):"))
        self.assertTrue(all(p.startswith("sandbox/") for p in self.committed_paths()))
        self.assertEqual("", self.git("status", "--porcelain", "--", "sandbox"))
        self.assertEqual("", self.git("diff", one, "HEAD", "--", "sandbox"))
        state = json.loads((self.workspace / STATE_NAME).read_text(encoding="utf-8"))
        statuses = {n["id"]: n["status"] for n in state["nodes"]}
        self.assertEqual({"1-one": "completed", "2-two": "pending"}, statuses)

    def test_hard_rewind_to_init_when_workspace_absent_there(self):
        # The seed commit predates every sandbox file.
        self.complete_one_and_two()
        self.cli(
            "jump", "1-one", "--reset-descendants", "--git-rollback", "hard", "--force",
            check=0,
        )
        self.assertEqual("", self.git("ls-files", "--", "sandbox"))
        self.assertEqual("outside\n", (self.repo / "other.txt").read_text(encoding="utf-8"))


class PlatformVariantsTest(unittest.TestCase):
    """Per-OS node.variants validation and runtime resolution (C-G5)."""

    @classmethod
    def setUpClass(cls):
        cls.dm = load_cli_module()
        previous = sys.modules.get("dag_manager")
        sys.modules["dag_manager"] = cls.dm
        cls.addClassCleanup(
            lambda: sys.modules.__setitem__("dag_manager", previous)
            if previous is not None
            else sys.modules.pop("dag_manager", None)
        )

    def setUp(self):
        self.workspace = Path(tempfile.mkdtemp(prefix="dag-variants-test-"))
        self.addCleanup(shutil.rmtree, self.workspace, ignore_errors=True)
        self.state_path = self.workspace / STATE_NAME

    def _base_node(self, **overrides):
        node = {
            "id": "1-step",
            "title": "Install step",
            "summary": "Install dependency.",
            "dependencies": [],
            "action_type": "execute",
            "status": "pending",
            "commit_sha": None,
            "artifacts": [],
            "commands": ["echo base-cmd"],
            "verification": {"type": "command", "command": "echo base-check"},
            "undo": ["echo base-undo"],
        }
        node.update(overrides)
        return node

    def _write_state(self, nodes, active="1-step"):
        state = {
            "schema_version": "1.2",
            "title": "Variants Demo",
            "source_doc": "README.md",
            "mode": "assisted",
            "persistence": "file",
            "active_node_id": active,
            "created_at": "2026-11-27T00:00:00Z",
            "updated_at": "2026-11-27T00:00:00Z",
            "git": {
                "branch": "main",
                "initial_commit_sha": "",
                "initial_dirty": False,
                "exclude_configured": True,
            },
            "nodes": nodes,
        }
        self.state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
        return state

    def _run_main(self, *argv):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            code = self.dm.main(["--cwd", str(self.workspace)] + list(argv))
        return code, buf.getvalue()

    def test_linux_and_darwin_variant_selection_and_field_fallback(self):
        node = self._base_node(
            variants={
                "linux": {
                    "commands": ["echo linux-cmd"],
                    "verification": {"type": "command", "command": "echo linux-check"},
                    "undo": ["echo linux-undo"],
                },
                "darwin": {
                    "commands": ["echo darwin-cmd"],
                },
            }
        )
        with mock.patch("dag_manager.platform.system", return_value="Linux"):
            self.assertEqual("linux", self.dm.current_os())
            self.assertEqual(["echo linux-cmd"], self.dm.effective_commands(node))
            self.assertEqual(
                {"type": "command", "command": "echo linux-check"},
                self.dm.effective_verification(node),
            )
            self.assertEqual(["echo linux-undo"], self.dm.effective_undo(node))

        with mock.patch("dag_manager.platform.system", return_value="Darwin"):
            self.assertEqual("darwin", self.dm.current_os())
            self.assertEqual(["echo darwin-cmd"], self.dm.effective_commands(node))
            # Verification and undo are absent from darwin variant -> fall back to base.
            self.assertEqual(
                {"type": "command", "command": "echo base-check"},
                self.dm.effective_verification(node),
            )
            self.assertEqual(["echo base-undo"], self.dm.effective_undo(node))

    def test_fallback_to_base_when_os_absent_from_variants(self):
        node = self._base_node(
            variants={"linux": {"commands": ["echo linux-cmd"]}}
        )
        with mock.patch("dag_manager.platform.system", return_value="Windows"):
            self.assertEqual("windows", self.dm.current_os())
            self.assertEqual(["echo base-cmd"], self.dm.effective_commands(node))
            self.assertEqual(
                {"type": "command", "command": "echo base-check"},
                self.dm.effective_verification(node),
            )
            self.assertEqual(["echo base-undo"], self.dm.effective_undo(node))

    def test_windows_variant_schema_validation_passes(self):
        node = self._base_node(
            variants={
                "windows": {
                    "commands": ["winget install -e --id Git.Git"],
                    "verification": {"type": "command", "command": "git --version"},
                    "undo": ["winget uninstall --id Git.Git"],
                }
            }
        )
        self.assertEqual([], self.dm.validate_node(node, 0, {}))

    def test_invalid_variants_rejected(self):
        bad_cases = [
            "linux",
            {},
            {"plan9": {"commands": ["echo hi"]}},
            {"linux": {}},
            {"linux": ["echo hi"]},
            {"linux": {"artifacts": ["file.txt"]}},
            {"linux": {"commands": [""]}},
            {"linux": {"commands": "echo hi"}},
            {"linux": {"undo": ["   "]}},
            {"linux": {"verification": {"type": "bogus"}}},
        ]
        for bad in bad_cases:
            node = self._base_node(variants=bad)
            problems = self.dm.validate_node(node, 0, {})
            self.assertTrue(problems, "expected rejection for {!r}".format(bad))

    def test_risk_tier_and_interactive_hints_use_effective_variant(self):
        node = self._base_node(
            interactive={"answers": ["yes"]},
            variants={
                "linux": {
                    "commands": ["sudo apt-get install -y jq"],
                    "undo": ["echo undo-linux"],
                },
                "darwin": {
                    "commands": ["brew install jq"],
                },
            },
        )
        with mock.patch("dag_manager.platform.system", return_value="Linux"):
            tier, labels = self.dm.risk_tier(node)
            self.assertEqual(("high", ["sudo"]), (tier, labels))
            hints = self.dm.interactive_hints(node)
            self.assertIn("sudo apt-get install -y jq", hints[0])

        with mock.patch("dag_manager.platform.system", return_value="Darwin"):
            tier, labels = self.dm.risk_tier(node)
            self.assertEqual(("low", []), (tier, labels))
            hints = self.dm.interactive_hints(node)
            self.assertIn("brew install jq", hints[0])

    def test_status_verify_and_jump_undo_use_effective_variant(self):
        marker = self.workspace / "undone.txt"
        node = self._base_node(
            status="completed",
            side_effects=["user"],
            commands=["echo base-cmd"],
            verification={"type": "command", "command": "echo base-ok"},
            undo=["echo base > undone.txt"],
            variants={
                "linux": {
                    "commands": ["echo linux-only-cmd"],
                    "verification": {"type": "command", "command": "echo linux-ok"},
                    "undo": ["printf linux > undone.txt"],
                }
            },
        )
        root = self._base_node(
            id="0-root",
            dependencies=[],
            status="completed",
            side_effects=["repo"],
            commands=["true"],
            verification={"type": "confirm", "prompt": "ok"},
        )
        node["dependencies"] = ["0-root"]
        self._write_state([root, node], active="1-step")

        with mock.patch("dag_manager.platform.system", return_value="Linux"):
            code, out = self._run_main("status")
            self.assertEqual(0, code, out)
            self.assertIn("$ echo linux-only-cmd", out)
            self.assertIn("linux-ok", out)

            live_node = json.loads(self.state_path.read_text(encoding="utf-8"))["nodes"][1]
            passed, lines = self.dm.run_verification(live_node, self.workspace, False)
            self.assertTrue(passed, lines)
            self.assertIn("linux-ok", "\n".join(lines))

            code, out = self._run_main(
                "jump", "0-root", "--reset-descendants", "--undo"
            )
            self.assertEqual(0, code, out)
            self.assertEqual("linux", marker.read_text(encoding="utf-8"))

    def test_deviate_mutates_active_variant_or_falls_back_to_base(self):
        node = self._base_node(
            variants={
                "linux": {
                    "commands": ["apt-get install foo"],
                    "verification": {"type": "command", "command": "which foo"},
                },
                "darwin": {
                    "commands": ["brew install foo"],
                },
            }
        )
        self._write_state([node], active="1-step")

        # Linux overrides both commands and verification -> mutate linux variant only.
        with mock.patch("dag_manager.platform.system", return_value="Linux"):
            code, out = self._run_main(
                "deviate", "1-step", "--command", "0", "--to", "apt-get install bar",
                "--reason", "renamed package",
            )
            self.assertEqual(0, code, out)
            code, out = self._run_main(
                "deviate", "1-step", "--check", "--to", "sudo which bar",
                "--reason", "check renamed binary",
            )
            self.assertEqual(0, code, out)
            self.assertIn("risk: high (sudo)", out)

        saved = json.loads(self.state_path.read_text(encoding="utf-8"))["nodes"][0]
        self.assertEqual(["apt-get install bar"], saved["variants"]["linux"]["commands"])
        self.assertEqual("sudo which bar", saved["variants"]["linux"]["verification"]["command"])
        self.assertEqual(["echo base-cmd"], saved["commands"])
        self.assertEqual("echo base-check", saved["verification"]["command"])

        # Darwin overrides commands only -> --check falls back to mutating base verification.
        with mock.patch("dag_manager.platform.system", return_value="Darwin"):
            code, out = self._run_main(
                "deviate", "1-step", "--check", "--to", "echo darwin-fallback-check",
                "--reason", "fix base check",
            )
            self.assertEqual(0, code, out)

        saved = json.loads(self.state_path.read_text(encoding="utf-8"))["nodes"][0]
        self.assertEqual("echo darwin-fallback-check", saved["verification"]["command"])
        self.assertEqual("sudo which bar", saved["variants"]["linux"]["verification"]["command"])

        # Windows absent from variants -> mutates base commands.
        with mock.patch("dag_manager.platform.system", return_value="Windows"):
            code, out = self._run_main(
                "deviate", "1-step", "--command", "0", "--to", "echo windows-base-cmd",
                "--reason", "fix base cmd",
            )
            self.assertEqual(0, code, out)

        saved = json.loads(self.state_path.read_text(encoding="utf-8"))["nodes"][0]
        self.assertEqual(["echo windows-base-cmd"], saved["commands"])
        self.assertEqual(["brew install foo"], saved["variants"]["darwin"]["commands"])

    def test_next_prints_variant_lines_on_match_and_omits_on_fallback(self):
        node = self._base_node(
            variants={
                "linux": {
                    "commands": ["sudo apt-get update", "sudo apt-get install -y jq"],
                    "verification": {"type": "command", "command": "jq --version"},
                },
                "darwin": {
                    "commands": [],
                    "verification": {"type": "port_listening", "port": 8080},
                },
            }
        )
        self._write_state([node], active="1-step")

        with mock.patch("dag_manager.platform.system", return_value="Linux"):
            code, out = self._run_main("next")
            self.assertEqual(0, code, out)
            self.assertIn("  variant: linux (overrides: commands, verification)", out)
            self.assertIn("  commands: sudo apt-get update; sudo apt-get install -y jq", out)
            self.assertIn("  verification: command jq --version", out)

        with mock.patch("dag_manager.platform.system", return_value="Darwin"):
            code, out = self._run_main("next")
            self.assertEqual(0, code, out)
            self.assertIn("  variant: darwin (overrides: commands, verification)", out)
            self.assertIn("  commands: (none)", out)
            self.assertIn("  verification: port_listening 8080", out)

        with mock.patch("dag_manager.platform.system", return_value="Windows"):
            code, out = self._run_main("next")
            self.assertEqual(0, code, out)
            self.assertIn("active -> 1-step (execute)", out)
            self.assertNotIn("  variant:", out)
            self.assertNotIn("  commands:", out)
            self.assertNotIn("  verification:", out)

        plain_node = self._base_node()
        self._write_state([plain_node], active="1-step")
        with mock.patch("dag_manager.platform.system", return_value="Linux"):
            code, out = self._run_main("next")
            self.assertEqual(0, code, out)
            self.assertIn("active -> 1-step (execute)", out)
            self.assertNotIn("  variant:", out)

    def test_deviate_and_export_with_active_variant(self):
        tutorial = self.workspace / "README.md"
        tutorial.write_text(
            "Install foo:\n```sh\napt-get install foo\n```\nVerify:\n```sh\nwhich foo\n```\n",
            encoding="utf-8",
        )
        node = self._base_node(
            variants={
                "linux": {
                    "commands": ["apt-get install foo"],
                    "verification": {"type": "command", "command": "which foo"},
                },
                "darwin": {
                    "commands": ["brew install foo"],
                },
            }
        )
        self._write_state([node], active="1-step")

        with mock.patch("dag_manager.platform.system", return_value="Linux"):
            code, out = self._run_main(
                "deviate", "1-step", "--command", "0", "--to", "apt-get install bar",
                "--reason", "package renamed",
            )
            self.assertEqual(0, code, out)
            code, out = self._run_main(
                "deviate", "1-step", "--check", "--to", "which bar",
                "--reason", "binary renamed",
            )
            self.assertEqual(0, code, out)
            code, out = self._run_main("export", "--out", "export-out")
            self.assertEqual(0, code, out)

        export_dir = self.workspace / "export-out"
        deviations_md = (export_dir / "deviations.md").read_text(encoding="utf-8")
        self.assertIn("- target: `command:0`", deviations_md)
        self.assertIn("- original: `apt-get install foo`", deviations_md)
        self.assertIn("- replacement: `apt-get install bar`", deviations_md)
        self.assertIn("- target: `check`", deviations_md)
        self.assertIn("- original: `which foo`", deviations_md)
        self.assertIn("- replacement: `which bar`", deviations_md)
        self.assertNotIn("## Not applied", deviations_md)

        corrected_md = (export_dir / "tutorial.corrected.md").read_text(encoding="utf-8")
        self.assertIn("apt-get install bar", corrected_md)
        self.assertNotIn("apt-get install foo", corrected_md)
        self.assertIn("which bar", corrected_md)
        self.assertNotIn("which foo", corrected_md)


if __name__ == "__main__":
    unittest.main()
