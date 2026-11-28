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
import copy
import json
import os
import platform
import re
import shlex
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
SCHEMA_VERSION = "1.2"

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
EXPORT_DIR_NAME = ".tutorial-export"
IMAGE_EXTENSIONS = (".png", ".svg", ".jpg", ".jpeg")
LOG_EXCLUDE_LINE = LOG_DIR_NAME + "/"
SERVICE_STOP_GRACE = 5
SERVICE_POLL = 0.5
SERVICE_SIGNALS = {"TERM": "SIGTERM", "INT": "SIGINT", "KILL": "SIGKILL"}
SERVICE_FIELDS = ("command", "ready", "ready_timeout", "stop_signal")
INTERACTIVE_FIELDS = ("answers", "human")
PREFLIGHT_OS = ("linux", "darwin", "windows")
VARIANT_FIELDS = ("commands", "verification", "undo")
REQUIRES_FIELDS = ("tools", "os")
TOOL_SPEC_FIELDS = ("min", "version_command")
VERSION_SPEC_RE = re.compile(r"^\d+(?:\.\d+){0,2}$")
VERSION_FIND_RE = re.compile(r"\d+(?:\.\d+)+")
DOCTOR_TIMEOUT = 10
INTERACTIVE_PATTERNS = (
    re.compile(r"^\s*(npm|yarn|pnpm) init\b(?!.*(-y|--yes))"),
    re.compile(r"\bnpx create-\S+(?!.*(--yes|-y))"),
    re.compile(r"^\s*(vi|vim|nano|emacs)\b"),
    re.compile(r"^\s*(python3?|node|irb|psql|sqlite3|mysql)\s*$"),
    re.compile(r"\bgit (rebase|add) -i\b"),
    re.compile(r"\bssh-keygen\b(?!.*-N)"),
    re.compile(r"^\s*passwd\b"),
    re.compile(r"\bread\s+(-r\s+)?\w+"),
)
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
    "command": ("type", "command", "pattern", "expect_exit", "stdin", "timeout"),
    "file_exists": ("type", "path"),
    "file_contains": ("type", "path", "pattern", "regex"),
    "confirm": ("type", "prompt"),
    "http": ("type", "url", "expect_status", "pattern", "timeout"),
    "port_listening": ("type", "port", "host", "timeout"),
}


DEVIATION_TARGET_RE = re.compile(r"^(check|command:\d+)$")


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


def load_validated_state(
    path: Path, *, emit_warnings: bool = False, summary_err: str = ""
) -> dict:
    """Load state and validate it, raising CliError on validation failure."""
    state = load_state(path)
    problems, warnings = validate_document(state)
    if problems:
        if summary_err:
            for problem in problems:
                err(problem)
            if emit_warnings:
                for message in warnings:
                    warn(message)
            raise CliError(summary_err.format(len(problems)))
        if emit_warnings:
            for message in warnings:
                warn(message)
        for problem in problems[:-1]:
            err(problem)
        raise CliError(problems[-1])
    if emit_warnings:
        for message in warnings:
            warn(message)
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
    if "requires" in state:
        problems.extend(validate_requires(state.get("requires")))
    if "preflight" in state:
        problems.extend(validate_preflight(state.get("preflight")))

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
        check = effective_verification(node)
        if (
            check.get("type") == "confirm"
            and str(check.get("prompt", "")).startswith("DRAFT:")
        ):
            warnings.append("node '{}' still has a draft check".format(nid))
        if (
            node.get("action_type") == "mutate"
            and not node.get("artifacts")
            and not node.get("side_effects")
        ):
            warnings.append(
                "node '{}' is a mutate node but lists no artifacts".format(nid)
            )
        block = node.get("interactive")
        if isinstance(block, dict) and block.get("human"):
            if effective_verification(node).get("type") != "confirm":
                warnings.append(
                    "node '{}' is human: true but its verification is not confirm".format(nid)
                )
        if "interactive" not in node:
            for command in effective_commands(node):
                if any(pattern.search(command) for pattern in INTERACTIVE_PATTERNS):
                    warnings.append(
                        "node '{}' command looks interactive: {}".format(nid, command)
                    )
                    break
        scopes = [s for s in node.get("side_effects", []) if s != "repo"]
        if isinstance(node.get("side_effects"), list) and scopes and not effective_undo(node):
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


def validate_source_ref(ref, label: str) -> list:
    """``source_ref`` is optional; when present it is ``{"line": int>0, "heading": str}``."""
    if not isinstance(ref, dict):
        return ["{} field 'source_ref' must be an object".format(label)]
    problems = []
    if set(ref) != {"line", "heading"}:
        problems.append(
            "{} field 'source_ref' must have exactly the keys 'line' and 'heading'".format(label)
        )
    line = ref.get("line")
    if not isinstance(line, int) or isinstance(line, bool) or line < 1:
        problems.append("{} field 'source_ref.line' must be a positive integer".format(label))
    if not isinstance(ref.get("heading"), str):
        problems.append("{} field 'source_ref.heading' must be a string".format(label))
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
        if field in node and not is_nonempty_str_list(node.get(field)):
            problems.append(
                "{} field {!r} must be an array of non-empty strings".format(label, field)
            )

    if "verification" in node:
        problems.extend(validate_verification(node["verification"], label))

    if "source_ref" in node:
        problems.extend(validate_source_ref(node["source_ref"], label))

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
    if "undo" in node and not is_nonempty_str_list(node.get("undo"), require_nonblank=True):
        problems.append(
            "{} field 'undo' must be an array of non-empty command strings".format(label)
        )
    if "variants" in node:
        problems.extend(validate_variants(node["variants"], label))
    if "service" in node:
        problems.extend(validate_service(node["service"], label))
    if "interactive" in node:
        problems.extend(validate_interactive(node["interactive"], label))
    if "risk" in node and node.get("risk") not in RISK_TIERS:
        problems.append(
            "{} field 'risk' must be one of {}, got {!r}".format(
                label, list(RISK_TIERS), node.get("risk")
            )
        )
    if "deviations" in node:
        problems.extend(validate_deviations(node["deviations"], label))
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
        elif "deviations" in value and (
            not isinstance(value["deviations"], int)
            or isinstance(value["deviations"], bool)
            or value["deviations"] < 0
        ):
            problems.append(
                "{} field 'approved.deviations' must be a non-negative integer".format(label)
            )
    return problems


def is_nonempty_str_list(value, require_nonblank: bool = False) -> bool:
    """True when ``value`` is a list of non-empty (and optionally non-blank) strings."""
    if not isinstance(value, list):
        return False
    return all(
        isinstance(item, str) and (bool(item.strip()) if require_nonblank else bool(item))
        for item in value
    )


def current_os() -> str:
    """Normalized operating system name matching ``PREFLIGHT_OS`` (C-G5)."""
    return platform.system().lower()


def active_variant(node: dict) -> dict:
    """Return the matching OS variant dict for ``node``, or ``{}`` if absent (C-G5)."""
    variants = node.get("variants")
    if not isinstance(variants, dict):
        return {}
    variant = variants.get(current_os())
    return variant if isinstance(variant, dict) else {}


def _effective_field(node: dict, field: str, expected_type: type, default):
    """Resolve one field from the active OS variant with fallback to the base node."""
    variant = active_variant(node)
    if field in variant and isinstance(variant[field], expected_type):
        return variant[field]
    base = node.get(field)
    return base if isinstance(base, expected_type) else default


def effective_commands(node: dict) -> list:
    """Return OS-resolved ``commands`` for ``node`` with field-by-field fallback (C-G5)."""
    return _effective_field(node, "commands", list, [])


def effective_verification(node: dict) -> dict:
    """Return OS-resolved ``verification`` for ``node`` with field-by-field fallback (C-G5)."""
    return _effective_field(node, "verification", dict, {})


def effective_undo(node: dict) -> list:
    """Return OS-resolved ``undo`` for ``node`` with field-by-field fallback (C-G5)."""
    return _effective_field(node, "undo", list, [])


def inspected_commands(node: dict) -> list:
    """Commands the risk classifier reads: commands, undo, and a command check (C-G6)."""
    commands = list(effective_commands(node)) + list(effective_undo(node))
    verification = effective_verification(node)
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


def deviations_covered(node: dict) -> bool:
    """True when the node's approval covers every recorded deviation (C-G8)."""
    approved = node.get("approved")
    covered = approved.get("deviations", 0) if isinstance(approved, dict) else 0
    return covered >= len(node.get("deviations") or [])


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


def validate_deviations(value, label) -> list:
    """Validate a node's ``deviations`` records (C-G8)."""
    if not isinstance(value, list):
        return ["{} field 'deviations' must be an array".format(label)]
    problems = []
    for position, entry in enumerate(value):
        item = "{} deviations[{}]".format(label, position)
        if not isinstance(entry, dict):
            problems.append("{} must be an object".format(item))
            continue
        target = entry.get("target")
        if not isinstance(target, str) or not DEVIATION_TARGET_RE.fullmatch(target):
            problems.append(
                "{} field 'target' must match {}".format(item, DEVIATION_TARGET_RE.pattern)
            )
        for field in ("original", "replacement", "recorded_at"):
            if not isinstance(entry.get(field), str):
                problems.append("{} field {!r} must be a string".format(item, field))
        reason = entry.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            problems.append("{} field 'reason' must be a non-empty string".format(item))
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


def validate_interactive(block, label: str) -> list:
    """Validate a node's ``interactive`` block (C-G11)."""
    if not isinstance(block, dict):
        return ["{} field 'interactive' must be an object".format(label)]
    problems = []
    for field in block:
        if field not in INTERACTIVE_FIELDS:
            problems.append("{} interactive does not accept {!r}".format(label, field))
    if "answers" in block:
        answers = block["answers"]
        if not isinstance(answers, list) or any(not isinstance(a, str) for a in answers):
            problems.append("{} interactive.answers must be an array of strings".format(label))
    if "human" in block and not isinstance(block["human"], bool):
        problems.append("{} interactive.human must be a boolean".format(label))
    return problems


def interactive_hints(node: dict) -> list:
    """Lines telling the agent how to run an interactive node (C-G11)."""
    block = node.get("interactive")
    if not isinstance(block, dict):
        return []
    lines = []
    answers = block.get("answers") or []
    if answers:
        command = (effective_commands(node) or ["<command>"])[0]
        lines.append(
            "feed answers: printf '%s\\n' {} | {}".format(
                " ".join(shlex.quote(answer) for answer in answers), command
            )
        )
    if block.get("human"):
        lines.append("a person must run this step (interactive)")
    return lines


def validate_requires(requires) -> list:
    """Validate the root ``requires`` block: tools and OS preconditions (C-G4)."""
    if not isinstance(requires, dict):
        return ["root field 'requires' must be an object"]
    problems = []
    for field in requires:
        if field not in REQUIRES_FIELDS:
            problems.append("requires does not accept {!r}".format(field))
    tools = requires.get("tools", {})
    if not isinstance(tools, dict):
        problems.append("requires.tools must be an object of name -> version spec")
        tools = {}
    for name, spec in tools.items():
        if not re.match(r"^[A-Za-z0-9._+-]+$", name):
            problems.append("requires.tools name {!r} is not a plain command name".format(name))
        problems.extend(validate_tool_spec(spec, name))
    if "os" in requires:
        value = requires["os"]
        if (
            not isinstance(value, list)
            or not value
            or len(set(value)) != len(value)
            or any(item not in PREFLIGHT_OS for item in value)
        ):
            problems.append("requires.os must be a unique array of {}".format(list(PREFLIGHT_OS)))
    return problems


def validate_variants(variants, label: str) -> list:
    """Validate a node's per-OS ``variants`` block (C-G5)."""
    if not isinstance(variants, dict) or not variants:
        return ["{} field 'variants' must be a non-empty object".format(label)]
    problems = []
    for os_key, variant in variants.items():
        if os_key not in PREFLIGHT_OS:
            problems.append(
                "{} variants key {!r} must be one of {}".format(
                    label, os_key, list(PREFLIGHT_OS)
                )
            )
            continue
        vlabel = "{} variants.{}".format(label, os_key)
        if not isinstance(variant, dict) or not variant:
            problems.append("{} must be a non-empty object".format(vlabel))
            continue
        for field in variant:
            if field not in VARIANT_FIELDS:
                problems.append("{} does not accept {!r}".format(vlabel, field))
        if "commands" in variant and not is_nonempty_str_list(variant["commands"]):
            problems.append(
                "{} field 'commands' must be an array of non-empty strings".format(vlabel)
            )
        if "undo" in variant and not is_nonempty_str_list(
            variant["undo"], require_nonblank=True
        ):
            problems.append(
                "{} field 'undo' must be an array of non-empty command strings".format(vlabel)
            )
        if "verification" in variant:
            problems.extend(validate_verification(variant["verification"], vlabel))
    return problems


def validate_tool_spec(spec, name: str) -> list:
    """A spec is ``"*"``, ``">=X[.Y[.Z]]"``, or ``{"min": "X.Y", "version_command": str}``."""
    label = "requires.tools[{!r}]".format(name)
    if isinstance(spec, str):
        if spec == "*" or re.match(r"^>=\d+(?:\.\d+){0,2}$", spec):
            return []
        return ["{} must be \"*\", \">=X[.Y[.Z]]\", or an object, got {!r}".format(label, spec)]
    if isinstance(spec, dict):
        problems = []
        for field in spec:
            if field not in TOOL_SPEC_FIELDS:
                problems.append("{} does not accept {!r}".format(label, field))
        if not isinstance(spec.get("min"), str) or not VERSION_SPEC_RE.match(spec.get("min")):
            problems.append("{} needs 'min' as X[.Y[.Z]]".format(label))
        command = spec.get("version_command")
        if "version_command" in spec and (not isinstance(command, str) or not command.strip()):
            problems.append("{} version_command must be a non-empty string".format(label))
        return problems
    return ["{} must be a string or an object".format(label)]


def validate_preflight(record) -> list:
    """The root ``preflight`` record written by ``doctor`` (C-G4)."""
    if not isinstance(record, dict):
        return ["root field 'preflight' must be an object"]
    problems = []
    if not isinstance(record.get("passed"), bool):
        problems.append("preflight.passed must be a boolean")
    if not isinstance(record.get("at"), str):
        problems.append("preflight.at must be a timestamp string")
    missing = record.get("missing")
    if not isinstance(missing, list) or any(not isinstance(item, str) for item in missing):
        problems.append("preflight.missing must be an array of strings")
    return problems


def version_tuple(text: str) -> tuple:
    """'24.0.7' -> (24, 0, 7), padded to three parts for comparison."""
    parts = tuple(int(piece) for piece in text.split("."))
    return parts + (0,) * (3 - len(parts))


def spec_parts(spec) -> tuple:
    """(minimum tuple or None, version command or None) for a validated spec."""
    if spec == "*":
        return None, None
    if isinstance(spec, str):
        return version_tuple(spec[2:]), None
    return version_tuple(spec["min"]), spec.get("version_command")


def probe_tool(name: str, command) -> tuple:
    """Run the version command. Returns (kind, text): kind is missing, found, or unknown."""
    shell_command = command or "{} --version".format(shlex.quote(name))
    try:
        proc = subprocess.run(
            shell_command,
            shell=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            universal_newlines=True,
            timeout=DOCTOR_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return "unknown", "timed out"
    except OSError:
        return "missing", ""
    if proc.returncode == 127:
        return "missing", ""
    match = VERSION_FIND_RE.search(proc.stdout or "")
    if match:
        return "found", match.group(0)
    return "unknown", ""


def check_tool(name: str, spec) -> dict:
    """One ``doctor`` row for a tool (C-G4)."""
    minimum, command = spec_parts(spec)
    need = spec if isinstance(spec, str) else "min {}".format(spec["min"])
    kind, text = probe_tool(name, command)
    row = {"name": name, "found": text or "-", "need": need}
    if kind == "missing":
        row.update(status="missing", ok=False)
    elif minimum is None:
        # "*" accepts any tool that runs, even when its version cannot be read.
        row.update(status="ok" if kind == "found" else "unknown-version", ok=True)
    elif kind != "found":
        row.update(status="unknown-version", ok=False)
    elif version_tuple(text) < minimum:
        row.update(status="too-old", ok=False)
    else:
        row.update(status="ok", ok=True)
    return row


def run_checks(requires: dict) -> tuple:
    """Check every declared tool and the OS. Returns (passed, rows, missing names)."""
    rows = [check_tool(name, spec) for name, spec in (requires.get("tools") or {}).items()]
    missing = [row["name"] for row in rows if not row["ok"]]
    passed = not missing
    if "os" in requires:
        current = current_os()
        ok = current in requires["os"]
        rows.append({
            "name": "os",
            "status": "ok" if ok else "mismatch",
            "found": current,
            "need": ", ".join(requires["os"]),
            "ok": ok,
        })
        passed = passed and ok
    return passed, rows, missing


def format_check(row: dict) -> str:
    return "{:<16} {:<22} {} (need {})".format(row["status"], row["name"], row["found"], row["need"])


def cmd_doctor(args, state_path: Path, workspace: Path) -> int:
    """Check declared tools and OS; record the result as ``preflight`` (C-G4)."""
    state = load_validated_state(state_path)
    if "requires" not in state:
        say("no requires block; nothing to check")
        return EXIT_OK
    passed, rows, missing = run_checks(state["requires"])
    state["preflight"] = {"passed": passed, "at": now_iso(), "missing": missing}
    save_state(state_path, state)
    if args.json:
        say(json.dumps({"passed": passed, "checks": rows}, indent=2))
    else:
        for row in rows:
            say(format_check(row))
        say("preflight passed" if passed else "preflight failed: {}".format(
            ", ".join(missing) or "os mismatch"))
    return EXIT_OK if passed else EXIT_FAIL


def preflight_gate(state: dict, state_path: Path, skip: bool) -> None:
    """Run doctor before the first node when the DAG declares ``requires`` (C-G4)."""
    if skip or "requires" not in state:
        return
    if any(node.get("status") in ("completed", "skipped") for node in state.get("nodes") or []):
        return
    if (state.get("preflight") or {}).get("passed") is True:
        return
    passed, rows, missing = run_checks(state["requires"])
    state["preflight"] = {"passed": passed, "at": now_iso(), "missing": missing}
    save_state(state_path, state)
    if passed:
        return
    failing = ", ".join(missing) or "os mismatch"
    raise CliError(
        "preflight failed ({}): run doctor, or pass --skip-doctor to continue anyway".format(
            failing
        )
    )


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
    if kind == "command" and "stdin" in verification:
        if not isinstance(verification["stdin"], str):
            problems.append("{} verification.stdin must be a string".format(label))
    if kind == "command" and "timeout" in verification:
        if not is_positive_int(verification["timeout"]):
            problems.append("{} verification.timeout must be a positive integer".format(label))
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
    """Current branch name, or ``HEAD`` when detached.

    Falls back to ``symbolic-ref`` so an unborn branch (no commits yet) still
    reports its name.
    """
    code, out, _ = run_git(["rev-parse", "--abbrev-ref", "HEAD"], workspace)
    if code == 0:
        return out
    code, out, _ = run_git(["symbolic-ref", "--short", "HEAD"], workspace)
    return out if code == 0 else ""


def head_sha(workspace: Path) -> str:
    """Full sha of ``HEAD``, or ``\"\"`` when the repository has no commits yet."""
    code, out, _ = run_git(["rev-parse", "--verify", "HEAD"], workspace)
    return out if code == 0 else ""


def is_dirty(workspace: Path) -> bool:
    """True when tracked or untracked (non-excluded) changes exist in the workspace.

    Limited to the workspace path, so changes elsewhere in a larger repository
    never count.
    """
    code, out, _ = run_git(["status", "--porcelain", "--", "."], workspace)
    return bool(out)


def workspace_prefix(workspace: Path) -> str:
    """Path of the workspace inside its repository (``""`` at the top level)."""
    code, out, stderr = run_git(["rev-parse", "--show-prefix"], workspace)
    if code != 0:
        raise CliError("cannot locate the workspace in its repository: {}".format(stderr))
    return out


def resolve_sha(workspace: Path, ref: str) -> str:
    """Resolve ``ref`` to a full sha or fail."""
    code, out, stderr = run_git(["rev-parse", "--verify", "{}^{{commit}}".format(ref)], workspace)
    if code != 0:
        raise CliError("cannot resolve git revision {!r}: {}".format(ref, stderr or "unknown revision"))
    return out


def commit_all(workspace: Path, message: str) -> str:
    """Stage and commit everything under the workspace; return the new ``HEAD`` sha.

    Both steps are limited to the workspace path, so files elsewhere in a larger
    repository are never staged or committed.
    """
    code, _, stderr = run_git(["add", "-A", "--", "."], workspace)
    if code != 0:
        raise CliError("git add -A failed: {}".format(stderr))
    code, _, stderr = run_git(["commit", "--no-verify", "-m", message, "--", "."], workspace)
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


def git_paths(args: list, workspace: Path) -> set:
    """NUL-separated path listing from ``git <command> -z <rest>``, workspace-relative."""
    code, out, stderr = run_git(args[:1] + ["-z"] + args[1:], workspace)
    if code != 0:
        raise CliError("git {} failed: {}".format(" ".join(args), stderr))
    return {path for path in out.split("\0") if path}


def restore_workspace(workspace: Path, sha: str, prefix: str) -> bool:
    """Make the workspace subdirectory match ``sha`` without moving ``HEAD``.

    Used instead of ``git reset --hard`` when the workspace is a subdirectory, so
    commits and uncommitted changes outside it survive. Tracked files are restored
    and files tracked since ``sha`` are removed; untracked files stay, as with
    ``reset --hard``. The result is committed as a rewind commit limited to the
    workspace. Returns True when a commit was made.
    """
    wanted = git_paths(["ls-tree", "-r", "--name-only", sha, "--", "."], workspace)
    if wanted:
        code, _, stderr = run_git(["checkout", sha, "--", "."], workspace)
        if code != 0:
            raise CliError("git checkout {} -- . failed: {}".format(sha[:12], stderr))
    extra = sorted(git_paths(["ls-files", "--", "."], workspace) - wanted)
    if extra:
        code, _, stderr = run_git(
            ["--literal-pathspecs", "rm", "-q", "-f", "--"] + extra, workspace
        )
        if code != 0:
            raise CliError("git rm failed: {}".format(stderr))
    code, _, _ = run_git(["diff", "--cached", "--quiet", "HEAD", "--", "."], workspace)
    if code == 0:
        return False
    message = "tutorial(rewind): restore {} to {}".format(prefix.rstrip("/"), sha[:12])
    code, _, stderr = run_git(["commit", "--no-verify", "-q", "-m", message, "--", "."], workspace)
    if code != 0:
        raise CliError("git commit of the rewind failed: {}".format(stderr))
    return True


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
    verification = effective_verification(node)
    kind = verification.get("type")
    label = "node '{}'".format(node.get("id"))

    if kind == "command":
        command = verification["command"]
        expect = verification.get("expect_exit", 0)
        timeout = verification.get("timeout", COMMAND_TIMEOUT)
        # C-G11: a declared stdin is fed as input; otherwise stdin is /dev/null (EOF).
        stdin_kwargs = (
            {"input": verification["stdin"]} if "stdin" in verification
            else {"stdin": subprocess.DEVNULL}
        )
        try:
            proc = subprocess.run(
                command,
                shell=True,
                cwd=str(workspace),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                universal_newlines=True,
                timeout=timeout,
                **stdin_kwargs
            )
        except subprocess.TimeoutExpired:
            return False, [
                "command timed out after {}s: {}".format(timeout, command)
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


def strip_progress(doc: dict) -> dict:
    """Return a copy of a DAG document with run progress removed.

    Drops the runtime blocks (``services``, ``residue``, ``preflight``,
    ``active_node_id``) and the ``git`` block, and resets every node to
    ``pending`` with a null ``commit_sha``, removing ``approved`` and
    ``deviations``. The input is never modified.
    """
    stripped = copy.deepcopy(doc)
    for key in ("services", "residue", "preflight", "active_node_id", "git"):
        stripped.pop(key, None)
    for node in stripped.get("nodes", []) or []:
        if isinstance(node, dict):
            node["status"] = "pending"
            node["commit_sha"] = None
            node.pop("approved", None)
            node.pop("deviations", None)
    return stripped


def load_init_source(path: Path) -> dict:
    """Read the prebuilt DAG named by ``init --from``; problems exit 1."""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise CliError("cannot read --from file {}: {}".format(path, exc))
    try:
        doc = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CliError(
            "--from file {} is not valid JSON (line {} column {}): {}".format(
                path, exc.lineno, exc.colno, exc.msg
            )
        )
    if not isinstance(doc, dict):
        raise CliError("--from file {} must contain a JSON object".format(path))
    return doc


def cmd_init(args, state_path: Path, workspace: Path) -> int:
    """Create a fresh state document and hide it from Git."""
    if state_path.exists() and not args.force:
        raise CliError(
            "{} already exists. Resume it, or pass --force to start over "
            "(--force discards recorded progress).".format(state_path)
        )
    source_doc = None
    if args.from_path:
        source_path = Path(args.from_path)
        if not source_path.is_absolute():
            source_path = workspace / source_path
        source_doc = load_init_source(source_path)
    elif not args.title or not args.source:
        missing = [flag for flag, value in (("--title", args.title), ("--source", args.source))
                   if not value]
        raise CliError(
            "{} required unless --from is given".format(" and ".join(missing)), EXIT_USAGE
        )

    title = args.title or (source_doc or {}).get("title")
    source = args.source or (source_doc or {}).get("source_doc")
    if source_doc is not None and not (title and source):
        raise CliError(
            "--from file has no {}; pass {} to supply it".format(
                "title" if not title else "source_doc",
                "--title" if not title else "--source",
            )
        )
    if source_doc is None:
        seed = {"nodes": []}
        mode = args.mode or "assisted"
    else:
        seed = strip_progress(source_doc)
        mode = args.mode or source_doc.get("mode")
    state = {
        "schema_version": SCHEMA_VERSION,
        "title": title,
        "source_doc": source,
        "mode": mode,
        "persistence": args.persistence,
        "active_node_id": None,
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "git": {
            "branch": "",
            "initial_commit_sha": "",
            "initial_dirty": False,
            "exclude_configured": True,
        },
        "nodes": seed["nodes"] if source_doc is not None else [],
    }
    if source_doc is not None and "requires" in seed:
        state["requires"] = seed["requires"]
    if source_doc is not None:
        problems, _ = validate_document(state)
        if problems:
            for problem in problems:
                err(problem)
            raise CliError(
                "{} problem(s) in --from file {}; nothing written".format(
                    len(problems), args.from_path
                )
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

    state["git"] = {
        "branch": current_branch(workspace),
        "initial_commit_sha": sha,
        "initial_dirty": is_dirty(workspace),
        "exclude_configured": True,
    }
    save_state(state_path, state)

    say("initialised {}".format(state_path))
    say("  title       {}".format(title))
    say("  source      {}".format(source))
    say("  mode        {}".format(mode))
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
    say("next: decompose {} into nodes, write them into the 'nodes' array,".format(source))
    say("      then run `validate` and `next`.")
    return EXIT_OK


# --------------------------------------------------------------------------
# draft: skeleton nodes from a Markdown tutorial (C-G10)

DRAFT_SHELL_INFO = ("sh", "bash", "shell", "console", "zsh")
DRAFT_WINDOWS_SHELL_INFO = ("powershell", "pwsh", "cmd", "bat")
DRAFT_SUMMARY_MAX = 200
DRAFT_SLUG_MAX = 40
DRAFT_H1_RE = re.compile(r"^#[ \t]+(.+?)\s*$")
DRAFT_SECTION_RE = re.compile(r"^#{2,3}[ \t]+(.+?)\s*$")
DRAFT_ITEM_RE = re.compile(r"^\d+\.\s+(.*?)\s*$")
DRAFT_SUBSECTION_RE = re.compile(r"^#{4,6}[ \t]+(.+?)\s*$")
DRAFT_ENV_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=.*$")
DRAFT_TOOLS = frozenset(
    {
        "python3",
        "git",
        "docker",
        "sqlite3",
        "node",
        "npm",
        "curl",
        "cargo",
        "go",
        "Rscript",
        "make",
    }
)
DRAFT_OS_LABEL_RE = re.compile(
    r"^(?:(?:on|for|in)\s+)?(linux|macos|mac\s+os|darwin|windows)(?:\s*\([^)]*\))?\s*:?$",
    re.IGNORECASE,
)
DRAFT_SERVER_PATTERNS = (
    re.compile(r"\bhttp\.server\b"),
    re.compile(r"\buvicorn\b"),
    re.compile(r"\bflask\s+run\b"),
    re.compile(r"\bnpm\s+run\s+dev\b"),
    re.compile(r"\bvite\b"),
    re.compile(r"\brails\s+server\b"),
)


def slugify(text: str) -> str:
    """Return a lowercase, dash-joined slug of at most 40 characters."""
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:DRAFT_SLUG_MAX].rstrip("-")
    return slug or "step"


def scan_fences(lines):
    """Return (kinds, fences) for a Markdown document.

    ``kinds[i]`` is ``"fence"`` for a fence delimiter line, ``"code"`` for a
    line inside a fence, and ``"text"`` otherwise. Each fence is a dict with
    ``open``, ``close`` (None when it runs to EOF), ``info``, and ``indent``.
    """
    kinds = ["text"] * len(lines)
    fences = []
    current = None
    for i, line in enumerate(lines):
        stripped = line.strip()
        if current is None:
            if stripped[:3] in ("```", "~~~"):
                marker = stripped[:3]
                current = {
                    "open": i,
                    "close": None,
                    "info": stripped[3:].strip(),
                    "indent": len(line) - len(line.lstrip()),
                    "marker": marker,
                }
                kinds[i] = "fence"
        else:
            if stripped.startswith(current["marker"]) and not stripped.strip(current["marker"][0]):
                current["close"] = i
                kinds[i] = "fence"
                fences.append(current)
                current = None
            else:
                kinds[i] = "code"
    if current is not None:
        fences.append(current)
    fences.sort(key=lambda fence: fence["open"])
    return kinds, fences


def draft_sections(lines, kinds):
    """Return (title_hint, [(line_index, heading_text)]) for the document."""
    title_hint = None
    headed = []
    items = []
    for i, line in enumerate(lines):
        if kinds[i] != "text":
            continue
        match = DRAFT_H1_RE.match(line)
        if match and title_hint is None:
            title_hint = match.group(1)
        match = DRAFT_SECTION_RE.match(line)
        if match:
            headed.append((i, match.group(1)))
            continue
        match = DRAFT_ITEM_RE.match(line)
        if match:
            items.append((i, match.group(1)))
    return title_hint, (headed if headed else items)


def normalize_draft_os(token: str):
    """Map an OS label token to a ``PREFLIGHT_OS`` key, or ``None``."""
    key = token.strip().lower().replace(" ", "")
    if key == "linux":
        return "linux"
    if key in ("macos", "darwin"):
        return "darwin"
    if key == "windows":
        return "windows"
    return None


def detect_os_label(line: str):
    """Return ``(os_key, is_heading)`` if ``line`` is an explicit OS label."""
    stripped = line.strip()
    if not stripped:
        return None, False
    sub_match = DRAFT_SUBSECTION_RE.match(stripped)
    is_heading = sub_match is not None
    candidate = sub_match.group(1) if sub_match else stripped
    if not is_heading:
        candidate = re.sub(r"^(?:[-*]\s+)", "", candidate)
    candidate = candidate.replace("`", "").replace("*", "").replace("_", "").strip()
    match = DRAFT_OS_LABEL_RE.match(candidate)
    if match:
        return normalize_draft_os(match.group(1)), is_heading
    return None, is_heading


def detect_fence_os(info: str):
    """Return a ``PREFLIGHT_OS`` key if a fenced-block info string carries an OS label."""
    for token in re.split(r"[\s:,()\[\]]+", info.strip()):
        if not token:
            continue
        os_key = normalize_draft_os(token)
        if os_key is not None:
            return os_key
    return None


def _tokenize_shell_line(line: str) -> list:
    """Split one shell line into tokens while preserving ``; | &`` operators."""
    try:
        lexer = shlex.shlex(line, posix=True, punctuation_chars=";|&")
        lexer.whitespace_split = True
        return list(lexer)
    except ValueError:
        return line.split()


def detect_draft_tools(commands) -> set:
    """Detect known CLI tools in command positions across shell command strings."""
    detected = set()
    for command in commands:
        for raw_line in command.splitlines():
            stripped = raw_line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            at_cmd_pos = True
            for token in _tokenize_shell_line(stripped):
                if token in (";", "&&", "||", "|", "&", "|&"):
                    at_cmd_pos = True
                    continue
                if not at_cmd_pos:
                    continue
                if DRAFT_ENV_ASSIGN_RE.match(token) or token in ("sudo", "env", "nohup", "command"):
                    continue
                cmd_name = token.rsplit("/", 1)[-1] if token.startswith("/") else token
                if cmd_name in DRAFT_TOOLS:
                    detected.add(cmd_name)
                at_cmd_pos = False
    return detected


def detect_draft_server(commands, texts=()) -> bool:
    """True when drafted commands or inline code mention a long-running server."""
    candidates = list(commands)
    for text in texts:
        for match in re.finditer(r"`([^`]+)`", text):
            candidates.append(match.group(1))
    return any(
        pattern.search(candidate)
        for candidate in candidates
        for pattern in DRAFT_SERVER_PATTERNS
    )


def draft_node(lines, kinds, fences, start, end, n, previous, title, line_no):
    """Build one skeleton node from the body lines in ``[start, end)``."""
    summary = None
    commands = []
    variants = {}
    kinds_seen = []
    fence_by_open = {
        fence["open"]: fence for fence in fences if start <= fence["open"] < end
    }
    active_os = None
    active_os_is_heading = False
    inline_texts = [title]

    for i in range(start, end):
        if i in fence_by_open:
            fence = fence_by_open[i]
            info_tokens = fence["info"].split()
            info_word = info_tokens[0].lower() if info_tokens else ""
            fence_os = detect_fence_os(fence["info"])
            target_os = fence_os or active_os
            close = fence["close"] if fence["close"] is not None else len(lines)
            body = []
            for line in lines[fence["open"] + 1 : close]:
                cut = min(fence["indent"], len(line) - len(line.lstrip(" ")))
                body.append(line[cut:])
            is_prompt = any(row.startswith("$ ") for row in body)
            is_shell = (
                info_word in DRAFT_SHELL_INFO
                or (not info_word and is_prompt)
                or (target_os == "windows" and info_word in DRAFT_WINDOWS_SHELL_INFO)
            )
            if is_shell:
                kinds_seen.append("execute")
                cmd_text = "\n".join(
                    row[2:] if row.startswith("$ ") else row for row in body
                )
                if target_os:
                    if cmd_text:
                        variants.setdefault(target_os, []).append(cmd_text)
                else:
                    commands.append(cmd_text)
            else:
                kinds_seen.append("mutate")
            continue

        if kinds[i] != "text":
            continue
        stripped = lines[i].strip()
        if not stripped:
            continue
        inline_texts.append(lines[i])
        if summary is None:
            block = []
            j = i
            while j < end and kinds[j] == "text" and lines[j].strip():
                block.append(lines[j])
                j += 1
            text = " ".join(" ".join(block).split())
            if len(text) > DRAFT_SUMMARY_MAX:
                text = text[: DRAFT_SUMMARY_MAX - 3] + "..."
            summary = text
        os_key, is_heading = detect_os_label(lines[i])
        if os_key is not None:
            active_os = os_key
            active_os_is_heading = is_heading
        elif is_heading or not active_os_is_heading:
            active_os = None
            active_os_is_heading = False

    if "execute" in kinds_seen:
        action = "execute"
    elif kinds_seen:
        action = "mutate"
    else:
        action = "read-only"
    if summary is None:
        summary = title
    node_id = "{}-{}".format(n, slugify(title))
    all_commands = list(commands)
    for os_cmds in variants.values():
        all_commands.extend(os_cmds)
    prompt = "DRAFT: replace with a real check for {}".format(title)
    if detect_draft_server(all_commands, inline_texts):
        prompt += "; DRAFT: consider a service block"
    node = {
        "id": node_id,
        "title": title,
        "summary": summary,
        "dependencies": [previous] if previous else [],
        "action_type": action,
        "commands": commands,
        "artifacts": [],
        "verification": {
            "type": "confirm",
            "prompt": prompt,
        },
        "status": "pending",
        "commit_sha": None,
        "source_ref": {"line": line_no, "heading": title},
    }
    if variants:
        node["variants"] = {
            os_key: {"commands": os_cmds} for os_key, os_cmds in variants.items()
        }
    return node


def build_draft(lines, stem):
    """Return the ``draft`` document ``{"title", "nodes"}`` (and optional ``requires``)."""
    kinds, fences = scan_fences(lines)
    title_hint, sections = draft_sections(lines, kinds)
    nodes = []
    all_commands = []
    previous = None
    for index, (line_index, heading) in enumerate(sections):
        end = sections[index + 1][0] if index + 1 < len(sections) else len(lines)
        node = draft_node(
            lines, kinds, fences, line_index + 1, end, index + 1, previous, heading, line_index + 1
        )
        nodes.append(node)
        all_commands.extend(node.get("commands", []))
        for variant in (node.get("variants") or {}).values():
            all_commands.extend(variant.get("commands", []))
        previous = node["id"]
    document = {"title": title_hint or stem, "nodes": nodes}
    detected_tools = detect_draft_tools(all_commands)
    if detected_tools:
        document["requires"] = {
            "tools": {tool: "*" for tool in sorted(detected_tools)}
        }
    return document


def cmd_draft(args, state_path: Path, workspace: Path) -> int:
    """Print skeleton nodes for a Markdown tutorial as JSON, or fill state with --write (C-G10)."""
    path = Path(args.path)
    if not path.is_absolute():
        path = workspace / path
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise CliError("cannot read {}: {}".format(path, exc.__class__.__name__))
    document = build_draft(text.splitlines(), path.stem)
    if not args.write:
        print(json.dumps(document, indent=2))
        return EXIT_OK
    state = load_state(state_path)
    if state.get("nodes"):
        raise CliError(
            "nodes already present in {}; draft --write only fills an empty state".format(
                state_path
            )
        )
    state["nodes"] = document["nodes"]
    if document.get("requires") and not state.get("requires"):
        state["requires"] = document["requires"]
    problems, warnings = validate_document(state)
    if problems:
        for problem in problems:
            err(problem)
        err("draft not written: {} problem(s)".format(len(problems)))
        return EXIT_FAIL
    save_state(state_path, state)
    for message in warnings:
        warn(message)
    say(header_line(state, workspace))
    return EXIT_OK


SKILL_ROOT = Path(__file__).resolve().parent.parent
SCAFFOLD_TEMPLATE = Path("templates") / "SKILL.md.tmpl"
SCAFFOLD_DESCRIPTION = (
    "Run the {title} tutorial step by step with checks and rewinds. "
    "Use when the user wants to work through {title}."
)


def cmd_scaffold(args, state_path: Path, workspace: Path) -> int:
    """Package a validated DAG as its own skill directory (C-G12)."""
    name = args.name
    if not ID_RE.match(name):
        raise CliError(
            "skill name {!r} must match {}".format(name, ID_RE.pattern), EXIT_USAGE
        )
    source_path = Path(args.from_path)
    if not source_path.is_absolute():
        source_path = workspace / source_path
    stripped = strip_progress(load_init_source(source_path))

    placeholder_git = {
        "branch": "",
        "initial_commit_sha": "",
        "initial_dirty": False,
        "exclude_configured": True,
    }
    problems, _ = validate_document(dict(stripped, git=placeholder_git))
    if problems:
        for problem in problems:
            err(problem)
        raise CliError(
            "{} problem(s) in --from file {}; nothing written".format(
                len(problems), args.from_path
            )
        )

    template = SKILL_ROOT / SCAFFOLD_TEMPLATE
    if not template.is_file():
        raise CliError(
            "scaffold needs {} (run it from the source skill)".format(SCAFFOLD_TEMPLATE.as_posix())
        )

    dest = Path(args.dest) if args.dest else workspace / ".agents" / "skills"
    if not dest.is_absolute():
        dest = workspace / dest
    target = dest / name
    if target.is_symlink():
        raise CliError("{} is a symlink; remove it or pass a different name".format(target))
    if target.exists() and not args.force:
        raise CliError(
            "{} already exists; pass --force to replace it".format(target)
        )

    dest.mkdir(parents=True, exist_ok=True)
    build = Path(tempfile.mkdtemp(prefix=".{}.tmp-".format(name), dir=str(dest)))
    try:
        for folder in ("scripts", "references"):
            shutil.copytree(str(SKILL_ROOT / folder), str(build / folder),
                            ignore=shutil.ignore_patterns("__pycache__"))
        (build / "dag.json").write_text(
            json.dumps(stripped, indent=2) + "\n", encoding="utf-8"
        )
        title = stripped["title"]
        text = template.read_text(encoding="utf-8")
        for key, value in (
            ("{{name}}", name),
            ("{{title}}", title),
            ("{{description}}", SCAFFOLD_DESCRIPTION.format(title=title)),
            ("{{node_count}}", str(len(stripped.get("nodes") or []))),
        ):
            text = text.replace(key, value)
        (build / "SKILL.md").write_text(text, encoding="utf-8")
        old = None
        try:
            if target.exists():
                old = Path(tempfile.mkdtemp(prefix=".{}.old-".format(name), dir=str(dest)))
                old.rmdir()
                os.rename(str(target), str(old))
        except OSError as exc:
            raise CliError("could not move aside existing {}: {}".format(target, exc))
        try:
            os.rename(str(build), str(target))
        except OSError as exc:
            if old is None:
                raise CliError("could not place {}: {}".format(target, exc))
            try:
                os.rename(str(old), str(target))
            except OSError as back:
                raise CliError(
                    "could not replace {} ({}); previous copy left at {} ({})".format(
                        target, exc, old, back))
            raise CliError("could not replace {}: {}; previous copy restored".format(target, exc))
        if old is not None:
            shutil.rmtree(str(old), ignore_errors=True)
    except BaseException:
        shutil.rmtree(str(build), ignore_errors=True)
        raise
    say(str(target))
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
    state = load_validated_state(
        state_path,
        emit_warnings=True,
        summary_err="{} problem(s); run `validate` for detail",
    )

    if args.json:
        say(json.dumps(state, indent=2, ensure_ascii=False))
        return EXIT_OK

    say(header_line(state, workspace))
    say("")
    for line in render_graph(state):
        say(line)
    say("")
    for node in state.get("nodes") or []:
        if node.get("deviations"):
            say("deviations  {}: {}".format(node["id"], len(node["deviations"])))
    if any(node.get("deviations") for node in state.get("nodes") or []):
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
        for command in effective_commands(node):
            say("  $ {}".format(command))
        for artifact in node.get("artifacts") or []:
            say("  * {}".format(artifact))
        for hint in interactive_hints(node):
            say("  {}".format(hint))
        verification = effective_verification(node)
        if node.get("deviations"):
            say("  deviations: {}".format(len(node["deviations"])))
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
    state = load_validated_state(state_path)
    node = get_node(state, args.node_id)
    tier, labels = risk_tier(node)
    node["approved"] = {
        "tier": tier,
        "at": now_iso(),
        "deviations": len(node.get("deviations", [])),
    }
    save_state(state_path, state)
    say(
        "approved {} at risk {} ({})".format(
            args.node_id, tier, ", ".join(labels) or "declared"
        )
    )
    say(header_line(state, workspace))
    return EXIT_OK


def cmd_deviate(args, state_path: Path, workspace: Path) -> int:
    """Replace a node's check or one of its commands and record why (C-G8)."""
    state = load_validated_state(state_path)
    nid = resolve_target(state, args.node_id, workspace)
    node = get_node(state, nid)
    if node.get("status") == "completed":
        raise CliError(
            "'{}' is already completed. Jump back with `jump {} --reset-descendants` "
            "before deviating.".format(nid, nid)
        )
    if not args.to.strip():
        raise CliError("--to must be a non-empty command", EXIT_USAGE)
    if not args.reason.strip():
        raise CliError("--reason must be non-empty", EXIT_USAGE)

    if args.check:
        verification = effective_verification(node)
        if verification.get("type") != "command":
            raise CliError(
                "'{}' check is not a command check; --check needs a 'command' "
                "verification".format(nid),
                EXIT_USAGE,
            )
        target = "check"
        original = verification.get("command")
    else:
        commands = effective_commands(node)
        if args.index < 0 or args.index >= len(commands):
            raise CliError(
                "'{}' has {} command(s); --command {} is out of range".format(
                    nid, len(commands), args.index
                ),
                EXIT_USAGE,
            )
        target = "command:{}".format(args.index)
        original = commands[args.index]
    if original == args.to:
        raise CliError("--to is the same as the current text of {}".format(target), EXIT_USAGE)

    variant = active_variant(node)
    if args.check:
        target_verification = (
            variant["verification"] if "verification" in variant else node["verification"]
        )
        target_verification["command"] = args.to
    else:
        target_commands = (
            variant["commands"] if "commands" in variant else node["commands"]
        )
        target_commands[args.index] = args.to
    node.setdefault("deviations", []).append(
        {
            "target": target,
            "original": original,
            "replacement": args.to,
            "reason": args.reason,
            "recorded_at": now_iso(),
        }
    )
    save_state(state_path, state)
    say("deviation recorded: {} {}".format(nid, target))
    tier, labels = risk_tier(node)
    if tier == "high":
        say("risk: high ({})".format(", ".join(labels) or "declared risk high"))
    return EXIT_OK


def _format_variant_verification(verification: dict) -> str:
    """Format a variant's verification gate as ``<type> <target>``."""
    kind = verification.get("type", "")
    required = VERIFICATION_REQUIRED.get(kind, ())
    target_key = required[0] if required else None
    target = verification.get(target_key) if target_key else None
    if target not in (None, ""):
        return "{} {}".format(kind, target)
    return kind


def _print_active_node(state: dict, index: dict, nid: str, workspace: Path) -> None:
    """Print the active-node summary, variant overrides, interactive hints, and status header."""
    node = index[nid]
    say("active -> {}".format(summary(state, nid)))
    variant = active_variant(node)
    if variant:
        say("  variant: {} (overrides: {})".format(current_os(), ", ".join(sorted(variant))))
        if "commands" in variant:
            commands = variant["commands"]
            say("  commands: {}".format("; ".join(commands) if commands else "(none)"))
        if "verification" in variant:
            say("  verification: {}".format(_format_variant_verification(variant["verification"])))
    for hint in interactive_hints(node):
        say("  {}".format(hint))
    say(header_line(state, workspace))


def cmd_next(args, state_path: Path, workspace: Path) -> int:
    """Advance the active pointer, refusing to guess at branch points."""
    state = load_validated_state(state_path)
    preflight_gate(state, state_path, getattr(args, "skip_doctor", False))

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
        _print_active_node(state, index, args.node_id, workspace)
        return EXIT_OK

    active = state.get("active_node_id")
    index = node_index(state)
    if active and index.get(active, {}).get("status") == "pending":
        check_risk(state, active)
        _print_active_node(state, index, active, workspace)
        return EXIT_OK
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
    _print_active_node(state, index, ready[0], workspace)
    return EXIT_OK


def cmd_verify(args, state_path: Path, workspace: Path) -> int:
    """Run a node's verification gate and report the outcome."""
    state = load_validated_state(state_path)
    nid = resolve_target(state, args.node_id, workspace)
    node = get_node(state, nid)
    check_risk(state, nid)
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
    state = load_validated_state(state_path)

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

    if state.get("mode") == "hands-off":
        if risk_blocked(state, node):
            check_risk(state, nid)
        if not deviations_covered(node):
            raise CliError(
                "'{}' has deviated and hands-off mode needs a person to review it first. "
                "After they say yes, run `approve {}`.".format(nid, nid)
            )

    if not args.no_verify:
        passed, lines = run_verification(node, workspace, args.yes)
        for line in lines:
            say("  {}".format(line))
        if not passed:
            if state.get("mode") == "hands-off":
                err(
                    "verification failed for {} - state unchanged. "
                    "Fix the work and re-run.".format(nid)
                )
            else:
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
            say("nothing to commit; completed {} without a checkpoint".format(nid))
        else:
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


def orphaned_nodes(state: dict, sha: str, reset_set: set, workspace: Path) -> list:
    """Completed nodes outside ``reset_set`` whose checkpoint ``sha`` would discard.

    A rollback to ``sha`` drops every commit that is not an ancestor of it. A
    completed node that keeps its status after the rewind must keep its
    checkpoint too, or the DAG would claim work that is no longer there.
    """
    orphaned = []
    order, _ = topological_order(state)
    index = node_index(state)
    for nid in order:
        node = index[nid]
        commit = node.get("commit_sha")
        if nid in reset_set or node.get("status") != "completed" or not commit:
            continue
        code, _, _ = run_git(["merge-base", "--is-ancestor", commit, sha], workspace)
        if code != 0:
            orphaned.append(nid)
    return orphaned


def node_scopes(node: dict) -> list:
    """Side-effect scopes for a node; an absent ``side_effects`` means repo-only (C-G2)."""
    return list(node.get("side_effects") or ["repo"])


def side_effect_line(node: dict) -> str:
    """One listing line for a rewound node with non-repo side effects (C-G2)."""
    undo = effective_undo(node)
    tail = "undo: {}".format("; ".join(undo)) if undo else "undo: (none) | irreversible"
    return "side effects: {} [{}] {}".format(
        node["id"], ", ".join(node_scopes(node)), tail
    )


def residue_entry(node: dict, reason: str) -> dict:
    """A root ``residue`` record for one node (C-G2)."""
    return {
        "node_id": node["id"],
        "scopes": node_scopes(node),
        "undo": list(effective_undo(node)),
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
    for command in effective_undo(node):
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
    state = load_validated_state(state_path)

    target = args.target_id
    get_node(state, target)
    check_risk(state, target)
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
        orphaned = orphaned_nodes(state, sha, {target} | set(descendants), workspace)
        if orphaned:
            raise CliError(
                "refusing to roll back to {}: it would discard the checkpoints of "
                "completed nodes that are not downstream of {}: {}. Rewind to a "
                "common ancestor instead (`jump <ancestor> --reset-descendants "
                "--git-rollback {}`), or use --git-rollback none.".format(
                    short(sha), target, ", ".join(orphaned), args.git_rollback
                )
            )

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
        prefix = workspace_prefix(workspace)
        if prefix:
            committed = restore_workspace(workspace, sha, prefix)
            say("restored {} to {}{}".format(
                prefix.rstrip("/"), sha,
                " (rewind commit {})".format(short(head_sha(workspace), 12))
                if committed else " (no changes)",
            ))
        else:
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
            elif not effective_undo(node):
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
    state = load_validated_state(state_path)

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
    state = load_validated_state(state_path)
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
    state = load_validated_state(state_path)
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


def _sorted_deviations(state: dict) -> list:
    """Return ``(node, record)`` pairs for all deviations sorted by ``recorded_at``."""
    records = []
    for node in state.get("nodes") or []:
        for record in node.get("deviations") or []:
            records.append((node, record))
    records.sort(key=lambda pair: pair[1].get("recorded_at", ""))
    return records


def _md_cell(value) -> str:
    """Escape pipe characters and newlines for a single Markdown table cell."""
    return str(value).replace("|", "\\|").replace("\n", " ")


def render_report(state: dict, workspace: Path, out: Path) -> str:
    """Render a Markdown summary report (``report.md``) for the tutorial DAG."""
    title = state.get("title") or "Tutorial Report"
    source_doc = state.get("source_doc") or "(none)"
    mode = state.get("mode") or ""
    persistence = state.get("persistence") or ""
    branch = (state.get("git") or {}).get("branch") or current_branch(workspace) or "(none)"
    completed, skipped, total = counts(state)

    lines = [
        "# {}".format(title),
        "",
        "- source_doc: `{}`".format(source_doc),
        "- mode: `{}`".format(mode),
        "- persistence: `{}`".format(persistence),
        "- git.branch: `{}`".format(branch),
        "- progress: {} completed / {} skipped / {} total".format(completed, skipped, total),
        "",
    ]

    preflight = state.get("preflight")
    if isinstance(preflight, dict):
        status_text = "passed" if preflight.get("passed") else "failed"
        missing = preflight.get("missing") or []
        lines.extend([
            "## Preflight",
            "",
            "- status: {}".format(status_text),
            "- checked_at: {}".format(preflight.get("at") or ""),
        ])
        if missing:
            lines.append("- missing: {}".format(", ".join(missing)))
        requires = state.get("requires")
        if isinstance(requires, dict):
            _, rows, _ = run_checks(requires)
            if rows:
                lines.extend([
                    "",
                    "| Check | Status | Found | Need |",
                    "|---|---|---|---|",
                ])
                for row in rows:
                    lines.append(
                        "| {} | {} | {} | {} |".format(
                            _md_cell(row.get("name", "")),
                            _md_cell(row.get("status", "")),
                            _md_cell(row.get("found", "")),
                            _md_cell(row.get("need", "")),
                        )
                    )
        lines.append("")

    index = node_index(state)
    parents, _ = relation_maps(state)
    order, cycle = topological_order(state)
    if cycle:
        order = order + sorted(cycle)

    lines.extend(["## Nodes", ""])
    if order:
        lines.extend([
            "| ID | Title | Status | Commit | Artifacts | Deviations |",
            "|---|---|---|---|---|---|",
        ])
        for nid in order:
            node = index[nid]
            status_label = derived_state(state, nid, index, parents)
            sha = node.get("commit_sha")
            commit_label = sha[:7] if sha else "\u2014"
            artifacts = node.get("artifacts") or []
            artifacts_label = ", ".join(artifacts) if artifacts else "\u2014"
            dev_count = len(node.get("deviations") or [])
            lines.append(
                "| {} | {} | {} | {} | {} | {} |".format(
                    _md_cell(nid),
                    _md_cell(node.get("title", "")),
                    _md_cell(status_label),
                    _md_cell(commit_label),
                    _md_cell(artifacts_label),
                    dev_count,
                )
            )
    else:
        lines.append("None.")
    lines.append("")

    visual_embeds = []
    seen_visuals = set()
    out_abs = os.path.abspath(str(out))
    for nid in order:
        node = index[nid]
        for artifact in node.get("artifacts") or []:
            if Path(artifact).suffix.lower() not in IMAGE_EXTENSIONS:
                continue
            art_path = Path(artifact)
            full_path = art_path if art_path.is_absolute() else workspace / art_path
            if not full_path.is_file():
                continue
            full_abs = os.path.normpath(os.path.abspath(str(full_path)))
            if full_abs in seen_visuals:
                continue
            seen_visuals.add(full_abs)
            rel_posix = os.path.relpath(full_abs, out_abs).replace(os.sep, "/")
            visual_embeds.append("![{}]({})".format(artifact, rel_posix))
    if visual_embeds:
        lines.extend(["## Visual Artifacts", ""])
        lines.extend(visual_embeds)
        lines.append("")

    records = _sorted_deviations(state)
    lines.extend(["## Deviations", ""])
    if records:
        for node, record in records:
            lines.append(
                "- `{}` (`{}`): `{}` -> `{}` \u2014 {}".format(
                    node["id"],
                    record.get("target", ""),
                    record.get("original", ""),
                    record.get("replacement", ""),
                    record.get("reason", ""),
                )
            )
    else:
        lines.append("None.")
    lines.append("")

    residue_entries = state.get("residue") or []
    lines.extend(["## Residue", ""])
    if residue_entries:
        for entry in residue_entries:
            undo = entry.get("undo") or []
            scopes = ", ".join(entry.get("scopes") or [])
            undo_text = " (undo: {})".format("; ".join(undo)) if undo else ""
            lines.append(
                "- `{}` [{}]: {}{}".format(
                    entry.get("node_id", ""),
                    scopes,
                    entry.get("reason", ""),
                    undo_text,
                )
            )
    else:
        lines.append("None.")
    lines.append("")

    return "\n".join(lines)


def cmd_export(args, state_path: Path, workspace: Path) -> int:
    """Write report.md, deviations.md, and, when source_doc is readable, tutorial.corrected.md (C-G8)."""
    state = load_validated_state(state_path)

    if args.out:
        out = Path(args.out)
        if not out.is_absolute():
            out = workspace / out
    else:
        out = workspace / EXPORT_DIR_NAME
        ensure_excluded(workspace, EXPORT_DIR_NAME + "/")
    out = Path(os.path.normpath(str(out)))
    try:
        out.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise CliError("cannot create {}: {}".format(out, exc))

    records = _sorted_deviations(state)

    source_doc = state.get("source_doc") or ""
    source_text = None
    if source_doc and records:
        candidate = Path(source_doc)
        if not candidate.is_absolute():
            candidate = workspace / candidate
        try:
            source_text = candidate.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            source_text = None

    not_applied = []
    corrected = source_text
    if corrected is not None:
        for _, record in records:
            original = record.get("original", "")
            if original and original in corrected:
                corrected = corrected.replace(original, record.get("replacement", ""), 1)
            else:
                not_applied.append(record)

    lines = ["# Tutorial deviations", "", "Source: `{}`".format(source_doc or "(none)"), ""]
    if not records:
        lines.append("No deviations recorded.")
    for node, record in records:
        lines.extend([
            "## {}: {}".format(node["id"], node.get("title", "")),
            "",
            "- target: `{}`".format(record.get("target", "")),
            "- original: `{}`".format(record.get("original", "")),
            "- replacement: `{}`".format(record.get("replacement", "")),
            "- reason: {}".format(record.get("reason", "")),
            "- recorded_at: {}".format(record.get("recorded_at", "")),
            "",
        ])
    if not_applied:
        lines.extend(["## Not applied", ""])
        for record in not_applied:
            lines.append("- `{}` ({})".format(record.get("original", ""), record.get("target", "")))
        lines.append("")

    written = []
    try:
        stale_path = out / "tutorial.corrected.md"
        if corrected is None and stale_path.exists():
            stale_path.unlink()
        summary_path = out / "report.md"
        summary_path.write_text(render_report(state, workspace, out), encoding="utf-8")
        written.append(summary_path)
        report_path = out / "deviations.md"
        report_path.write_text("\n".join(lines), encoding="utf-8")
        written.append(report_path)
        if corrected is not None:
            corrected_path = out / "tutorial.corrected.md"
            corrected_path.write_text(corrected, encoding="utf-8")
            written.append(corrected_path)
    except OSError as exc:
        raise CliError("cannot write export to {}: {}".format(out, exc))
    for path in written:
        say("wrote {}".format(path))
    return EXIT_OK


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
    init.add_argument("--title", default=None,
                      help="tutorial title (default: the --from file's title)")
    init.add_argument("--source", default=None,
                      help="tutorial file or URL being run (default: the --from file's source_doc)")
    init.add_argument("--from", dest="from_path", metavar="DAG_JSON", default=None,
                      help="start from a prebuilt DAG; progress is stripped")
    init.add_argument("--mode", choices=MODES, default=None,
                      help="default assisted, or the --from file's mode")
    init.add_argument("--persistence", choices=PERSISTENCE_MODES, default="file")
    init.add_argument(
        "--git-branch",
        default=None,
        help="create/checkout this branch first (suggest tutorial/<slug>)",
    )
    init.add_argument("--force", action="store_true", help="overwrite an existing state file")
    init.set_defaults(func=cmd_init)

    scaffold = sub.add_parser("scaffold", help="package a DAG as its own skill directory")
    scaffold.add_argument("name", help="skill name (lowercase, digits, dot, dash, underscore)")
    scaffold.add_argument("--from", dest="from_path", metavar="DAG_JSON", required=True,
                          help="prebuilt DAG to package; progress is stripped")
    scaffold.add_argument("--dest", default=None,
                          help="parent directory (default: .agents/skills under the workspace)")
    scaffold.add_argument("--force", action="store_true", help="replace an existing skill directory")
    scaffold.set_defaults(func=cmd_scaffold)

    draft = sub.add_parser("draft", help="print skeleton nodes from a Markdown tutorial as JSON")
    draft.add_argument("path", help="Markdown tutorial to read")
    draft.add_argument("--write", action="store_true",
                       help="fill the empty nodes of the state file with the draft")
    draft.set_defaults(func=cmd_draft)

    validate = sub.add_parser("validate", help="check structure, ids, and acyclicity")
    validate.set_defaults(func=cmd_validate)

    status = sub.add_parser("status", help="turn header plus topological node table")
    status.add_argument(
        "--json", action="store_true", help="print the state document (in-context round trip)"
    )
    status.set_defaults(func=cmd_status)

    nxt = sub.add_parser("next", help="advance the active pointer")
    nxt.add_argument("node_id", nargs="?", default=None, help="ready node to activate")
    nxt.add_argument(
        "--skip-doctor",
        action="store_true",
        help="continue past a failed preflight (requires block) without running doctor",
    )
    nxt.set_defaults(func=cmd_next)

    doctor = sub.add_parser("doctor", help="check the tools and OS declared in requires")
    doctor.add_argument("--json", action="store_true", help="print the checks as JSON")
    doctor.set_defaults(func=cmd_doctor)

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

    export = sub.add_parser(
        "export", help="write summary report, deviation report, and corrected tutorial"
    )
    export.add_argument(
        "--out", default=None, metavar="DIR",
        help="output directory (default: {}/ under the workspace)".format(EXPORT_DIR_NAME),
    )
    export.set_defaults(func=cmd_export)

    approve = sub.add_parser("approve", help="record a person's approval of a high-risk node")
    approve.add_argument("node_id", help="node whose current risk tier to approve")
    approve.set_defaults(func=cmd_approve)

    deviate = sub.add_parser("deviate", help="replace a check or command and record why")
    deviate.add_argument("node_id", nargs="?", default=None, help="node (default: active)")
    which = deviate.add_mutually_exclusive_group(required=True)
    which.add_argument(
        "--command", dest="index", type=int, metavar="N", help="replace commands[N] (0-based)"
    )
    which.add_argument("--check", action="store_true", help="replace the command check")
    deviate.add_argument("--to", required=True, metavar="CMD", help="replacement text")
    deviate.add_argument("--reason", required=True, metavar="TEXT", help="why the text is wrong")
    deviate.set_defaults(func=cmd_deviate)

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
