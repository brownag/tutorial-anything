#!/usr/bin/env python3
"""Track and navigate an interactive tutorial DAG.

Standard library only. The state document (``.tutorial-dag.json``) records a
tutorial decomposed into nodes whose ``dependencies`` form a strict DAG, the
per-node execution status, and the Git checkpoint that made each mutating node
reproducible. See ``references/dag-schema.md`` for the field contract and
``SKILL.md`` for the agent workflow this CLI drives.

Conventions:
  * Paths in ``verification`` / ``artifacts`` and every ``command`` resolve
    against the workspace root: ``--cwd`` if given, else the current directory.
  * This tool never runs a node's ``commands``. It only runs ``verification``.
  * Exit codes: 0 success, 1 verification/validation/operation failure,
    2 usage error.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

STATE_FILENAME = ".tutorial-dag.json"
SCHEMA_VERSION = "1.1"

MODES = ("guided", "assisted", "hands-off")
PERSISTENCE_MODES = ("file", "in-context")
ACTION_TYPES = ("read-only", "instruct", "mutate", "execute")
CHECKPOINTED_ACTIONS = ("mutate", "execute")
STATUSES = ("pending", "completed", "skipped")
VERIFICATION_TYPES = (
    "command",
    "file_exists",
    "file_contains",
    "confirm",
    "http",
    "port_listening",
)
SIDE_EFFECT_SCOPES = ("repo", "user", "system", "remote")
RESIDUE_REASONS = ("not-run", "failed", "irreversible", "remote")
RISK_TIERS = ("low", "elevated", "high")
RISK_SHORT = {"low": "low", "elevated": "elev", "high": "HIGH"}
LOG_DIR_NAME = ".tutorial-logs"
LOG_EXCLUDE_LINE = LOG_DIR_NAME + "/"
SERVICE_STOP_GRACE = 5
SERVICE_POLL = 0.5
SERVICE_SIGNALS = {"TERM": "SIGTERM", "INT": "SIGINT", "KILL": "SIGKILL"}
SERVICE_FIELDS = ("command", "ready", "ready_timeout", "stop_signal")
HIGH_RISK_PATTERNS = (
    (re.compile(r"\bsudo\b"), "sudo"),
    (re.compile(r"\b(curl|wget)\b[^|]*\|\s*(sudo\s+)?(ba|z)?sh\b"), "pipe-to-shell"),
    (re.compile(r"\brm\s+-[a-zA-Z]*r[a-zA-Z]*f?\s+(/|~|\$HOME)"), "recursive-delete"),
    (re.compile(r"\bchmod\s+(-R\s+)?777\b"), "chmod-777"),
    (re.compile(r"(>|\btee\b\s+(-a\s+)?)\s*/(etc|usr|opt|boot|var)/"), "system-path-write"),
    (re.compile(r"\bmkfs\b"), "mkfs"),
    (re.compile(r"\bdd\s+if="), "dd"),
)
GIT_ROLLBACK_MODES = ("none", "branch", "hard")
DERIVED_ORDER = ("ACTIVE", "READY", "PENDING", "COMPLETED", "SKIPPED")

ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
EXCLUDE_LINE = STATE_FILENAME
COMMAND_TIMEOUT = 600
OUTPUT_TAIL = 1200

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_USAGE = 2

NODE_REQUIRED = (
    "id",
    "title",
    "summary",
    "dependencies",
    "action_type",
    "status",
    "commit_sha",
    "artifacts",
    "commands",
    "verification",
)
ROOT_REQUIRED = (
    "schema_version",
    "title",
    "source_doc",
    "mode",
    "persistence",
    "active_node_id",
    "git",
    "nodes",
)
VERIFICATION_REQUIRED = {
    "command": ("command",),
    "file_exists": ("path",),
    "file_contains": ("path", "pattern"),
    "confirm": ("prompt",),
    "http": ("url",),
    "port_listening": ("port",),
}
VERIFICATION_ALLOWED = {
    "command": ("type", "command", "pattern", "expect_exit"),
    "file_exists": ("type", "path"),
    "file_contains": ("type", "path", "pattern", "regex"),
    "confirm": ("type", "prompt"),
    "http": ("type", "url", "expect_status", "pattern", "timeout"),
    "port_listening": ("type", "port", "host", "timeout"),
}


class CliError(Exception):
    """A recoverable, user-facing failure. Carries the process exit code."""

    def __init__(self, message: str, code: int = EXIT_FAIL) -> None:
        super().__init__(message)
        self.message = message
        self.code = code


def now_iso() -> str:
    """Return the current UTC time in the schema's ISO-8601 second form."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def say(message: str = "") -> None:
    print(message)


def warn(message: str) -> None:
    print("warning: {}".format(message), file=sys.stderr)


def err(message: str) -> None:
    print("error: {}".format(message), file=sys.stderr)


def tail(text: str, limit: int = OUTPUT_TAIL) -> str:
    """Return the last ``limit`` characters of ``text``, marked if shortened."""
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return "...(earlier output truncated)...\n" + text[-limit:]


# --------------------------------------------------------------------------
# state I/O


def load_state(path: Path) -> dict:
    """Read and parse the state document without mutating it."""
    if not path.exists():
        raise CliError(
            "no state file at {} (run `init` first, or pass --file)".format(path)
        )
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise CliError("cannot read {}: {}".format(path, exc))
    if not raw.strip():
        raise CliError("{} is empty; expected a JSON object".format(path))
    try:
        state = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CliError(
            "{} is not valid JSON (line {} column {}): {}".format(
                path, exc.lineno, exc.colno, exc.msg
            )
        )
    if not isinstance(state, dict):
        raise CliError("{} must contain a JSON object".format(path))
    return state


def save_state(path: Path, state: dict) -> None:
    """Atomically write the state document and refresh ``updated_at``."""
    state["updated_at"] = now_iso()
    path = path.resolve()
    handle = None
    tmp_path = None
    try:
        fd, tmp_name = tempfile.mkstemp(
            prefix="{}.".format(path.name), suffix=".tmp", dir=str(path.parent)
        )
        tmp_path = Path(tmp_name)
        handle = os.fdopen(fd, "w", encoding="utf-8")
        json.dump(state, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
        handle.close()
        handle = None
        os.replace(str(tmp_path), str(path))
    except OSError as exc:
        raise CliError("cannot write {}: {}".format(path, exc))
    finally:
        if handle is not None:
            handle.close()
        if tmp_path is not None and tmp_path.exists():
            tmp_path.unlink()


# --------------------------------------------------------------------------
# graph helpers


def node_index(state: dict) -> dict:
    """Map node id -> node, ignoring malformed entries (validate reports those)."""
    index = {}
    for node in state.get("nodes") or []:
        if isinstance(node, dict) and isinstance(node.get("id"), str):
            index.setdefault(node["id"], node)
    return index


def node_order(state: dict) -> list:
    """Node ids in document order, the tie-breaker for deterministic output."""
    order = []
    for node in state.get("nodes") or []:
        if isinstance(node, dict) and isinstance(node.get("id"), str):
            if node["id"] not in order:
                order.append(node["id"])
    return order


def dependency_map(state: dict) -> dict:
    """Map node id -> list of dependency ids (only ids that exist)."""
    order = node_order(state)
    known = set(order)
    deps = {}
    for nid in order:
        raw = (node_index(state).get(nid) or {}).get("dependencies") or []
        deps[nid] = [d for d in raw if isinstance(d, str) and d in known]
    return deps


def topological_order(state: dict) -> tuple:
    """Kahn's algorithm, deterministic by document order.

    Returns ``(order, cycle_ids)``. ``cycle_ids`` is empty for a strict DAG;
    otherwise it holds the ids Kahn could not place, i.e. the cycle members.
    """
    deps = dependency_map(state)
    order = node_order(state)
    pos = {nid: i for i, nid in enumerate(order)}
    children = {nid: [] for nid in order}
    indegree = {}
    for nid in order:
        unique = set(deps[nid])
        indegree[nid] = len(unique)
        for dep in unique:
            children[dep].append(nid)

    ready = sorted([nid for nid in order if indegree[nid] == 0], key=lambda n: pos[n])
    result = []
    while ready:
        current = ready.pop(0)
        result.append(current)
        for child in sorted(children[current], key=lambda n: pos[n]):
            indegree[child] -= 1
            if indegree[child] == 0:
                ready.append(child)
                ready.sort(key=lambda n: pos[n])

    if len(result) != len(order):
        placed = set(result)
        return result, [nid for nid in order if nid not in placed]
    return result, []


def relation_maps(state: dict) -> tuple:
    """Return ``(parents, children)`` adjacency maps over known ids."""
    deps = dependency_map(state)
    order = node_order(state)
    parents = {nid: list(deps[nid]) for nid in order}
    children = {nid: [] for nid in order}
    for nid in order:
        for dep in deps[nid]:
            children[dep].append(nid)
    return parents, children


def transitive(nid: str, adjacency: dict) -> list:
    """All nodes reachable from ``nid`` through ``adjacency``, in BFS order."""
    seen = []
    queue = list(adjacency.get(nid, []))
    while queue:
        current = queue.pop(0)
        if current in seen or current == nid:
            continue
        seen.append(current)
        queue.extend(adjacency.get(current, []))
    return seen


def satisfied(status: str) -> bool:
    """A dependency is satisfied once it is completed or deliberately skipped."""
    return status in ("completed", "skipped")


def derived_state(state: dict, nid: str, index: dict, parents: dict) -> str:
    """Collapse stored status + dependency status into the display state."""
    node = index[nid]
    stored = node.get("status")
    if state.get("active_node_id") == nid:
        return "ACTIVE"
    if stored == "completed":
        return "COMPLETED"
    if stored == "skipped":
        return "SKIPPED"
    if all(satisfied((index.get(dep) or {}).get("status")) for dep in parents[nid]):
        return "READY"
    return "PENDING"


def ready_nodes(state: dict) -> list:
    """Ids that are stored-pending with every dependency satisfied, topo-ordered."""
    index = node_index(state)
    parents, _ = relation_maps(state)
    order, _ = topological_order(state)
    active = state.get("active_node_id")
    result = []
    for nid in order:
        if index[nid].get("status") != "pending":
            continue
        if all(satisfied((index.get(dep) or {}).get("status")) for dep in parents[nid]):
            result.append(nid)
    return [nid for nid in result if nid != active]


# --------------------------------------------------------------------------
# validation


def validate_document(state: dict) -> tuple:
    """Check document shape, id integrity, and acyclicity.

    Mirrors ``references/dag-schema.json`` without importing a validator.
    Returns ``(problems, warnings)`` as lists of strings.
    """
    problems = []

    version = state.get("schema_version")
    if not isinstance(version, str) or not version.startswith("1."):
        problems.append(
            "schema_version must be a 1.x string, got {!r}".format(version)
        )

    for field in ("title", "source_doc"):
        value = state.get(field)
        if not isinstance(value, str) or not value.strip():
            problems.append("root field {!r} must be a non-empty string".format(field))

    for field, allowed in (("mode", MODES), ("persistence", PERSISTENCE_MODES)):
        if state.get(field) not in allowed:
            problems.append(
                "root field {!r} must be one of {}, got {!r}".format(
                    field, list(allowed), state.get(field)
                )
            )

    problems.extend(validate_git_block(state.get("git")))

    if "residue" in state:
        problems.extend(validate_residue(state.get("residue")))
    if "services" in state:
        problems.extend(validate_services(state.get("services")))

    nodes = state.get("nodes")
    if not isinstance(nodes, list):
        problems.append("root field 'nodes' must be an array")
        return problems, []
    if nodes:
        nodes_warnings = []
    else:
        # Legal only in the window between `init` and decomposition.
        nodes_warnings = [
            "root field 'nodes' is empty; decompose the tutorial before running `next`"
        ]

    seen = {}
    for position, node in enumerate(nodes):
        problems.extend(validate_node(node, position, seen))

    index = node_index(state)
    order = node_order(state)

    active = state.get("active_node_id")
    if active is not None:
        if not isinstance(active, str) or active not in index:
            problems.append(
                "active_node_id {!r} does not name a node".format(active)
            )

    for nid in order:
        raw = index[nid].get("dependencies") if isinstance(index[nid], dict) else None
        if not isinstance(raw, list):
            continue
        for dep in raw:
            if isinstance(dep, str) and dep not in index:
                problems.append(
                    "node '{}' depends on unknown id '{}'".format(nid, dep)
                )
        if isinstance(raw, list) and nid in raw:
            problems.append("node '{}' depends on itself".format(nid))

    _, cycle = topological_order(state)
    if cycle:
        problems.append(
            "cycle detected involving: {}".format(", ".join(sorted(cycle)))
        )

    warnings = finalize_warnings(state, problems, nodes_warnings + collect_warnings(state))
    return problems, warnings


def collect_warnings(state: dict) -> list:
    """Non-fatal observations worth showing the agent."""
    warnings = []
    index = node_index(state)
    for nid in node_order(state):
        node = index[nid]
        if node.get("action_type") == "mutate" and not node.get("artifacts"):
            warnings.append(
                "node '{}' is a mutate node but lists no artifacts".format(nid)
            )
        scopes = [s for s in node.get("side_effects", []) if s != "repo"]
        if isinstance(node.get("side_effects"), list) and scopes and not node.get("undo"):
            warnings.append(
                "node '{}' has {} side effects but no undo (rewinds cannot revert it)".format(
                    nid, ", ".join(scopes)
                )
            )
    return warnings


def finalize_warnings(state: dict, problems: list, warnings: list) -> list:
    """Drop cosmetic warnings once the document is already known to be broken."""
    return warnings if not problems else []


def validate_git_block(git) -> list:
    """Validate the root ``git`` block."""
    problems = []
    if not isinstance(git, dict):
        return ["root field 'git' must be an object"]
    for field in ("branch", "initial_commit_sha", "initial_dirty", "exclude_configured"):
        if field not in git:
            problems.append("git block missing required field {!r}".format(field))
    branch = git.get("branch")
    if branch is not None and not isinstance(branch, str):
        problems.append("git.branch must be a string or null")
    sha = git.get("initial_commit_sha")
    if not isinstance(sha, str) or (sha != "" and not SHA_RE.match(sha)):
        problems.append(
            "git.initial_commit_sha must be a 40-char hex sha or \"\" (no commits at init)"
        )
    for field in ("initial_dirty", "exclude_configured"):
        if not isinstance(git.get(field), bool):
            problems.append("git.{!r} must be a boolean".format(field))
    return problems


def validate_node(node, position: int, seen: dict) -> list:
    """Validate one node object; ``seen`` accumulates ids for uniqueness checks."""
    label = "nodes[{}]".format(position)
    if not isinstance(node, dict):
        return ["{} must be an object".format(label)]
    nid = node.get("id")
    if isinstance(nid, str) and ID_RE.match(nid):
        label = "node '{}'".format(nid)
    else:
        return ["{} field 'id' must match {}".format(label, ID_RE.pattern)]

    problems = []
    if nid in seen:
        problems.append("{}: duplicate id (first seen at nodes[{}])".format(label, seen[nid]))
    seen[nid] = position

    for field in NODE_REQUIRED:
        if field not in node:
            problems.append("{} missing required field {!r}".format(label, field))

    for field in ("title", "summary"):
        value = node.get(field)
        if field in node and (not isinstance(value, str) or not value.strip()):
            problems.append("{} field {!r} must be a non-empty string".format(label, field))

    deps = node.get("dependencies")
    if "dependencies" in node:
        if not isinstance(deps, list):
            problems.append("{} field 'dependencies' must be an array".format(label))
        else:
            if len(set(deps)) != len(deps):
                problems.append("{} has duplicate dependencies".format(label))
            for dep in deps:
                if not isinstance(dep, str) or not ID_RE.match(dep):
                    problems.append(
                        "{} dependency {!r} is not a valid id".format(label, dep)
                    )

    for field, allowed in (
        ("action_type", ACTION_TYPES),
        ("status", STATUSES),
    ):
        if field in node and node.get(field) not in allowed:
            problems.append(
                "{} field {!r} must be one of {}, got {!r}".format(
                    label, field, list(allowed), node.get(field)
                )
            )

    if "commit_sha" in node:
        sha = node.get("commit_sha")
        if sha is not None and (not isinstance(sha, str) or not re.match(r"^[0-9a-f]{7,40}$", sha)):
            problems.append(
                "{} field 'commit_sha' must be null or 7-40 hex chars".format(label)
            )

    for field in ("artifacts", "commands"):
        value = node.get(field)
        if field in node and (
            not isinstance(value, list)
            or any(not isinstance(item, str) or not item for item in value)
        ):
            problems.append(
                "{} field {!r} must be an array of non-empty strings".format(label, field)
            )

    if "verification" in node:
        problems.extend(validate_verification(node["verification"], label))

    if "side_effects" in node:
        value = node.get("side_effects")
        if (
            not isinstance(value, list)
            or not value
            or len(set(value)) != len(value)
            or any(scope not in SIDE_EFFECT_SCOPES for scope in value)
        ):
            problems.append(
                "{} field 'side_effects' must be a non-empty unique array of {}".format(
                    label, list(SIDE_EFFECT_SCOPES)
                )
            )
    if "undo" in node:
        value = node.get("undo")
        if not isinstance(value, list) or any(
            not isinstance(cmd, str) or not cmd.strip() for cmd in value
        ):
            problems.append(
                "{} field 'undo' must be an array of non-empty command strings".format(label)
            )
    if "service" in node:
        problems.extend(validate_service(node["service"], label))
    if "risk" in node and node.get("risk") not in RISK_TIERS:
        problems.append(
            "{} field 'risk' must be one of {}, got {!r}".format(
                label, list(RISK_TIERS), node.get("risk")
            )
        )
    if "approved" in node:
        value = node.get("approved")
        if (
            not isinstance(value, dict)
            or value.get("tier") not in RISK_TIERS
            or not isinstance(value.get("at"), str)
        ):
            problems.append(
                "{} field 'approved' must be an object with 'tier' and 'at' (C-G6)".format(label)
            )
    return problems


def inspected_commands(node: dict) -> list:
    """Commands the risk classifier reads: commands, undo, and a command check (C-G6)."""
    commands = list(node.get("commands") or []) + list(node.get("undo") or [])
    verification = node.get("verification") or {}
    if verification.get("type") == "command" and isinstance(verification.get("command"), str):
        commands.append(verification["command"])
    service = node.get("service") or {}
    if isinstance(service.get("command"), str):
        commands.append(service["command"])
    return commands


def risk_tier(node: dict) -> tuple:
    """Effective (tier, labels) for a node: max(computed, declared) (C-G6)."""
    labels = []
    for command in inspected_commands(node):
        for pattern, label in HIGH_RISK_PATTERNS:
            if pattern.search(command) and label not in labels:
                labels.append(label)
    if labels:
        computed = "high"
    elif set(node_scopes(node)) - {"repo"}:
        computed = "elevated"
    else:
        computed = "low"
    declared = node.get("risk") if node.get("risk") in RISK_TIERS else "low"
    tier = max(computed, declared, key=RISK_TIERS.index)
    return tier, labels


def approval_covers(node: dict, tier: str) -> bool:
    """An approval covers a node while its effective tier is at or below the recorded one."""
    approved = node.get("approved")
    if not isinstance(approved, dict) or approved.get("tier") not in RISK_TIERS:
        return False
    return RISK_TIERS.index(tier) <= RISK_TIERS.index(approved["tier"])


def risk_blocked(state: dict, node: dict) -> bool:
    """True when hands-off mode forbids acting on a high-risk, unapproved node."""
    tier, _ = risk_tier(node)
    return (
        state.get("mode") == "hands-off"
        and tier == "high"
        and not approval_covers(node, tier)
    )


def validate_service(service, label: str) -> list:
    """Validate a node's ``service`` block (C-G3)."""
    if not isinstance(service, dict):
        return ["{} field 'service' must be an object".format(label)]
    problems = []
    for field in service:
        if field not in SERVICE_FIELDS:
            problems.append("{} service does not accept {!r}".format(label, field))
    command = service.get("command")
    if not isinstance(command, str) or not command.strip():
        problems.append("{} service requires a non-empty 'command'".format(label))
    if "ready" not in service:
        problems.append("{} service requires 'ready'".format(label))
    else:
        ready = service["ready"]
        if not isinstance(ready, dict) or ready.get("type") == "confirm":
            problems.append(
                "{} service.ready must be a verification other than confirm".format(label)
            )
        else:
            problems.extend(validate_verification(ready, label + " service"))
    if "ready_timeout" in service and not is_positive_int(service["ready_timeout"]):
        problems.append("{} service.ready_timeout must be a positive integer".format(label))
    if "stop_signal" in service and service["stop_signal"] not in SERVICE_SIGNALS:
        problems.append(
            "{} service.stop_signal must be one of {}".format(label, list(SERVICE_SIGNALS))
        )
    return problems


def validate_services(services) -> list:
    """Validate the root ``services`` map of live processes (C-G3)."""
    if not isinstance(services, dict):
        return ["root field 'services' must be an object"]
    problems = []
    for nid, record in services.items():
        label = "services[{!r}]".format(nid)
        if not isinstance(record, dict):
            problems.append("{} must be an object".format(label))
            continue
        for field in ("pid", "pgid"):
            if not is_positive_int(record.get(field)):
                problems.append("{} field {!r} must be a positive integer".format(label, field))
        for field in ("command", "log", "started_at"):
            value = record.get(field)
            if not isinstance(value, str) or not value.strip():
                problems.append("{} field {!r} must be a non-empty string".format(label, field))
    return problems


def validate_residue(residue) -> list:
    """Validate the root ``residue`` array (C-G2)."""
    if not isinstance(residue, list):
        return ["root field 'residue' must be an array"]
    problems = []
    for position, entry in enumerate(residue):
        label = "residue[{}]".format(position)
        if not isinstance(entry, dict):
            problems.append("{} must be an object".format(label))
            continue
        node_id = entry.get("node_id")
        if not isinstance(node_id, str) or not ID_RE.match(node_id):
            problems.append("{} field 'node_id' must be a valid id".format(label))
        scopes = entry.get("scopes")
        if (
            not isinstance(scopes, list)
            or not scopes
            or any(scope not in SIDE_EFFECT_SCOPES for scope in scopes)
        ):
            problems.append(
                "{} field 'scopes' must be an array of {}".format(label, list(SIDE_EFFECT_SCOPES))
            )
        undo = entry.get("undo")
        if not isinstance(undo, list) or any(
            not isinstance(cmd, str) or not cmd.strip() for cmd in undo
        ):
            problems.append(
                "{} field 'undo' must be an array of non-empty command strings".format(label)
            )
        if entry.get("reason") not in RESIDUE_REASONS:
            problems.append(
                "{} field 'reason' must be one of {}, got {!r}".format(
                    label, list(RESIDUE_REASONS), entry.get("reason")
                )
            )
        recorded = entry.get("recorded_at")
        if not isinstance(recorded, str) or not recorded.strip():
            problems.append("{} field 'recorded_at' must be a non-empty string".format(label))
    return problems


def is_positive_int(value) -> bool:
    """True for ints >= 1 (bools excluded)."""
    return isinstance(value, int) and not isinstance(value, bool) and value >= 1


def is_port(value) -> bool:
    """True for TCP port numbers 1-65535 (C-G3)."""
    return is_positive_int(value) and value <= 65535


def validate_verification(verification, label: str) -> list:
    """Validate the per-node verification gate against its variant contract."""
    if not isinstance(verification, dict):
        return ["{} field 'verification' must be an object".format(label)]
    kind = verification.get("type")
    if kind not in VERIFICATION_TYPES:
        return [
            "{} verification.type must be one of {}, got {!r}".format(
                label, list(VERIFICATION_TYPES), kind
            )
        ]
    problems = []
    for field in VERIFICATION_REQUIRED[kind]:
        if field == "port":
            continue  # integer, checked with the other port_listening fields below
        value = verification.get(field)
        if not isinstance(value, str) or not value.strip():
            problems.append(
                "{} verification of type '{}' requires non-empty {!r}".format(
                    label, kind, field
                )
            )
    for field in verification:
        if field not in VERIFICATION_ALLOWED[kind]:
            problems.append(
                "{} verification of type '{}' does not accept {!r}".format(
                    label, kind, field
                )
            )
    if kind == "port_listening":
        if not is_port(verification.get("port")):
            problems.append("{} verification.port must be an integer 1-65535".format(label))
        if "host" in verification and (
            not isinstance(verification["host"], str) or not verification["host"].strip()
        ):
            problems.append("{} verification.host must be a non-empty string".format(label))
        if "timeout" in verification and not is_positive_int(verification["timeout"]):
            problems.append("{} verification.timeout must be a positive integer".format(label))
    if kind == "http":
        url = verification.get("url")
        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            problems.append("{} verification.url must start with http:// or https://".format(label))
        if "expect_status" in verification and not (
            is_positive_int(verification["expect_status"])
            and 100 <= verification["expect_status"] <= 599
        ):
            problems.append("{} verification.expect_status must be an HTTP status code".format(label))
        if "timeout" in verification and not is_positive_int(verification["timeout"]):
            problems.append("{} verification.timeout must be a positive integer".format(label))
        if "pattern" in verification:
            problems.extend(bad_regex(verification["pattern"], label, "verification.pattern"))
    if kind == "command" and "expect_exit" in verification:
        expect = verification.get("expect_exit")
        if not isinstance(expect, int) or isinstance(expect, bool) or expect < 0:
            problems.append(
                "{} verification.expect_exit must be a non-negative integer".format(label)
            )
    if kind == "file_contains" and "regex" in verification:
        if not isinstance(verification.get("regex"), bool):
            problems.append("{} verification.regex must be a boolean".format(label))
    if kind == "command" and "pattern" in verification:
        problems.extend(bad_regex(verification["pattern"], label, "verification.pattern"))
    if kind == "file_contains" and verification.get("regex") and "pattern" in verification:
        problems.extend(bad_regex(verification["pattern"], label, "verification.pattern"))
    return problems


def bad_regex(pattern, label: str, field: str) -> list:
    """Return a problem string when ``pattern`` is not a usable regex."""
    if not isinstance(pattern, str):
        return ["{} {} must be a string".format(label, field)]
    try:
        re.compile(pattern)
    except re.error as exc:
        return ["{} {} is not a valid regex: {}".format(label, field, exc)]
    return []


# --------------------------------------------------------------------------
# workspace and git


def resolve_workspace(cwd_arg) -> Path:
    """Return the absolute workspace root that paths and commands use."""
    root = Path(cwd_arg) if cwd_arg else Path.cwd()
    if not root.is_dir():
        raise CliError("workspace {} is not a directory".format(root), EXIT_USAGE)
    return root.resolve()


def run_git(args: list, workspace: Path) -> tuple:
    """Run ``git`` in ``workspace``. Returns ``(returncode, stdout, stderr)``."""
    command = ["git", "--no-pager"] + list(args)
    try:
        proc = subprocess.run(
            command,
            cwd=str(workspace),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
        )
    except OSError as exc:
        raise CliError("cannot run {}: {}".format(" ".join(command), exc))
    return proc.returncode, (proc.stdout or "").strip(), (proc.stderr or "").strip()


def require_git(workspace: Path) -> None:
    """Fail with actionable guidance unless ``workspace`` is a Git work tree."""
    code, _, stderr = run_git(["rev-parse", "--is-inside-work-tree"], workspace)
    if code != 0:
        raise CliError(
            "{} is not a Git work tree. Run `git init` (or pass --cwd inside a "
            "repository) and retry.\n  git: {}".format(workspace, stderr or "not a git repository")
        )


def git_common_dir(workspace: Path) -> Path:
    """Absolute path to the shared ``.git`` directory (work-tree aware)."""
    code, out, stderr = run_git(["rev-parse", "--git-common-dir"], workspace)
    if code != 0:
        raise CliError("cannot locate the git directory: {}".format(stderr))
    path = Path(out)
    if not path.is_absolute():
        path = workspace / path
    return path.resolve()


def ensure_excluded(workspace: Path, line: str = EXCLUDE_LINE) -> bool:
    """Add ``line`` (default: the state file) to ``.git/info/exclude``. Returns True if it changed."""
    exclude = git_common_dir(workspace) / "info" / "exclude"
    existing = ""
    if exclude.exists():
        try:
            existing = exclude.read_text(encoding="utf-8")
        except OSError as exc:
            raise CliError("cannot read {}: {}".format(exclude, exc))
    for known in existing.splitlines():
        if known.strip() == line:
            return False
    exclude.parent.mkdir(parents=True, exist_ok=True)
    with exclude.open("a", encoding="utf-8") as handle:
        if existing and not existing.endswith("\n"):
            handle.write("\n")
        handle.write("{}\n".format(line))
    return True


def current_branch(workspace: Path) -> str:
    """Current branch name, or ``HEAD`` when detached."""
    code, out, _ = run_git(["rev-parse", "--abbrev-ref", "HEAD"], workspace)
    return out if code == 0 else ""


def head_sha(workspace: Path) -> str:
    """Full sha of ``HEAD``, or ``\"\"`` when the repository has no commits yet."""
    code, out, _ = run_git(["rev-parse", "--verify", "HEAD"], workspace)
    return out if code == 0 else ""


def is_dirty(workspace: Path) -> bool:
    """True when tracked or untracked (non-excluded) changes exist."""
    code, out, _ = run_git(["status", "--porcelain"], workspace)
    return bool(out)


def resolve_sha(workspace: Path, ref: str) -> str:
    """Resolve ``ref`` to a full sha or fail."""
    code, out, stderr = run_git(["rev-parse", "--verify", "{}^{{commit}}".format(ref)], workspace)
    if code != 0:
        raise CliError("cannot resolve git revision {!r}: {}".format(ref, stderr or "unknown revision"))
    return out


def commit_all(workspace: Path, message: str) -> str:
    """Stage everything, commit, and return the new ``HEAD`` sha."""
    code, _, stderr = run_git(["add", "-A"], workspace)
    if code != 0:
        raise CliError("git add -A failed: {}".format(stderr))
    code, _, stderr = run_git(["commit", "--no-verify", "-m", message], workspace)
    if code != 0:
        raise CliError(
            "git commit failed: {}".format(stderr or "nothing to commit or commit rejected")
        )
    return head_sha(workspace)


def checkout_branch(workspace: Path, branch: str, sha: str) -> None:
    """Create ``branch`` at ``sha`` and check it out."""
    code, _, stderr = run_git(["checkout", "-b", branch, sha], workspace)
    if code != 0:
        raise CliError("git checkout -b {} {} failed: {}".format(branch, sha[:12], stderr))


def reset_hard(workspace: Path, sha: str) -> None:
    """Hard-reset the current branch to ``sha`` (excluded files are untouched)."""
    code, _, stderr = run_git(["reset", "--hard", sha], workspace)
    if code != 0:
        raise CliError("git reset --hard {} failed: {}".format(sha[:12], stderr))


def branch_exists(workspace: Path, branch: str) -> bool:
    """True when ``branch`` already exists locally."""
    code, _, _ = run_git(["rev-parse", "--verify", "refs/heads/{}".format(branch)], workspace)
    return code == 0


# --------------------------------------------------------------------------
# verification


def workspace_relative(workspace: Path, raw: str, label: str) -> Path:
    """Resolve a verification path, refusing anything that escapes the workspace."""
    candidate = Path(raw)
    if candidate.is_absolute():
        resolved = candidate
    else:
        resolved = workspace / candidate
    resolved = Path(os.path.normpath(str(resolved)))
    if resolved != workspace and workspace not in resolved.parents:
        raise CliError(
            "{} path {!r} escapes the workspace root {}".format(label, raw, workspace)
        )
    return resolved


def check_http(verification: dict) -> tuple:
    """GET the URL and compare the status (and optional body pattern) (C-G3).

    Any HTTP status is compared to ``expect_status``; only transport failures fail
    before a status arrives. Proxies are ignored so local checks stay local.
    """
    url = verification["url"]
    expect = verification.get("expect_status", 200)
    timeout = verification.get("timeout", 5)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(url, timeout=timeout) as resp:
            status = resp.status
            body = resp.read(65536).decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        status, body = exc.code, ""
    except (OSError, ValueError) as exc:
        return False, ["FAIL: cannot reach {}: {}".format(url, exc)]
    lines = ["GET {} -> {} (expected {})".format(url, status, expect)]
    if status != expect:
        return False, lines + ["FAIL: status {} != {}".format(status, expect)]
    pattern = verification.get("pattern")
    if pattern:
        try:
            matched = re.search(pattern, body)
        except re.error as exc:
            return False, lines + ["FAIL: verification.pattern is not a valid regex: {}".format(exc)]
        if not matched:
            return False, lines + ["FAIL: body did not match pattern {!r}".format(pattern)]
        lines.append("matched pattern {!r}".format(pattern))
    return True, lines + ["PASS"]


def check_port(verification: dict) -> tuple:
    """Succeed when a TCP connection to host:port is accepted (C-G3)."""
    host = verification.get("host", "127.0.0.1")
    port = verification["port"]
    timeout = verification.get("timeout", 2)
    try:
        socket.create_connection((host, port), timeout=timeout).close()
    except OSError as exc:
        return False, ["FAIL: nothing accepts connections on {}:{} ({})".format(host, port, exc)]
    return True, ["PASS: {}:{} accepts connections".format(host, port)]


def run_verification(node: dict, workspace: Path, assume_yes: bool) -> tuple:
    """Execute a node's gate. Returns ``(passed, lines)``."""
    verification = node.get("verification") or {}
    kind = verification.get("type")
    label = "node '{}'".format(node.get("id"))

    if kind == "command":
        command = verification["command"]
        expect = verification.get("expect_exit", 0)
        try:
            proc = subprocess.run(
                command,
                shell=True,
                cwd=str(workspace),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                universal_newlines=True,
                timeout=COMMAND_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            return False, [
                "command timed out after {}s: {}".format(COMMAND_TIMEOUT, command)
            ]
        except OSError as exc:
            return False, ["could not run command {!r}: {}".format(command, exc)]
        output = proc.stdout or ""
        lines = [
            "$ {}".format(command),
            "exit code {} (expected {})".format(proc.returncode, expect),
        ]
        if output.strip():
            lines.append("--- output (last {} chars) ---".format(OUTPUT_TAIL))
            lines.append(tail(output))
        if proc.returncode != expect:
            lines.append("FAIL: exit code {} != {}".format(proc.returncode, expect))
            return False, lines
        pattern = verification.get("pattern")
        if pattern:
            try:
                matched = re.search(pattern, output)
            except re.error as exc:
                return False, lines + ["FAIL: verification.pattern is not a valid regex: {}".format(exc)]
            if not matched:
                lines.append("FAIL: output did not match pattern {!r}".format(pattern))
                return False, lines
            lines.append("matched pattern {!r}".format(pattern))
        lines.append("PASS")
        return True, lines

    if kind == "file_exists":
        path = workspace_relative(workspace, verification["path"], "file_exists")
        if path.exists():
            kind_label = "dir" if path.is_dir() else "file"
            return True, ["PASS: {} {} exists".format(kind_label, path)]
        return False, ["FAIL: no {} at {}".format("path", path)]

    if kind == "file_contains":
        path = workspace_relative(workspace, verification["path"], "file_contains")
        if not path.is_file():
            return False, ["FAIL: cannot read {}".format(path)]
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return False, ["FAIL: cannot read {}: {}".format(path, exc)]
        pattern = verification["pattern"]
        if verification.get("regex"):
            try:
                matched = re.search(pattern, text)
            except re.error as exc:
                return False, ["FAIL: pattern is not a valid regex: {}".format(exc)]
            if matched:
                return True, [
                    "PASS: {} matches /{}/".format(path, pattern),
                    "  matched: {}".format(tail(matched.group(0), 200) or "<empty match>"),
                ]
            return False, ["FAIL: {} does not match /{}/".format(path, pattern)]
        if pattern in text:
            return True, ["PASS: {} contains {!r}".format(path, pattern)]
        return False, [
            "FAIL: {} does not contain {!r}".format(path, tail(pattern, 200)),
            "  (set verification.regex true for pattern matching)",
        ]

    if kind == "confirm":
        prompt = verification["prompt"]
        if assume_yes:
            return True, ["PASS: human confirmed {!r}".format(prompt)]
        return False, [
            "CONFIRMATION NEEDED: {}".format(prompt),
            "  re-run with --yes once the human has confirmed.",
        ]

    if kind == "http":
        return check_http(verification)

    if kind == "port_listening":
        return check_port(verification)

    raise CliError(
        "{} has unsupported verification type {!r}".format(label, kind), EXIT_USAGE
    )


# --------------------------------------------------------------------------
# shared view helpers


def counts(state: dict) -> tuple:
    """Return ``(completed, skipped, total)`` for the progress readout."""
    nodes = [n for n in state.get("nodes") or [] if isinstance(n, dict)]
    total = len(nodes)
    completed = len([n for n in nodes if n.get("status") == "completed"])
    skipped = len([n for n in nodes if n.get("status") == "skipped"])
    return completed, skipped, total


def header_line(state: dict, workspace: Path) -> str:
    """The compact turn header the skill prints on every turn."""
    completed, skipped, total = counts(state)
    branch = current_branch(workspace) or state.get("git", {}).get("branch") or "?"
    progress = "{}/{}".format(completed, total)
    if skipped:
        progress += " ({} skip)".format(skipped)
    active = state.get("active_node_id") or "none"
    unlocked = ready_nodes(state)
    unlocked_text = ", ".join(unlocked[:4]) if unlocked else "none"
    if len(unlocked) > 4:
        unlocked_text += ", +{}".format(len(unlocked) - 4)
    residue_count = len(state.get("residue") or [])
    residue_text = " | residue: {}".format(residue_count) if residue_count else ""
    svc_count = len(state.get("services") or {})
    svc_text = " | svc: {}".format(svc_count) if svc_count else ""
    return "[{} | {} | {} | {} | active: {} | next: {}{}{}]".format(
        state.get("mode"),
        state.get("persistence"),
        branch,
        progress,
        active,
        unlocked_text,
        residue_text,
        svc_text,
    )


def short(sha, width: int = 7):
    """Shorten a commit sha for display."""
    if not sha:
        return "-"
    return sha[:width]


def render_graph(state: dict) -> list:
    """Topologically ordered, aligned node table."""
    index = node_index(state)
    parents, _ = relation_maps(state)
    order, cycle = topological_order(state)
    if cycle:
        order = order + sorted(cycle)
    rows = []
    for nid in order:
        node = index[nid]
        state_label = derived_state(state, nid, index, parents)
        deps = ", ".join(parents[nid]) if parents[nid] else "-"
        tier, _ = risk_tier(node)
        rows.append(
            (
                "> " if state.get("active_node_id") == nid else "  ",
                nid,
                state_label,
                RISK_SHORT[tier],
                node.get("action_type", "?"),
                deps,
                short(node.get("commit_sha")),
                node.get("title", ""),
            )
        )
    if not rows:
        return ["  (no nodes yet - decompose the tutorial, then re-run validate)"]

    headers = ("  ", "ID", "STATE", "RISK", "ACTION", "DEPS", "COMMIT", "TITLE")
    last = len(headers) - 1
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            if i == last:
                continue
            widths[i] = max(widths[i], len(str(cell)))
    lines = []
    template = "  ".join("{{:{}}}".format(w) if i else "{{:{}s}}".format(w) for i, w in enumerate(widths))
    lines.append(template.format(*headers).rstrip())
    lines.append(template.format(*["  "] + ["-" * w for w in widths[1:last]] + ["-"]).rstrip())
    for row in rows:
        title = row[last]
        if len(title) > 52:
            title = title[:49] + "..."
        lines.append((template.format(*(list(row[:last]) + [title]))).rstrip())
    return lines


def require_active_node(state: dict) -> str:
    """Return the active node id or explain how to set one."""
    active = state.get("active_node_id")
    if not active:
        ready = ready_nodes(state)
        if len(ready) == 1:
            raise CliError(
                "no active node; run `next` (uniquely ready: {})".format(ready[0])
            )
        raise CliError(
            "no active node; run `next <id>` with one of: {}".format(
                ", ".join(ready) if ready else "<none ready>"
            )
        )
    return active


def get_node(state: dict, nid: str) -> dict:
    """Fetch a node by id or fail."""
    index = node_index(state)
    if nid not in index:
        raise CliError(
            "unknown node id {!r}. Known ids: {}".format(nid, ", ".join(node_order(state)))
        )
    return index[nid]


def resolve_target(state: dict, node_id, workspace: Path) -> str:
    """Use the explicit id if given, else the active node."""
    if node_id:
        get_node(state, node_id)
        return node_id
    return require_active_node(state)


def summary(state: dict, nid: str) -> str:
    """One-line node description for messages."""
    node = get_node(state, nid)
    return "{} ({})".format(nid, node.get("action_type"))


# --------------------------------------------------------------------------
# subcommands


def cmd_init(args, state_path: Path, workspace: Path) -> int:
    """Create a fresh state document and hide it from Git."""
    if state_path.exists() and not args.force:
        raise CliError(
            "{} already exists. Resume it, or pass --force to start over "
            "(--force discards recorded progress).".format(state_path)
        )
    require_git(workspace)

    if args.git_branch:
        if branch_exists(workspace, args.git_branch):
            code, _, stderr = run_git(["checkout", args.git_branch], workspace)
            if code != 0:
                raise CliError(
                    "cannot check out existing branch {}: {}".format(args.git_branch, stderr)
                )
        else:
            code, _, stderr = run_git(["checkout", "-b", args.git_branch], workspace)
            if code != 0:
                raise CliError(
                    "cannot create branch {}: {}".format(args.git_branch, stderr)
                )

    excluded = ensure_excluded(workspace)
    ensure_excluded(workspace, LOG_EXCLUDE_LINE)
    sha = head_sha(workspace)
    if not sha:
        warn(
            "repository has no commits yet; git.initial_commit_sha is empty and "
            "`jump --git-rollback` will refuse to run until a first commit exists"
        )

    state = {
        "schema_version": SCHEMA_VERSION,
        "title": args.title,
        "source_doc": args.source,
        "mode": args.mode,
        "persistence": args.persistence,
        "active_node_id": None,
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "git": {
            "branch": current_branch(workspace),
            "initial_commit_sha": sha,
            "initial_dirty": is_dirty(workspace),
            "exclude_configured": True,
        },
        "nodes": [],
    }
    save_state(state_path, state)

    say("initialised {}".format(state_path))
    say("  title       {}".format(args.title))
    say("  source      {}".format(args.source))
    say("  mode        {}".format(args.mode))
    say("  persistence {}".format(args.persistence))
    say("  branch      {}".format(state["git"]["branch"]))
    say("  HEAD        {}".format(short(sha, 12) if sha else "<no commits>"))
    say(
        "  exclude     {} (.git/info/exclude)".format(
            "added {}".format(EXCLUDE_LINE) if excluded else "{} already present".format(EXCLUDE_LINE)
        )
    )
    if state["git"]["initial_dirty"]:
        warn(
            "workspace is dirty; commit or stash first so the first tutorial "
            "checkpoint contains only tutorial changes"
        )
    say("")
    say("next: decompose {} into nodes, write them into the 'nodes' array,".format(args.source))
    say("      then run `validate` and `next`.")
    return EXIT_OK


def cmd_validate(args, state_path: Path, workspace: Path) -> int:
    """Report document, id-integrity, and cycle problems."""
    state = load_state(state_path)
    problems, warnings = validate_document(state)
    for problem in problems:
        err(problem)
    for message in warnings:
        warn(message)
    if problems:
        err("{} problem(s) in {}".format(len(problems), state_path))
        return EXIT_FAIL
    order, _ = topological_order(state)
    completed, skipped, total = counts(state)
    say(
        "OK {} - {} node(s), strict DAG, topological order: {}".format(
            state_path, total, " -> ".join(order) if order else "(empty)"
        )
    )
    say(
        "     completed {} / skipped {} / total {}; active {}".format(
            completed, skipped, total, state.get("active_node_id") or "none"
        )
    )
    return EXIT_OK


def cmd_status(args, state_path: Path, workspace: Path) -> int:
    """Print the turn header, the graph, and (with --json) the raw document."""
    state = load_state(state_path)
    problems, warnings = validate_document(state)
    for problem in problems:
        err(problem)
    for message in warnings:
        warn(message)
    if problems:
        err("{} problem(s); run `validate` for detail".format(len(problems)))
        return EXIT_FAIL

    if args.json:
        say(json.dumps(state, indent=2, ensure_ascii=False))
        return EXIT_OK

    say(header_line(state, workspace))
    say("")
    for line in render_graph(state):
        say(line)
    say("")
    for entry in state.get("residue") or []:
        undo = entry.get("undo") or []
        say(
            "residue  {} [{}] {}{}".format(
                entry["node_id"],
                ", ".join(entry.get("scopes") or []),
                entry.get("reason"),
                " undo: {}".format("; ".join(undo)) if undo else "",
            )
        )
    if state.get("residue"):
        say("")
    services = state.get("services") or {}
    for nid in sorted(services):
        record = services[nid]
        say(
            "services: {} (pid {}, {})".format(
                nid, record["pid"], "running" if pid_alive(record["pid"]) else "dead"
            )
        )
    if services:
        say("")
    index = node_index(state)
    parents, _ = relation_maps(state)
    active = state.get("active_node_id")
    if active:
        node = index[active]
        say("active  {}".format(summary(state, active)))
        say("  {} {}".format(">", node.get("summary", "")))
        for command in node.get("commands") or []:
            say("  $ {}".format(command))
        for artifact in node.get("artifacts") or []:
            say("  * {}".format(artifact))
        verification = node.get("verification") or {}
        say("  verify: {}".format(json.dumps(verification, ensure_ascii=False)))
        unmet = [
            dep
            for dep in parents[active]
            if (index[dep].get("status") not in ("completed", "skipped"))
        ]
        if unmet:
            warn("active node still has unmet dependencies: {}".format(", ".join(unmet)))
    else:
        ready = ready_nodes(state)
        if len(ready) == 1:
            say("no active node; `next` selects {}".format(ready[0]))
        elif ready:
            say("no active node; {} nodes ready: {}".format(len(ready), ", ".join(ready)))
        else:
            say("no active node and nothing is ready")
    return EXIT_OK


def check_risk(state: dict, nid: str) -> None:
    """Gate a node on its risk tier (C-G6). Only hands-off ever blocks."""
    node = node_index(state)[nid]
    tier, labels = risk_tier(node)
    if tier != "high":
        return
    detail = ", ".join(labels) or "declared risk high"
    if risk_blocked(state, node):
        raise CliError(
            "'{}' is high risk ({}) and hands-off mode will not run it until a person "
            "approves. After they say yes, run `approve {}`.".format(nid, detail, nid)
        )
    say("risk: high ({})".format(detail))


def cmd_approve(args, state_path: Path, workspace: Path) -> int:
    """Record a person's approval of a node's current risk tier (C-G6)."""
    state = load_state(state_path)
    problems, _ = validate_document(state)
    if problems:
        for problem in problems:
            err(problem)
        return EXIT_FAIL
    node = get_node(state, args.node_id)
    tier, labels = risk_tier(node)
    node["approved"] = {"tier": tier, "at": now_iso()}
    save_state(state_path, state)
    say(
        "approved {} at risk {} ({})".format(
            args.node_id, tier, ", ".join(labels) or "declared"
        )
    )
    say(header_line(state, workspace))
    return EXIT_OK


def cmd_next(args, state_path: Path, workspace: Path) -> int:
    """Advance the active pointer, refusing to guess at branch points."""
    state = load_state(state_path)
    problems, _ = validate_document(state)
    if problems:
        for problem in problems:
            err(problem)
        return EXIT_FAIL

    if args.node_id:
        node = get_node(state, args.node_id)
        if node.get("status") != "pending":
            raise CliError(
                "node '{}' is {}, not pending. Rewind to it with `jump {} "
                "--reset-descendants` before revisiting it.".format(
                    args.node_id, node.get("status"), args.node_id
                )
            )
        parents, _ = relation_maps(state)
        index = node_index(state)
        unmet = [
            dep
            for dep in parents[args.node_id]
            if index[dep].get("status") not in ("completed", "skipped")
        ]
        if unmet:
            raise CliError(
                "node '{}' is not ready; unmet dependencies: {}. Use "
                "`jump {} --skip-ancestors` to fast-forward past them.".format(
                    args.node_id, ", ".join(unmet), args.node_id
                )
            )
        check_risk(state, args.node_id)
        state["active_node_id"] = args.node_id
        save_state(state_path, state)
        say("active -> {}".format(summary(state, args.node_id)))
        say(header_line(state, workspace))
        return EXIT_OK

    active = state.get("active_node_id")
    index = node_index(state)
    if active and index.get(active, {}).get("status") == "pending":
        raise CliError(
            "'{}' is still in progress. Finish it with `complete` (or move the "
            "pointer with `jump <other>`).".format(active)
        )
    ready = ready_nodes(state)
    if not ready:
        completed, skipped, total = counts(state)
        raise CliError(
            "nothing is ready: {} completed, {} skipped, {} total. "
            "Run `status` to inspect the graph.".format(completed, skipped, total)
        )
    if len(ready) > 1:
        raise CliError(
            "branch point: {} nodes are ready ({}). Pick one with "
            "`next <id>`.".format(len(ready), ", ".join(ready)),
            EXIT_FAIL,
        )
    check_risk(state, ready[0])
    state["active_node_id"] = ready[0]
    save_state(state_path, state)
    say("active -> {}".format(summary(state, ready[0])))
    say(header_line(state, workspace))
    return EXIT_OK


def cmd_verify(args, state_path: Path, workspace: Path) -> int:
    """Run a node's verification gate and report the outcome."""
    state = load_state(state_path)
    problems, _ = validate_document(state)
    if problems:
        for problem in problems:
            err(problem)
        return EXIT_FAIL
    nid = resolve_target(state, args.node_id, workspace)
    node = get_node(state, nid)
    say("verify {} - {}".format(summary(state, nid), node.get("title", "")))
    passed, lines = run_verification(node, workspace, args.yes)
    for line in lines:
        say("  {}".format(line))
    if not passed:
        err("verification failed for {}".format(nid))
        return EXIT_FAIL
    say("verification passed for {}".format(nid))
    return EXIT_OK


def cmd_complete(args, state_path: Path, workspace: Path) -> int:
    """Verify, optionally checkpoint, mark completed, and recompute readiness."""
    state = load_state(state_path)
    problems, _ = validate_document(state)
    if problems:
        for problem in problems:
            err(problem)
        return EXIT_FAIL

    nid = resolve_target(state, args.node_id, workspace)
    node = get_node(state, nid)
    previous_active = state.get("active_node_id")

    if node.get("status") == "completed":
        raise CliError(
            "'{}' is already completed (commit {}). To redo it, jump to its "
            "parent with --reset-descendants.".format(
                nid, short(node.get("commit_sha")) or "none"
            )
        )
    parents, _ = relation_maps(state)
    index = node_index(state)
    unmet = [
        dep
        for dep in parents[nid]
        if index[dep].get("status") not in ("completed", "skipped")
    ]
    if unmet:
        raise CliError(
            "'{}' has unmet dependencies: {}. Complete them or run "
            "`jump {} --skip-ancestors` first.".format(nid, ", ".join(unmet), nid)
        )

    if not args.no_verify:
        passed, lines = run_verification(node, workspace, args.yes)
        for line in lines:
            say("  {}".format(line))
        if not passed:
            err(
                "verification failed for {} - state unchanged. Fix the work, or "
                "pass --no-verify to record it anyway.".format(nid)
            )
            return EXIT_FAIL
    else:
        warn("--no-verify: skipping the verification gate for {}".format(nid))

    commit_sha = node.get("commit_sha")
    if args.commit:
        require_git(workspace)
        if not is_dirty(workspace):
            raise CliError(
                "nothing to commit: the workspace is clean, so '{}' would create "
                "an empty checkpoint. Complete the node without --commit, or make "
                "the file changes first.".format(nid)
            )
        message = args.message or "tutorial({}): {}".format(nid, node.get("title", ""))
        commit_sha = commit_all(workspace, message)
        say("checkpoint {}".format(commit_sha))
        say("  message {}".format(message))

    node["status"] = "completed"
    node["commit_sha"] = commit_sha
    if previous_active == nid or previous_active is None:
        state["active_node_id"] = None
        ready = ready_nodes(state)
        if len(ready) == 1:
            state["active_node_id"] = ready[0]
    save_state(state_path, state)

    say("completed {}".format(summary(state, nid)))
    say(header_line(state, workspace))
    if state.get("active_node_id") is None:
        ready = ready_nodes(state)
        if len(ready) > 1:
            say("branch point, pick one: {}".format(", ".join(ready)))
        elif not ready:
            say("no nodes left in the graph")
    return EXIT_OK


def nearest_ancestor_sha(state: dict, ancestors: list, workspace: Path, target: str) -> str:
    """Nearest completed ancestor checkpoint, else the sha recorded at init."""
    index = node_index(state)
    order, _ = topological_order(state)
    pos = {nid: i for i, nid in enumerate(order)}
    ranked = sorted(
        [nid for nid in ancestors if index[nid].get("commit_sha")],
        key=lambda nid: pos.get(nid, 0),
        reverse=True,
    )
    if ranked:
        chosen = ranked[0]
        say("  checkpoint source: {} ({})".format(chosen, short(index[chosen]["commit_sha"])))
        return resolve_sha(workspace, index[chosen]["commit_sha"])
    initial = (state.get("git") or {}).get("initial_commit_sha") or ""
    if not initial:
        raise CliError(
            "no ancestor checkpoint and no commits existed at init, so a workspace "
            "rollback cannot be resolved. Commit the current state (or jump with "
            "--git-rollback none) and retry."
        )
    say("  checkpoint source: init ({})".format(short(initial)))
    return resolve_sha(workspace, initial)


def node_scopes(node: dict) -> list:
    """Side-effect scopes for a node; an absent ``side_effects`` means repo-only (C-G2)."""
    return list(node.get("side_effects") or ["repo"])


def side_effect_line(node: dict) -> str:
    """One listing line for a rewound node with non-repo side effects (C-G2)."""
    undo = node.get("undo") or []
    tail = "undo: {}".format("; ".join(undo)) if undo else "undo: (none) | irreversible"
    return "side effects: {} [{}] {}".format(
        node["id"], ", ".join(node_scopes(node)), tail
    )


def residue_entry(node: dict, reason: str) -> dict:
    """A root ``residue`` record for one node (C-G2)."""
    return {
        "node_id": node["id"],
        "scopes": node_scopes(node),
        "undo": list(node.get("undo") or []),
        "reason": reason,
        "recorded_at": now_iso(),
    }


def record_residue(state: dict, entries: list) -> None:
    """Store entries, replacing any earlier residue for the same nodes."""
    replaced = {entry["node_id"] for entry in entries}
    kept = [e for e in state.get("residue") or [] if e.get("node_id") not in replaced]
    state["residue"] = kept + entries


def run_undo(node: dict, workspace: Path) -> tuple:
    """Run a node's undo commands in order. Returns (ok, message)."""
    for command in node.get("undo") or []:
        say("  undo {}: {}".format(node["id"], command))
        try:
            proc = subprocess.run(
                command,
                shell=True,
                cwd=str(workspace),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                universal_newlines=True,
                timeout=COMMAND_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            return False, "timed out after {}s: {}".format(COMMAND_TIMEOUT, command)
        if proc.returncode != 0:
            return False, "exit {}: {}\n{}".format(
                proc.returncode, command, tail(proc.stdout or "")
            )
    return True, ""


def cmd_jump(args, state_path: Path, workspace: Path) -> int:
    """Move the active pointer, optionally fast-forwarding and reverting work."""
    if args.undo and not args.reset_descendants:
        raise CliError(
            "--undo runs the undo commands of rewound nodes, so it must be paired "
            "with --reset-descendants",
            EXIT_USAGE,
        )
    state = load_state(state_path)
    problems, _ = validate_document(state)
    if problems:
        for problem in problems:
            err(problem)
        return EXIT_FAIL

    target = args.target_id
    get_node(state, target)
    parents, children = relation_maps(state)
    index = node_index(state)
    ancestors = transitive(target, parents)
    descendants = transitive(target, children)

    unmet = [nid for nid in ancestors if index[nid].get("status") == "pending"]
    if unmet:
        order, _ = topological_order(state)
        pos = {nid: i for i, nid in enumerate(order)}
        unmet.sort(key=lambda nid: pos.get(nid, 0))
        say("unmet ancestors of {}: {}".format(target, ", ".join(unmet)))
        for nid in unmet:
            node = index[nid]
            say(
                "  {} {} - {}".format(
                    summary(state, nid),
                    node.get("status"),
                    node.get("title", ""),
                )
            )
        if not args.skip_ancestors:
            raise CliError(
                "refusing to jump over unmet ancestors. Either fast-forward through "
                "them one node at a time, or pass --skip-ancestors to mark them "
                "skipped ({} ).".format(", ".join(unmet))
            )
        for nid in unmet:
            index[nid]["status"] = "skipped"
        say("marked skipped: {}".format(", ".join(unmet)))

    done_downstream = [
        nid
        for nid in descendants
        if index[nid].get("status") == "completed"
        and index[nid].get("action_type") in CHECKPOINTED_ACTIONS
    ]
    if done_downstream:
        say(
            "completed work downstream of {} ({}): {}".format(
                target, "mutate/execute", ", ".join(done_downstream)
            )
        )
        if args.git_rollback == "none" and not args.reset_descendants:
            warn(
                "their file changes stay in the workspace; add --reset-descendants "
                "to reopen those nodes, or --git-rollback branch|hard to revert them"
            )

    if args.git_rollback != "none":
        if args.git_rollback in ("branch", "hard") and not args.reset_descendants:
            raise CliError(
                "--git-rollback {} reverts workspace work, so it must be paired with "
                "--reset-descendants (otherwise the DAG would still claim the "
                "reverted nodes are complete).".format(args.git_rollback)
            )
        if args.git_rollback == "hard" and is_dirty(workspace) and not args.force:
            raise CliError(
                "workspace has uncommitted changes; commit, stash, or pass --force "
                "to let `git reset --hard` discard them"
            )

    sha = ""
    if args.git_rollback != "none":
        require_git(workspace)
        sha = nearest_ancestor_sha(state, ancestors, workspace, target)

    if args.git_rollback == "branch":
        branch = args.new_branch or "tutorial/rewind-{}".format(target)
        suffix = 2
        while branch_exists(workspace, branch):
            branch = "tutorial/rewind-{}-{}".format(target, suffix)
            suffix += 1
        checkout_branch(workspace, branch, sha)
        state.setdefault("git", {})["branch"] = branch
        say("checked out new branch {} at {}".format(branch, sha))
    elif args.git_rollback == "hard":
        reset_hard(workspace, sha)
        say("reset --hard to {}".format(sha))

    reset_from = list(descendants)
    if args.git_rollback != "none" and args.reset_descendants:
        # The target's own artifacts were just reverted, so it is redoable too.
        reset_from = [target] + reset_from
    rewound = []
    if args.reset_descendants:
        order, _ = topological_order(state)
        pos = {nid: i for i, nid in enumerate(order)}
        reset_from = sorted(set(reset_from), key=lambda nid: pos.get(nid, 0))
        rewound = [nid for nid in reset_from if index[nid].get("status") == "completed"]
        for nid in reset_from:
            index[nid]["status"] = "pending"
            index[nid]["commit_sha"] = None
        say(
            "reset to pending: {}".format(", ".join(reset_from) if reset_from else "(none)")
        )
        for nid in [target] + list(descendants):
            record = (state.get("services") or {}).get(nid)
            if record:
                was_running = stop_record(record, node_stop_signal(state, nid))
                say(
                    "stopped service {} (pid {}{})".format(
                        nid, record["pid"], "" if was_running else ", was not running"
                    )
                )
                drop_service(state, nid)

    # Rewound nodes with non-repo side effects, newest first (C-G2).
    listed = [nid for nid in reversed(rewound) if set(node_scopes(index[nid])) - {"repo"}]
    for nid in listed:
        say(side_effect_line(index[nid]))

    undo_failed = False
    if args.undo:
        for nid in listed:
            node = index[nid]
            scopes = node_scopes(node)
            if undo_failed:
                record_residue(state, [residue_entry(node, "not-run")])
            elif "remote" in scopes:
                record_residue(state, [residue_entry(node, "remote")])
            elif risk_blocked(state, node):
                say("skipped undo of {} (hands-off: high risk, not approved)".format(nid))
                record_residue(state, [residue_entry(node, "not-run")])
            elif not node.get("undo"):
                record_residue(state, [residue_entry(node, "irreversible")])
            else:
                ok, message = run_undo(node, workspace)
                if ok:
                    say("undone: {}".format(nid))
                else:
                    undo_failed = True
                    err("undo failed for {}: {}".format(nid, message))
                    record_residue(state, [residue_entry(node, "failed")])
    elif listed:
        record_residue(state, [residue_entry(index[nid], "not-run") for nid in listed])

    state["active_node_id"] = target
    save_state(state_path, state)
    say("active -> {}".format(summary(state, target)))
    if index[target].get("status") in ("completed", "skipped"):
        say(
            "  ({} is {}; jump to its parent, or re-jump with --reset-descendants, "
            "to redo its work)".format(target, index[target]["status"])
        )
    say(header_line(state, workspace))
    return EXIT_FAIL if undo_failed else EXIT_OK


def require_posix() -> None:
    """Services need process groups and signals; refuse cleanly elsewhere (C-G3)."""
    if os.name != "posix":
        raise CliError("services require a POSIX system", EXIT_USAGE)


def pid_alive(pid: int) -> bool:
    """True when ``pid`` exists. Always False off POSIX, where services never run."""
    if os.name != "posix":
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def signal_group(pgid: int, name: str) -> None:
    """Send a named signal to a process group; a group that is already gone is fine."""
    try:
        os.killpg(pgid, getattr(signal, SERVICE_SIGNALS[name]))
    except ProcessLookupError:
        pass


def node_stop_signal(state: dict, nid: str) -> str:
    """The node's declared ``stop_signal``, defaulting to TERM."""
    node = node_index(state).get(nid) or {}
    return (node.get("service") or {}).get("stop_signal", "TERM")


def stop_record(record: dict, stop_signal: str = "TERM", proc=None) -> bool:
    """Stop one recorded service group: signal, wait up to the grace period, then KILL.

    ``proc`` is the live Popen handle when this process started the service, so the
    exit can be reaped; otherwise liveness is probed by pid. Returns True if it ran.
    """
    def alive() -> bool:
        return proc.poll() is None if proc is not None else pid_alive(record["pid"])

    if not alive():
        return False
    signal_group(record["pgid"], stop_signal)
    deadline = time.monotonic() + SERVICE_STOP_GRACE
    while alive() and time.monotonic() < deadline:
        time.sleep(0.1)
    if alive():
        signal_group(record["pgid"], "KILL")
        if proc is not None:
            proc.wait()
    return True


def drop_service(state: dict, nid: str) -> None:
    """Remove a service record and the ``services`` key when it is empty."""
    services = state.get("services") or {}
    services.pop(nid, None)
    if services:
        state["services"] = services
    else:
        state.pop("services", None)


def cmd_service_start(args, state_path: Path, workspace: Path) -> int:
    """Start a node's service detached and wait for its ``ready`` check (C-G3)."""
    require_posix()
    state = load_state(state_path)
    problems, _ = validate_document(state)
    if problems:
        for problem in problems:
            err(problem)
        return EXIT_FAIL

    nid = args.node_id or require_active_node(state)
    node = get_node(state, nid)
    service = node.get("service")
    if not service:
        raise CliError("node '{}' has no service block".format(nid), EXIT_USAGE)
    check_risk(state, nid)

    services = state.setdefault("services", {})
    previous = services.get(nid)
    if previous and pid_alive(previous["pid"]):
        raise CliError(
            "service for {} is already running (pid {}); run `service stop {}` "
            "first".format(nid, previous["pid"], nid)
        )
    drop_service(state, nid)
    ensure_excluded(workspace, LOG_EXCLUDE_LINE)

    log_rel = "{}/{}.log".format(LOG_DIR_NAME, nid)
    log_path = workspace / log_rel
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("w", encoding="utf-8") as log:
        proc = subprocess.Popen(
            service["command"],
            shell=True,
            cwd=str(workspace),
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    record = {
        "pid": proc.pid,
        "pgid": proc.pid,
        "command": service["command"],
        "log": log_rel,
        "started_at": now_iso(),
    }
    state.setdefault("services", {})[nid] = record
    save_state(state_path, state)
    say("starting {} (pid {}), waiting for ready".format(nid, proc.pid))

    ready = service["ready"]
    timeout = service.get("ready_timeout", 30)
    deadline = time.monotonic() + timeout
    while True:
        if proc.poll() is not None:
            reason = "exited with code {}".format(proc.returncode)
            break
        passed, _ = run_verification({"id": nid, "verification": ready}, workspace, False)
        if passed:
            save_state(state_path, state)
            say("service {} ready (pid {}, log {})".format(nid, proc.pid, log_rel))
            say(header_line(state, workspace))
            return EXIT_OK
        if time.monotonic() >= deadline:
            reason = "ready check did not pass within {}s".format(timeout)
            break
        time.sleep(SERVICE_POLL)

    stop_record(record, service.get("stop_signal", "TERM"), proc)
    drop_service(state, nid)
    save_state(state_path, state)
    err("service {} failed: {}".format(nid, reason))
    log_text = log_path.read_text(encoding="utf-8", errors="replace")
    if log_text.strip():
        err("--- log tail ({}) ---".format(log_rel))
        err(tail(log_text))
    return EXIT_FAIL


def cmd_service_stop(args, state_path: Path, workspace: Path) -> int:
    """Stop a recorded service; a process that already exited is not an error (C-G3)."""
    require_posix()
    state = load_state(state_path)
    problems, _ = validate_document(state)
    if problems:
        for problem in problems:
            err(problem)
        return EXIT_FAIL
    nid = args.node_id or require_active_node(state)
    record = (state.get("services") or {}).get(nid)
    if not record:
        say("no service recorded for {}".format(nid))
        return EXIT_OK
    was_running = stop_record(record, node_stop_signal(state, nid))
    drop_service(state, nid)
    save_state(state_path, state)
    if was_running:
        say("stopped {} (pid {})".format(nid, record["pid"]))
    else:
        say("{} was not running (pid {}); record removed".format(nid, record["pid"]))
    say(header_line(state, workspace))
    return EXIT_OK


def cmd_teardown(args, state_path: Path, workspace: Path) -> int:
    """Stop every recorded service (C-G3)."""
    require_posix()
    state = load_state(state_path)
    problems, _ = validate_document(state)
    if problems:
        for problem in problems:
            err(problem)
        return EXIT_FAIL
    services = dict(state.get("services") or {})
    if not services:
        say("no services recorded")
        return EXIT_OK
    for nid in sorted(services):
        record = services[nid]
        was_running = stop_record(record, node_stop_signal(state, nid))
        say(
            "stopped {} (pid {})".format(nid, record["pid"])
            if was_running
            else "{} was not running (pid {})".format(nid, record["pid"])
        )
        drop_service(state, nid)
    save_state(state_path, state)
    say(header_line(state, workspace))
    return EXIT_OK


def cmd_residue_clear(args, state_path: Path, workspace: Path) -> int:
    """Remove residue entries once the user has handled them (C-G2)."""
    state = load_state(state_path)
    if bool(args.node_id) == bool(args.all):
        raise CliError(
            "residue clear takes exactly one of <node_id> or --all", EXIT_USAGE
        )
    entries = state.get("residue") or []
    if args.all:
        removed = entries
        kept = []
    else:
        removed = [e for e in entries if e.get("node_id") == args.node_id]
        kept = [e for e in entries if e.get("node_id") != args.node_id]
    if not removed:
        say("no residue recorded for {}".format(args.node_id or "any node"))
        return EXIT_OK
    state["residue"] = kept
    save_state(state_path, state)
    say("cleared residue: {}".format(", ".join(e["node_id"] for e in removed)))
    say(header_line(state, workspace))
    return EXIT_OK


# --------------------------------------------------------------------------
# argument parsing


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI parser."""
    parser = argparse.ArgumentParser(
        prog="dag_manager.py",
        description=(
            "Track and navigate an interactive tutorial DAG: state, topological "
            "ordering, verification gates, and Git checkpoints."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "state file: --file (default ./"
            + STATE_FILENAME
            + ")\n"
            "workspace:  --cwd (default: current directory); verification paths and\n"
            "            commands resolve against it\n"
        ),
    )
    parser.add_argument(
        "--file",
        "-f",
        default=STATE_FILENAME,
        metavar="PATH",
        help="state document path (default: ./{})".format(STATE_FILENAME),
    )
    parser.add_argument(
        "--cwd",
        dest="cwd",
        default=None,
        metavar="DIR",
        help="workspace root for paths and commands (default: current directory)",
    )
    parser.add_argument("--version", action="version", version="dag_manager " + SCHEMA_VERSION)

    sub = parser.add_subparsers(dest="command", metavar="COMMAND")
    sub.required = True

    init = sub.add_parser("init", help="create a state document and hide it from git")
    init.add_argument("--title", required=True)
    init.add_argument("--source", required=True, help="tutorial file or URL being run")
    init.add_argument("--mode", choices=MODES, default="assisted")
    init.add_argument("--persistence", choices=PERSISTENCE_MODES, default="file")
    init.add_argument(
        "--git-branch",
        default=None,
        help="create/checkout this branch first (suggest tutorial/<slug>)",
    )
    init.add_argument("--force", action="store_true", help="overwrite an existing state file")
    init.set_defaults(func=cmd_init)

    validate = sub.add_parser("validate", help="check structure, ids, and acyclicity")
    validate.set_defaults(func=cmd_validate)

    status = sub.add_parser("status", help="turn header plus topological node table")
    status.add_argument(
        "--json", action="store_true", help="print the state document (in-context round trip)"
    )
    status.set_defaults(func=cmd_status)

    nxt = sub.add_parser("next", help="advance the active pointer")
    nxt.add_argument("node_id", nargs="?", default=None, help="ready node to activate")
    nxt.set_defaults(func=cmd_next)

    verify = sub.add_parser("verify", help="run a node's verification gate")
    verify.add_argument("node_id", nargs="?", default=None)
    verify.add_argument(
        "--yes",
        action="store_true",
        help="human confirmed this node (required for type=confirm gates)",
    )
    verify.set_defaults(func=cmd_verify)

    complete = sub.add_parser("complete", help="verify, checkpoint, and close a node")
    complete.add_argument("node_id", nargs="?", default=None)
    complete.add_argument(
        "--commit",
        action="store_true",
        help="git add -A + commit 'tutorial(<id>): <title>' and record the sha",
    )
    complete.add_argument("--message", default=None, help="override the commit message")
    complete.add_argument("--no-verify", action="store_true", help="skip the verification gate")
    complete.add_argument("--yes", action="store_true", help="pass-through for confirm gates")
    complete.set_defaults(func=cmd_complete)

    jump = sub.add_parser("jump", help="move the pointer anywhere in the graph")
    jump.add_argument("target_id", help="node to activate")
    jump.add_argument(
        "--skip-ancestors",
        action="store_true",
        help="mark unmet ancestors skipped instead of refusing",
    )
    jump.add_argument(
        "--reset-descendants",
        action="store_true",
        help="return downstream nodes to pending and clear their shas",
    )
    jump.add_argument(
        "--git-rollback",
        choices=GIT_ROLLBACK_MODES,
        default="none",
        help="none: pointer move only | branch: new branch at ancestor sha | hard: reset --hard",
    )
    jump.add_argument(
        "--new-branch",
        default=None,
        metavar="NAME",
        help="branch name for --git-rollback branch (default tutorial/rewind-<target>)",
    )
    jump.add_argument(
        "--force",
        action="store_true",
        help="allow --git-rollback hard to discard uncommitted changes",
    )
    jump.add_argument(
        "--undo",
        action="store_true",
        help="run undo commands of rewound user/system nodes (needs --reset-descendants)",
    )
    jump.set_defaults(func=cmd_jump)

    service = sub.add_parser("service", help="start, stop, or check background services")
    service_sub = service.add_subparsers(dest="service_command", metavar="ACTION")
    service_sub.required = True
    svc_start = service_sub.add_parser("start", help="start a node's service and wait for ready")
    svc_start.add_argument("node_id", nargs="?", default=None, help="node (default: active)")
    svc_start.set_defaults(func=cmd_service_start)
    svc_stop = service_sub.add_parser("stop", help="stop a node's recorded service")
    svc_stop.add_argument("node_id", nargs="?", default=None, help="node (default: active)")
    svc_stop.set_defaults(func=cmd_service_stop)

    teardown = sub.add_parser("teardown", help="stop every recorded service")
    teardown.set_defaults(func=cmd_teardown)

    approve = sub.add_parser("approve", help="record a person's approval of a high-risk node")
    approve.add_argument("node_id", help="node whose current risk tier to approve")
    approve.set_defaults(func=cmd_approve)

    residue = sub.add_parser("residue", help="manage residue left by jump")
    residue_sub = residue.add_subparsers(dest="residue_command", metavar="ACTION")
    residue_sub.required = True
    residue_clear = residue_sub.add_parser("clear", help="remove residue entries")
    residue_clear.add_argument("node_id", nargs="?", default=None, help="node to clear")
    residue_clear.add_argument("--all", action="store_true", help="clear every entry")
    residue_clear.set_defaults(func=cmd_residue_clear)

    return parser


def main(argv=None) -> int:
    """Entry point: parse, dispatch, and translate errors into exit codes."""
    parser = build_parser()
    args = parser.parse_args(argv)
    workspace = resolve_workspace(args.cwd)
    state_path = Path(args.file)
    if not state_path.is_absolute():
        state_path = workspace / state_path
    try:
        return int(args.func(args, state_path, workspace))
    except CliError as exc:
        err(exc.message)
        return exc.code
    except KeyboardInterrupt:
        err("interrupted")
        return 130


if __name__ == "__main__":
    sys.exit(main())
