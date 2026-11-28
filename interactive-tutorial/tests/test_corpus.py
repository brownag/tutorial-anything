"""Corpus harness: run real tutorial fixtures through the CLI end to end.

Each fixture under ``tests/corpus/<slug>/`` holds a tutorial, an expected DAG
that must validate today, and a ``fixture.json`` describing how to run it.
"""

from __future__ import annotations

import copy
import json
import os
import platform
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent
CLI = SKILL_DIR / "scripts" / "dag_manager.py"
CORPUS = SKILL_DIR / "tests" / "corpus"
CORPUS_SLUGS = [
    "container",
    "cross-os",
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


def write_temp_dag(dag: dict) -> Path:
    """Write ``dag`` to a fresh temp file and return its path; caller removes the parent."""
    folder = Path(tempfile.mkdtemp(prefix="corpus-dag-"))
    path = folder / "dag.json"
    path.write_text(json.dumps(dag, indent=2), encoding="utf-8")
    return path


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


def run_cli(ws: Path, *args) -> subprocess.CompletedProcess:
    """Run the CLI in ``ws`` with the isolated environment of that workspace."""
    return subprocess.run(
        [sys.executable, str(CLI)] + list(args), cwd=str(ws),
        env=isolated_env(ws.parent / "home"), stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True,
    )


def seed_workspace(dag: dict) -> Path:
    """Create a fresh git workspace, ``init`` it, and copy ``dag`` into live state.

    Returns the workspace path. The caller owns cleanup of ``ws.parent``.
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

    # Seed commit first: rollback falls back to initial_commit_sha.
    git("init", "-q", "-b", "main")
    (ws / "README.md").write_text("# Scratch workspace\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-q", "-m", "seed")

    init = run_cli(ws, "init", "--title", dag["title"], "--source", dag["source_doc"],
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
    return ws


def effective_node_field(node: dict, field: str):
    """Resolve ``node[field]`` via the active OS variant with fallback to the base field."""
    variant = (node.get("variants") or {}).get(platform.system().lower()) or {}
    if field in variant:
        return variant[field]
    return node[field]


def run_tutorial(case: dict, dag: dict, stop_before=None) -> Path:
    """Run ``dag`` to completion (or up to ``stop_before``) in a fresh workspace.

    Returns the workspace path. Caller owns cleanup via the returned parent
    directory's ``repo`` and ``home`` children.
    """
    ws = seed_workspace(dag)
    env = isolated_env(ws.parent / "home")

    for node in topological_order(dag["nodes"]):
        if node["id"] == stop_before:
            break
        proc = run_cli(ws, "next", node["id"])
        if proc.returncode != 0:
            raise RuntimeError("next {} failed: {}".format(node["id"], proc.stderr))
        if node.get("service"):
            proc = run_cli(ws, "service", "start", node["id"])
            if proc.returncode != 0:
                raise RuntimeError("service start {} failed: {}".format(node["id"], proc.stdout + proc.stderr))
        for command in effective_node_field(node, "commands"):
            subprocess.run(
                command, shell=True, cwd=str(ws), env=env, stdin=subprocess.DEVNULL,
                timeout=60, check=True,
            )
        if node["id"] == "stop":
            proc = run_cli(ws, "service", "stop", "serve")
            if proc.returncode != 0:
                raise RuntimeError("service stop serve failed: {}".format(proc.stdout + proc.stderr))
        complete_args = ["complete", node["id"]]
        dirty = subprocess.run(
            ["git", "status", "--porcelain"], cwd=str(ws), env=env,
            stdout=subprocess.PIPE, universal_newlines=True, check=True,
        ).stdout.strip()
        if dirty:
            complete_args.append("--commit")
        if effective_node_field(node, "verification")["type"] == "confirm":
            complete_args.append("--yes")
        proc = run_cli(ws, *complete_args)
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
                _, raw = load_case(slug)
                # Substitute placeholders so typed fields (port) validate as integers.
                home = tempfile.mkdtemp(prefix="corpus-home-")
                self.addCleanup(shutil.rmtree, home, ignore_errors=True)
                dag = substitute(raw, 8000, home)
                dag_path = write_temp_dag(dag)
                self.addCleanup(shutil.rmtree, dag_path.parent, ignore_errors=True)
                proc = subprocess.run(
                    [sys.executable, str(CLI), "--file", str(dag_path), "validate"],
                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE, universal_newlines=True,
                )
                self.assertEqual(0, proc.returncode, proc.stdout + proc.stderr)
                self.assertTrue(dag["schema_version"].startswith("1."))


class CorpusRunTest(unittest.TestCase):
    """Walk runnable fixtures through the CLI."""

    def test_runnable_fixtures_complete(self):
        for slug in CORPUS_SLUGS:
            case, raw = load_case(slug)
            if not case["runnable"] or case["expect_fail_at"] is not None:
                continue
            with self.subTest(slug=slug):
                dag = substitute(raw, free_port(), "")
                ws = run_tutorial(case, dag)
                self.addCleanup(shutil.rmtree, ws.parent, ignore_errors=True)
                self.addCleanup(stop_services, ws)
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
            case, raw = load_case(slug)
            fail_at = case["expect_fail_at"]
            if not case["runnable"] or fail_at is None:
                continue
            with self.subTest(slug=slug):
                dag = substitute(raw, free_port(), "")
                ws = run_tutorial(case, dag, stop_before=fail_at)
                self.addCleanup(shutil.rmtree, ws.parent, ignore_errors=True)
                self.addCleanup(stop_services, ws)
                proc = subprocess.run(
                    [sys.executable, str(CLI), "verify", fail_at], cwd=str(ws),
                    env=isolated_env(ws.parent / "home"), stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    universal_newlines=True,
                )
                self.assertEqual(1, proc.returncode, proc.stdout + proc.stderr)


# --- gap tests ---


def stop_services(ws: Path) -> None:
    """Stop any service a test started: ``teardown``, then kill recorded process groups."""
    run_cli(ws, "teardown")
    state_path = ws / ".tutorial-dag.json"
    if not state_path.exists():
        return
    state = json.loads(state_path.read_text(encoding="utf-8"))
    for entry in (state.get("services") or {}).values():
        pgid = entry.get("pgid")
        if pgid:
            try:
                os.killpg(pgid, signal.SIGKILL)
            except OSError:
                pass


class FeatureTest(unittest.TestCase):
    """Feature tests for the CLI behaviors that the corpus fixtures exercise."""

    def test_side_effects_undo(self):
        """Side-effect scopes, undo, jump --undo, and residue."""
        case, dag = load_case("shell-sysadmin")
        parent = next(n for n in dag["nodes"] if n["id"] == "write-config")["dependencies"][0]
        config = "{{HOME}}/.config/greet/greet.conf"

        # With --undo: the file is removed and no residue is recorded for write-config.
        ws = run_tutorial(case, dag)
        self.addCleanup(shutil.rmtree, ws.parent, ignore_errors=True)
        home = ws.parent / "home"
        proc = run_cli(ws, "jump", parent, "--reset-descendants",
                       "--git-rollback", "hard", "--force", "--undo")
        self.assertEqual(0, proc.returncode, proc.stdout + proc.stderr)
        self.assertFalse((home / ".config" / "greet" / "greet.conf").exists(), config)
        state = json.loads(run_cli(ws, "status", "--json").stdout)
        self.assertFalse(
            [entry for entry in state.get("residue", []) if entry["node_id"] == "write-config"]
        )

        # Without --undo: the file remains and exactly one not-run residue entry is recorded.
        ws = run_tutorial(case, dag)
        self.addCleanup(shutil.rmtree, ws.parent, ignore_errors=True)
        home = ws.parent / "home"
        proc = run_cli(ws, "jump", parent, "--reset-descendants",
                       "--git-rollback", "hard", "--force")
        self.assertEqual(0, proc.returncode, proc.stdout + proc.stderr)
        self.assertTrue((home / ".config" / "greet" / "greet.conf").exists(), config)
        state = json.loads(run_cli(ws, "status", "--json").stdout)
        entries = [entry for entry in state.get("residue", []) if entry["node_id"] == "write-config"]
        self.assertEqual(1, len(entries))
        self.assertEqual("not-run", entries[0]["reason"])
        status = run_cli(ws, "status")
        self.assertIn("residue: 1", status.stdout)

    def test_risk_tiers(self):
        """Risk tiers, HIGH gate in hands-off mode, and approve."""
        _, expected = load_case("container")
        # Test-only variant: hands-off mode, and no requires (docker may be absent).
        dag = copy.deepcopy(expected)
        dag["mode"] = "hands-off"
        del dag["requires"]
        ws = seed_workspace(dag)
        self.addCleanup(shutil.rmtree, ws.parent, ignore_errors=True)

        status = run_cli(ws, "status")
        self.assertEqual(0, status.returncode, status.stderr)
        rows = {line.split()[0]: line for line in status.stdout.splitlines() if line.strip()}
        self.assertIn("HIGH", rows.get("docker-group", ""), status.stdout)
        self.assertIn("low", rows.get("write-dockerfile", ""), status.stdout)

        blocked = run_cli(ws, "next", "docker-group")
        self.assertEqual(1, blocked.returncode, blocked.stdout + blocked.stderr)
        self.assertIn("approve docker-group", blocked.stdout + blocked.stderr)

        approved = run_cli(ws, "approve", "docker-group")
        self.assertEqual(0, approved.returncode, approved.stdout + approved.stderr)
        allowed = run_cli(ws, "next", "docker-group")
        self.assertEqual(0, allowed.returncode, allowed.stdout + allowed.stderr)

    def test_services(self):
        """Background services, http and port_listening checks, teardown."""
        case, raw = load_case("web-service")

        # Serve, check over HTTP, then teardown leaves nothing listening.
        port = free_port()
        dag = substitute(raw, port, "")
        ws = run_tutorial(case, dag, stop_before="serve")
        self.addCleanup(shutil.rmtree, ws.parent, ignore_errors=True)
        self.addCleanup(stop_services, ws)

        proc = run_cli(ws, "next", "serve")
        self.assertEqual(0, proc.returncode, proc.stdout + proc.stderr)
        proc = run_cli(ws, "service", "start", "serve")
        self.assertEqual(0, proc.returncode, proc.stdout + proc.stderr)
        proc = run_cli(ws, "complete", "serve")
        self.assertEqual(0, proc.returncode, proc.stdout + proc.stderr)
        proc = run_cli(ws, "next", "check")
        self.assertEqual(0, proc.returncode, proc.stdout + proc.stderr)
        proc = run_cli(ws, "verify", "check")
        self.assertEqual(0, proc.returncode, proc.stdout + proc.stderr)
        proc = run_cli(ws, "teardown")
        self.assertEqual(0, proc.returncode, proc.stdout + proc.stderr)
        with self.assertRaises(OSError):
            socket.create_connection(("127.0.0.1", port), timeout=2).close()
        state = json.loads(run_cli(ws, "status", "--json").stdout)
        self.assertFalse(state.get("services"))

        # Rewinding to serve with --reset-descendants stops its running service.
        port = free_port()
        dag = substitute(raw, port, "")
        ws = run_tutorial(case, dag, stop_before="serve")
        self.addCleanup(shutil.rmtree, ws.parent, ignore_errors=True)
        self.addCleanup(stop_services, ws)
        proc = run_cli(ws, "next", "serve")
        self.assertEqual(0, proc.returncode, proc.stdout + proc.stderr)
        proc = run_cli(ws, "service", "start", "serve")
        self.assertEqual(0, proc.returncode, proc.stdout + proc.stderr)
        proc = run_cli(ws, "jump", "serve", "--reset-descendants")
        self.assertEqual(0, proc.returncode, proc.stdout + proc.stderr)
        with self.assertRaises(OSError):
            socket.create_connection(("127.0.0.1", port), timeout=2).close()
        state = json.loads(run_cli(ws, "status", "--json").stdout)
        self.assertFalse(state.get("services"))

    def test_interactive_checks(self):
        """Stdin and timeout on command checks, interactive warnings."""
        _, expected = load_case("python-package")
        # Test-only nodes, appended to a copy of the real expected DAG.
        dag = copy.deepcopy(expected)
        dag["nodes"] += [
            {
                "id": "a-stdin-yes", "title": "Verify with fed stdin",
                "summary": "The verification reads 'yes' from stdin. Done when verify exits 0.",
                "dependencies": [], "action_type": "read-only", "status": "pending",
                "commit_sha": None, "artifacts": [], "commands": [],
                "verification": {
                    "type": "command",
                    "command": "python3 -c \"import sys; assert sys.stdin.read().strip() == 'yes'\"",
                    "stdin": "yes\n", "expect_exit": 0,
                },
            },
            {
                "id": "b-input-eof", "title": "Verify input() sees EOF",
                "summary": "input() with stdin held open must fail on EOF, not time out.",
                "dependencies": [], "action_type": "read-only", "status": "pending",
                "commit_sha": None, "artifacts": [], "commands": [],
                "verification": {"type": "command", "command": "python3 -c \"input()\"",
                                 "timeout": 5, "expect_exit": 0},
            },
            {
                "id": "c-npm-init", "title": "Flag an interactive command",
                "summary": "npm init is interactive and has no interactive block. Validate should warn.",
                "dependencies": [], "action_type": "execute", "status": "pending",
                "commit_sha": None, "artifacts": [], "commands": ["npm init"],
                "verification": {"type": "confirm", "prompt": "Confirm package.json was created."},
            },
        ]
        nodes = {node["id"]: node for node in dag["nodes"]}

        # (a) A command check gets the declared stdin.
        ws = seed_workspace(dag)
        self.addCleanup(shutil.rmtree, ws.parent, ignore_errors=True)
        proc = run_cli(ws, "verify", "a-stdin-yes")
        self.assertEqual(0, proc.returncode, proc.stdout + proc.stderr)

        # (b) With stdin held open, input() must see EOF quickly, not time out.
        ws = seed_workspace(dag)
        self.addCleanup(shutil.rmtree, ws.parent, ignore_errors=True)
        env = isolated_env(ws.parent / "home")
        with subprocess.Popen(
            [sys.executable, str(CLI), "verify", "b-input-eof"], cwd=str(ws), env=env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            universal_newlines=True,
        ) as proc:
            started = time.monotonic()
            try:
                returncode = proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
                self.fail("verify hung for more than 10 s with stdin held open")
            finally:
                proc.stdin.close()
            elapsed = time.monotonic() - started
            output = proc.stdout.read() + proc.stderr.read()
        self.assertLess(elapsed, 10)
        self.assertEqual(1, returncode, output)
        self.assertIn("EOF", output)

        # (c) Validate warns about an interactive command without an interactive block.
        dag_path = write_temp_dag(dag)
        self.addCleanup(shutil.rmtree, dag_path.parent, ignore_errors=True)
        proc = subprocess.run(
            [sys.executable, str(CLI), "--file", str(dag_path), "validate"],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            universal_newlines=True,
        )
        self.assertEqual(0, proc.returncode, proc.stdout + proc.stderr)
        self.assertIn("looks interactive", proc.stdout + proc.stderr)
        self.assertIn("npm init", nodes["c-npm-init"]["commands"][0])

    def test_doctor_preflight(self):
        """Root requires, doctor, and first-next preflight."""
        _, expected = load_case("container")
        # Test-only variant: guided mode and a tool that does not exist.
        dag = copy.deepcopy(expected)
        dag["mode"] = "guided"
        dag["requires"] = {"tools": {"python3": ">=3.0", "zz-missing-tool-9": "*"}}

        ws = seed_workspace(dag)
        self.addCleanup(shutil.rmtree, ws.parent, ignore_errors=True)
        proc = run_cli(ws, "doctor")
        self.assertEqual(1, proc.returncode, proc.stdout + proc.stderr)
        self.assertIn("zz-missing-tool-9", proc.stdout + proc.stderr)
        proc = run_cli(ws, "next", "write-dockerfile")
        self.assertEqual(1, proc.returncode, proc.stdout + proc.stderr)
        self.assertIn("--skip-doctor", proc.stdout + proc.stderr)
        proc = run_cli(ws, "next", "write-dockerfile", "--skip-doctor")
        self.assertEqual(0, proc.returncode, proc.stdout + proc.stderr)

        # Without the missing tool, doctor and first next both pass.
        fixed = copy.deepcopy(dag)
        del fixed["requires"]["tools"]["zz-missing-tool-9"]
        ws = seed_workspace(fixed)
        self.addCleanup(shutil.rmtree, ws.parent, ignore_errors=True)
        proc = run_cli(ws, "doctor")
        self.assertEqual(0, proc.returncode, proc.stdout + proc.stderr)
        proc = run_cli(ws, "next", "write-dockerfile")
        self.assertEqual(0, proc.returncode, proc.stdout + proc.stderr)

    def test_stale_deviation_export(self):
        """Stale v1 tutorial: deviate, verify, complete, export; hands-off gate."""
        case, raw = load_case("stale-tutorial")
        expected = copy.deepcopy(raw)
        expected["source_doc"] = str(CORPUS / "stale-tutorial" / "tutorial.md")
        deviate_args = ("deviate", "--check", "--to", "./greet --who Ada",
                        "--reason", "v2 renamed --name to --who")

        ws = run_tutorial(case, expected, stop_before="3-run-greet")
        self.addCleanup(shutil.rmtree, ws.parent, ignore_errors=True)
        self.addCleanup(stop_services, ws)
        proc = run_cli(ws, "next", "3-run-greet")
        self.assertEqual(0, proc.returncode, proc.stdout + proc.stderr)
        proc = run_cli(ws, "verify", "3-run-greet")
        self.assertEqual(1, proc.returncode, proc.stdout + proc.stderr)
        proc = run_cli(ws, *deviate_args)
        self.assertEqual(0, proc.returncode, proc.stdout + proc.stderr)
        proc = run_cli(ws, "verify", "3-run-greet")
        self.assertEqual(0, proc.returncode, proc.stdout + proc.stderr)
        proc = run_cli(ws, "complete", "3-run-greet")
        self.assertEqual(0, proc.returncode, proc.stdout + proc.stderr)
        proc = run_cli(ws, "export")
        self.assertEqual(0, proc.returncode, proc.stdout + proc.stderr)
        export = ws / ".tutorial-export"
        corrected = (export / "tutorial.corrected.md").read_text(encoding="utf-8")
        self.assertIn("./greet --who Ada", corrected)
        self.assertNotIn("./greet --name Ada", corrected)
        report = (export / "deviations.md").read_text(encoding="utf-8")
        self.assertIn("v2 renamed", report)

        # Hands-off: a deviated node cannot complete until approved.
        hands = copy.deepcopy(expected)
        hands["mode"] = "hands-off"
        ws = run_tutorial(case, hands, stop_before="3-run-greet")
        self.addCleanup(shutil.rmtree, ws.parent, ignore_errors=True)
        self.addCleanup(stop_services, ws)
        proc = run_cli(ws, "next", "3-run-greet")
        self.assertEqual(0, proc.returncode, proc.stdout + proc.stderr)
        proc = run_cli(ws, *deviate_args)
        self.assertEqual(0, proc.returncode, proc.stdout + proc.stderr)
        proc = run_cli(ws, "verify", "3-run-greet")
        self.assertEqual(0, proc.returncode, proc.stdout + proc.stderr)
        proc = run_cli(ws, "complete", "3-run-greet")
        self.assertEqual(1, proc.returncode, proc.stdout + proc.stderr)
        self.assertIn("approve 3-run-greet", proc.stdout + proc.stderr)
        proc = run_cli(ws, "approve", "3-run-greet")
        self.assertEqual(0, proc.returncode, proc.stdout + proc.stderr)
        proc = run_cli(ws, "complete", "3-run-greet")
        self.assertEqual(0, proc.returncode, proc.stdout + proc.stderr)

    def test_export_report_e2e(self):
        """export writes report.md with metadata, topological node table, deviations, and visual embeds."""
        case, raw = load_case("stale-tutorial")
        expected = copy.deepcopy(raw)
        expected["source_doc"] = str(CORPUS / "stale-tutorial" / "tutorial.md")
        for node in expected["nodes"]:
            if node["id"] == "3-run-greet":
                node["artifacts"] = ["plots/diagram.svg"]

        ws = run_tutorial(case, expected, stop_before="3-run-greet")
        self.addCleanup(shutil.rmtree, ws.parent, ignore_errors=True)
        self.addCleanup(stop_services, ws)

        svg_path = ws / "plots" / "diagram.svg"
        svg_path.parent.mkdir(parents=True, exist_ok=True)
        svg_path.write_text('<svg xmlns="http://www.w3.org/2000/svg"></svg>\n', encoding="utf-8")

        proc = run_cli(ws, "next", "3-run-greet")
        self.assertEqual(0, proc.returncode, proc.stdout + proc.stderr)
        proc = run_cli(
            ws, "deviate", "--check", "--to", "./greet --who Ada",
            "--reason", "v2 renamed --name to --who",
        )
        self.assertEqual(0, proc.returncode, proc.stdout + proc.stderr)
        proc = run_cli(ws, "verify", "3-run-greet")
        self.assertEqual(0, proc.returncode, proc.stdout + proc.stderr)
        proc = run_cli(ws, "complete", "3-run-greet", "--commit")
        self.assertEqual(0, proc.returncode, proc.stdout + proc.stderr)

        out_dir = ws / "docs" / "export"
        proc = run_cli(ws, "export", "--out", "docs/export")
        self.assertEqual(0, proc.returncode, proc.stdout + proc.stderr)

        report_path = out_dir / "report.md"
        self.assertTrue(report_path.is_file(), "missing report.md")
        report = report_path.read_text(encoding="utf-8")

        self.assertIn("# {}".format(expected["title"]), report)
        self.assertIn("- source_doc: `{}`".format(expected["source_doc"]), report)
        self.assertIn("- mode: `assisted`", report)
        self.assertIn("- persistence: `file`", report)
        self.assertIn("- git.branch: `main`", report)
        self.assertIn("- progress: 4 completed / 0 skipped / 5 total", report)

        self.assertIn("## Nodes", report)
        self.assertIn("| ID | Title | Status | Commit | Artifacts | Deviations |", report)
        expected_order = ["1-read", "2-write-greet", "2b-env-note", "3-run-greet", "4-finish"]
        row_positions = [report.index("| {} |".format(nid)) for nid in expected_order]
        self.assertEqual(row_positions, sorted(row_positions))
        self.assertRegex(
            report,
            r"\| 3-run-greet \| [^|]+ \| COMPLETED \| [0-9a-f]{7} \| plots/diagram\.svg \| 1 \|",
        )

        self.assertIn("## Deviations", report)
        self.assertIn(
            "- `3-run-greet` (`check`): `./greet --name Ada` -> `./greet --who Ada`"
            " \u2014 v2 renamed --name to --who",
            report,
        )

        self.assertIn("## Visual Artifacts", report)
        expected_rel = "../../plots/diagram.svg"
        self.assertIn("![plots/diagram.svg]({})".format(expected_rel), report)
        self.assertEqual((out_dir / expected_rel).resolve(), svg_path.resolve())

    def test_cross_os_variants(self):
        """next prints active OS variant details and verify/complete pass end-to-end."""
        _, dag = load_case("cross-os")
        ws = seed_workspace(dag)
        self.addCleanup(shutil.rmtree, ws.parent, ignore_errors=True)
        env = isolated_env(ws.parent / "home")
        active_os = platform.system().lower()

        for node in topological_order(dag["nodes"]):
            proc = run_cli(ws, "next", node["id"])
            self.assertEqual(0, proc.returncode, proc.stdout + proc.stderr)
            if node["id"] == "1-record-platform" and active_os in ("linux", "darwin"):
                self.assertIn(
                    "  variant: {} (overrides: commands, verification)".format(active_os),
                    proc.stdout,
                )
                self.assertIn(
                    "  commands: printf '{}\\n' > platform.txt".format(active_os),
                    proc.stdout,
                )
                self.assertIn("  verification: file_contains platform.txt", proc.stdout)

            for command in effective_node_field(node, "commands"):
                subprocess.run(
                    command, shell=True, cwd=str(ws), env=env,
                    stdin=subprocess.DEVNULL, timeout=60, check=True,
                )

            is_confirm = effective_node_field(node, "verification")["type"] == "confirm"
            verify_args = ["verify", node["id"]] + (["--yes"] if is_confirm else [])
            proc = run_cli(ws, *verify_args)
            self.assertEqual(0, proc.returncode, proc.stdout + proc.stderr)

            complete_args = ["complete", node["id"]]
            dirty = subprocess.run(
                ["git", "status", "--porcelain"], cwd=str(ws), env=env,
                stdout=subprocess.PIPE, universal_newlines=True, check=True,
            ).stdout.strip()
            if dirty:
                complete_args.append("--commit")
            if is_confirm:
                complete_args.append("--yes")
            proc = run_cli(ws, *complete_args)
            self.assertEqual(0, proc.returncode, proc.stdout + proc.stderr)

        if active_os in ("linux", "darwin"):
            self.assertEqual(active_os, (ws / "platform.txt").read_text(encoding="utf-8").strip())
            self.assertEqual("os=" + active_os, (ws / "note.txt").read_text(encoding="utf-8").strip())
        state = json.loads(run_cli(ws, "status", "--json").stdout)
        self.assertEqual({n["id"]: n["status"] for n in state["nodes"]}, {
            "1-record-platform": "completed",
            "2-normalize-note": "completed",
            "3-confirm-setup": "completed",
        })

    def test_in_context_persistence(self):
        """in-context mode keeps all state in --file without creating .tutorial-dag.json."""
        root = Path(tempfile.mkdtemp(prefix="corpus-in-context-"))
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        ws = root / "repo"
        home = root / "home"
        ws.mkdir()
        home.mkdir()
        env = isolated_env(home)
        state_path = root / "state.json"

        for args in (["git", "init", "-q", "-b", "main"],):
            subprocess.run(
                args, cwd=str(ws), env=env, check=True,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        (ws / "README.md").write_text("# Scratch workspace\n", encoding="utf-8")
        subprocess.run(
            ["git", "add", "-A"], cwd=str(ws), env=env, check=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        subprocess.run(
            ["git", "commit", "-q", "-m", "seed"], cwd=str(ws), env=env, check=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )

        init = run_cli(
            ws, "--file", str(state_path), "init",
            "--title", "In-context persistence", "--source", "tutorial.md",
            "--persistence", "in-context",
        )
        self.assertEqual(0, init.returncode, init.stdout + init.stderr)
        self.assertTrue(state_path.is_file())
        self.assertFalse((ws / ".tutorial-dag.json").exists())

        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["nodes"] = [
            {
                "id": "step-1",
                "title": "Create step one file",
                "summary": "Write one.txt in the workspace. Done when one.txt exists.",
                "dependencies": [],
                "action_type": "execute",
                "status": "pending",
                "commit_sha": None,
                "artifacts": ["one.txt"],
                "commands": ["printf 'one\\n' > one.txt"],
                "verification": {"type": "file_exists", "path": "one.txt"},
            },
            {
                "id": "step-2",
                "title": "Create step two file",
                "summary": "Write two.txt using one.txt. Done when two.txt contains two.",
                "dependencies": ["step-1"],
                "action_type": "execute",
                "status": "pending",
                "commit_sha": None,
                "artifacts": ["two.txt"],
                "commands": ["printf 'two\\n' > two.txt"],
                "verification": {"type": "file_contains", "path": "two.txt", "pattern": "two"},
            },
        ]
        state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")

        before = run_cli(ws, "--file", str(state_path), "status", "--json")
        self.assertEqual(0, before.returncode, before.stdout + before.stderr)
        before_doc = json.loads(before.stdout)
        self.assertEqual("in-context", before_doc["persistence"])
        self.assertEqual({n["id"]: n["status"] for n in before_doc["nodes"]}, {
            "step-1": "pending",
            "step-2": "pending",
        })

        for node in state["nodes"]:
            proc = run_cli(ws, "--file", str(state_path), "next", node["id"])
            self.assertEqual(0, proc.returncode, proc.stdout + proc.stderr)
            for command in node["commands"]:
                subprocess.run(
                    command, shell=True, cwd=str(ws), env=env,
                    stdin=subprocess.DEVNULL, timeout=60, check=True,
                )
            proc = run_cli(ws, "--file", str(state_path), "verify", node["id"])
            self.assertEqual(0, proc.returncode, proc.stdout + proc.stderr)
            proc = run_cli(ws, "--file", str(state_path), "complete", node["id"], "--commit")
            self.assertEqual(0, proc.returncode, proc.stdout + proc.stderr)

        after = run_cli(ws, "--file", str(state_path), "status", "--json")
        self.assertEqual(0, after.returncode, after.stdout + after.stderr)
        after_doc = json.loads(after.stdout)
        self.assertEqual("in-context", after_doc["persistence"])
        self.assertIsNone(after_doc["active_node_id"])
        self.assertEqual({n["id"]: n["status"] for n in after_doc["nodes"]}, {
            "step-1": "completed",
            "step-2": "completed",
        })
        self.assertTrue(all(n["commit_sha"] for n in after_doc["nodes"]))
        self.assertFalse((ws / ".tutorial-dag.json").exists())



def draft_workspace(slug: str) -> Path:
    """Init an empty workspace and fill its state with the draft of one corpus tutorial."""
    root = Path(tempfile.mkdtemp(prefix="corpus-draft-"))
    ws = root / "repo"
    home = root / "home"
    ws.mkdir()
    home.mkdir()
    env = isolated_env(home)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=str(ws), env=env, check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    init = run_cli(ws, "init", "--title", slug, "--source", "tutorial.md")
    if init.returncode != 0:
        raise RuntimeError("init failed: " + init.stdout + init.stderr)
    draft = run_cli(ws, "draft", str(CORPUS / slug / "tutorial.md"), "--write")
    if draft.returncode != 0:
        raise RuntimeError("draft --write failed: " + draft.stdout + draft.stderr)
    return root


def draft_state(root: Path) -> dict:
    """Read the live state of a workspace made by ``draft_workspace``."""
    return json.loads((root / "repo" / ".tutorial-dag.json").read_text(encoding="utf-8"))


def scan_tutorial(text: str) -> tuple:
    """Return (H2/H3 heading lines, top-level numbered items) outside code fences."""
    headings, numbered, in_fence = [], [], False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if re.match(r"^##{1,2}\s", line):
            headings.append(line)
        elif re.match(r"^\d+\.\s", line):
            numbered.append(line)
    return headings, numbered


def words(title: str) -> set:
    """Lowercase letter-only words of at least four letters."""
    return {w for w in re.findall(r"[a-z]+", title.lower()) if len(w) >= 4}


class CorpusDraftTest(unittest.TestCase):
    """`draft --write` covers every corpus tutorial (C-G10)."""

    def test_draft_covers_every_corpus_tutorial(self):
        for slug in CORPUS_SLUGS:
            with self.subTest(slug=slug):
                text = (CORPUS / slug / "tutorial.md").read_text(encoding="utf-8")
                headings, numbered = scan_tutorial(text)
                minimum = len(headings) if headings else len(numbered)
                root = draft_workspace(slug)
                self.addCleanup(shutil.rmtree, root, ignore_errors=True)
                nodes = draft_state(root)["nodes"]
                self.assertGreaterEqual(len(nodes), minimum)
                proc = run_cli(root / "repo", "validate")
                self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_draft_score_report(self):
        for slug in CORPUS_SLUGS:
            root = draft_workspace(slug)
            self.addCleanup(shutil.rmtree, root, ignore_errors=True)
            draft_titles = [n["title"] for n in draft_state(root)["nodes"]]
            expected = json.loads((CORPUS / slug / "expected-dag.json").read_text(encoding="utf-8"))
            e = len(expected["nodes"])
            d = len(draft_titles)
            k = sum(
                1 for node in expected["nodes"]
                if any(words(node["title"]) & words(t) for t in draft_titles)
            )
            print("draft {}: nodes {}/{} heading-match {}/{}".format(slug, d, e, k, e))


if __name__ == "__main__":
    unittest.main()
