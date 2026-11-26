"""Corpus harness: run real tutorial fixtures through the CLI end to end.

Each fixture under ``tests/corpus/<slug>/`` holds a tutorial, an expected DAG
that must validate today, and a ``fixture.json`` describing how to run it.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent
CLI = SKILL_DIR / "scripts" / "dag_manager.py"
CORPUS = SKILL_DIR / "tests" / "corpus"
CORPUS_SLUGS = [
    "container",
    "python-package",
    "shell-sysadmin",
    "sqlite-data",
    "stale-tutorial",
    "web-service",
]
REQUIRED_FILES = ["tutorial.md", "expected-dag.json", "fixture.json"]
# Root fields that the harness does not copy from the fixture into live state.
SKIPPED_ROOT_FIELDS = {"git", "schema_version", "persistence", "active_node_id"}


def free_port() -> int:
    """Return an unused TCP port on 127.0.0.1."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def substitute(obj, port: int, home: str):
    """Replace ``{{PORT}}`` and ``{{HOME}}`` placeholders in parsed JSON."""
    if isinstance(obj, dict):
        return {key: substitute(value, port, home) for key, value in obj.items()}
    if isinstance(obj, list):
        return [substitute(value, port, home) for value in obj]
    if isinstance(obj, str):
        if obj == "{{PORT}}":
            return port
        return obj.replace("{{PORT}}", str(port)).replace("{{HOME}}", home)
    return obj


def isolated_env(home: Path) -> dict:
    """Environment that keeps Git and HOME away from the real user config."""
    env = dict(os.environ)
    env.update(
        {
            "HOME": str(home),
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
    env.pop("GIT_DIR", None)
    env.pop("GIT_WORK_TREE", None)
    return env


def load_case(slug: str) -> tuple:
    """Return (fixture metadata, expected DAG) for one corpus slug."""
    folder = CORPUS / slug
    case = json.loads((folder / "fixture.json").read_text(encoding="utf-8"))
    dag = json.loads((folder / "expected-dag.json").read_text(encoding="utf-8"))
    return case, dag


def topological_order(nodes: list) -> list:
    """Kahn's algorithm over ``dependencies``; keeps file order for ties."""
    by_id = {node["id"]: node for node in nodes}
    done = set()
    order = []
    while len(order) < len(nodes):
        progressed = False
        for node in nodes:
            if node["id"] in done:
                continue
            if all(dep in done for dep in node["dependencies"]):
                order.append(node)
                done.add(node["id"])
                progressed = True
        if not progressed:
            raise ValueError("cycle or missing dependency among {}".format(sorted(by_id)))
    return order


def run_tutorial(case: dict, dag: dict, stop_before=None) -> Path:
    """Run ``dag`` to completion (or up to ``stop_before``) in a fresh workspace.

    Returns the workspace path. Caller owns cleanup via the returned parent
    directory's ``repo`` and ``home`` children.
    """
    root = Path(tempfile.mkdtemp(prefix="corpus-"))
    ws = root / "repo"
    home = root / "home"
    ws.mkdir()
    home.mkdir()
    env = isolated_env(home)

    def git(*args):
        subprocess.run(
            ["git"] + list(args), cwd=str(ws), env=env, check=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )

    def cli(*args) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(CLI)] + list(args), cwd=str(ws), env=env,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, universal_newlines=True,
        )

    # Seed commit first: rollback falls back to initial_commit_sha.
    git("init", "-q", "-b", "main")
    (ws / "README.md").write_text("# Scratch workspace\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-q", "-m", "seed")

    init = cli("init", "--title", dag["title"], "--source", dag["source_doc"],
               "--mode", dag["mode"])
    if init.returncode != 0:
        raise RuntimeError("init failed: " + init.stdout + init.stderr)

    state_path = ws / ".tutorial-dag.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    for key, value in dag.items():
        if key not in SKIPPED_ROOT_FIELDS and key != "nodes":
            state[key] = value
    state["nodes"] = dag["nodes"]
    state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")

    for node in topological_order(dag["nodes"]):
        if node["id"] == stop_before:
            break
        proc = cli("next", node["id"])
        if proc.returncode != 0:
            raise RuntimeError("next {} failed: {}".format(node["id"], proc.stderr))
        for command in node["commands"]:
            subprocess.run(
                command, shell=True, cwd=str(ws), env=env, stdin=subprocess.DEVNULL,
                timeout=60, check=True,
            )
        complete_args = ["complete", node["id"]]
        dirty = subprocess.run(
            ["git", "status", "--porcelain"], cwd=str(ws), env=env,
            stdout=subprocess.PIPE, universal_newlines=True, check=True,
        ).stdout.strip()
        if dirty:
            complete_args.append("--commit")
        if node["verification"]["type"] == "confirm":
            complete_args.append("--yes")
        proc = cli(*complete_args)
        if proc.returncode != 0:
            raise RuntimeError("complete {} failed: {}".format(node["id"], proc.stdout + proc.stderr))
    return ws


class CorpusFixtureTest(unittest.TestCase):
    """Fixture-level checks that need no tutorial execution."""

    def test_every_fixture_has_required_files(self):
        for slug in CORPUS_SLUGS:
            with self.subTest(slug=slug):
                folder = CORPUS / slug
                for name in REQUIRED_FILES:
                    self.assertTrue((folder / name).is_file(), "missing {}".format(name))
                dag = json.loads((folder / "expected-dag.json").read_text(encoding="utf-8"))
                roots = [node for node in dag["nodes"] if not node["dependencies"]]
                self.assertTrue(roots, "no root node without dependencies")

    def test_every_expected_dag_validates(self):
        for slug in CORPUS_SLUGS:
            with self.subTest(slug=slug):
                dag_path = CORPUS / slug / "expected-dag.json"
                proc = subprocess.run(
                    [sys.executable, str(CLI), "--file", str(dag_path), "validate"],
                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE, universal_newlines=True,
                )
                self.assertEqual(0, proc.returncode, proc.stdout + proc.stderr)
                dag = json.loads(dag_path.read_text(encoding="utf-8"))
                self.assertEqual("1.0", dag["schema_version"])


class CorpusRunTest(unittest.TestCase):
    """Walk runnable fixtures through the CLI."""

    def test_runnable_fixtures_complete(self):
        for slug in CORPUS_SLUGS:
            case, dag = load_case(slug)
            if not case["runnable"] or case["expect_fail_at"] is not None:
                continue
            with self.subTest(slug=slug):
                ws = run_tutorial(case, dag)
                self.addCleanup(shutil.rmtree, ws.parent, ignore_errors=True)
                proc = subprocess.run(
                    [sys.executable, str(CLI), "status", "--json"], cwd=str(ws),
                    env=isolated_env(ws.parent / "home"), stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    universal_newlines=True,
                )
                self.assertEqual(0, proc.returncode, proc.stderr)
                state = json.loads(proc.stdout)
                statuses = {node["id"]: node["status"] for node in state["nodes"]}
                for node_id, status in statuses.items():
                    self.assertIn(status, ("completed", "skipped"), node_id)

    def test_stale_fixture_fails_at_declared_node(self):
        for slug in CORPUS_SLUGS:
            case, dag = load_case(slug)
            fail_at = case["expect_fail_at"]
            if not case["runnable"] or fail_at is None:
                continue
            with self.subTest(slug=slug):
                ws = run_tutorial(case, dag, stop_before=fail_at)
                self.addCleanup(shutil.rmtree, ws.parent, ignore_errors=True)
                proc = subprocess.run(
                    [sys.executable, str(CLI), "verify", fail_at], cwd=str(ws),
                    env=isolated_env(ws.parent / "home"), stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    universal_newlines=True,
                )
                self.assertEqual(1, proc.returncode, proc.stdout + proc.stderr)


# --- gap tests ---


if __name__ == "__main__":
    unittest.main()
