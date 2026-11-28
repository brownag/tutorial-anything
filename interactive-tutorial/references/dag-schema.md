# DAG State Reference (`.tutorial-dag.json`)

Machine contract: `dag-schema.json` (JSON Schema Draft 2020-12). `scripts/dag_manager.py validate` enforces these rules using the Python standard library.

## Root object

| Field | Type | Notes |
|---|---|---|
| `schema_version` | string | `"1.2"` (written by `init`). Any `1.x` passes validation; other majors fail. |
| `title` | string | Tutorial title from `init --title`. |
| `source_doc` | string | Path or URL of the tutorial. |
| `mode` | `guided` \| `assisted` \| `hands-off` | Autonomy level. Default `assisted`. |
| `persistence` | `file` \| `in-context` | `file` writes `.tutorial-dag.json`. `in-context` stores state in conversation memory. |
| `active_node_id` | string \| null | Current active node. `null` before initial `next`. |
| `created_at`, `updated_at` | string | ISO-8601 UTC timestamp. |
| `git` | object | Git repository tracking metadata. |
| `nodes` | array | List of node objects. Cannot be empty. |

## git block

Git operations are limited to the workspace. When it is a subdirectory of the repository, `complete --commit` stages and commits only that subdirectory, dirty checks ignore changes outside it, and `jump --git-rollback hard` restores only it to the rollback commit and records the restore as a `tutorial(rewind): restore <subdir> to <sha12>` commit instead of moving `HEAD` back. At the repository root, `--git-rollback hard` runs `git reset --hard`. `--git-rollback branch` checks out a new branch for the whole repository in both cases.

| Field | Type | Notes |
|---|---|---|
| `branch` | string \| null | Branch recorded at `init`. |
| `initial_commit_sha` | string | HEAD commit SHA at `init`. Empty string if repository has no commits. |
| `initial_dirty` | boolean | `true` if uncommitted changes existed at `init`. |
| `exclude_configured` | boolean | `true` when `.tutorial-dag.json` is added to `.git/info/exclude`. |

## node object

| Field | Type | Notes |
|---|---|---|
| `id` | string | Unique identifier (`^[a-z0-9][a-z0-9._-]*$`). Use prefixes like `1-setup`, `2-build`. |
| `title` | string | Short imperative summary. |
| `summary` | string | Node requirements and expected outcome. 1 to 3 sentences. |
| `dependencies` | string[] | IDs of prerequisites that must be completed or skipped. Empty for root nodes. |
| `action_type` | `read-only` \| `instruct` \| `mutate` \| `execute` | Node category. See table below. |
| `status` | `pending` \| `completed` \| `skipped` | Stored state. `ready` and `active` are computed dynamically. |
| `commit_sha` | string \| null | Checkpoint commit created by `complete --commit`. |
| `artifacts` | string[] | Files created or modified by this node. |
| `commands` | string[] | Shell commands for this step. The CLI does not execute these automatically. |
| `verification` | object | Verification rule. Required on every node. |
| `deviations` | object[] | Optional. Records written by `deviate` (schema 1.2). See "Deviations (C-G8)". |
| `source_ref` | object | Optional. `{line, heading}`: `line` is a 1-based integer >= 1 (the source heading line), `heading` a string. Written by `draft`. See "Draft (C-G10)". |
| `variants` | object | Optional. Per-OS overrides (`linux`, `darwin`, `windows`) for `commands`, `verification`, and `undo`. See "OS variants (C-G5)". |

### OS variants (C-G5)

`variants` maps one or more OS keys (`linux`, `darwin`, `windows`) to an override object with any subset of `commands`, `verification`, and `undo` (at least one property). The CLI resolves the active OS using `platform.system().lower()` and falls back field-by-field to the node's top-level `commands`, `verification`, and `undo` when no matching OS entry or field is present. Pair `node.variants` with root `requires.os` to gate unsupported platforms during preflight.

### action_type taxonomy

| Value | Meaning | Uses Git checkpoint |
|---|---|---|
| `read-only` | Concepts, design review, discussion. No file changes. | No |
| `instruct` | Manual external steps (sign up, dashboard UI, hardware restart). | No |
| `mutate` | Creates or edits files. | Yes (`--commit`) |
| `execute` | Runs builds or tests. Commits if files change. | Yes (`--commit`) |

### verification types

The CLI supports four verification types:

```json
{ "type": "command", "command": "python3 -m pytest -q", "pattern": "(?s).*passed.*", "expect_exit": 0 }
{ "type": "file_exists", "path": "src/todo/__main__.py" }
{ "type": "file_contains", "path": "src/todo/cli.py", "pattern": "add_parser(\"add\"", "regex": false }
{ "type": "confirm", "prompt": "Confirm the deploy log shows a green run." }
```

- `command`: runs in the workspace root. Passes if exit code matches `expect_exit` (default `0`) and regex pattern matches combined stdout and stderr.
- `file_exists`: checks for file existence at workspace-relative path.
- `file_contains`: searches file for string. Uses exact substring matching unless `regex: true`.
- `confirm`: requires manual approval via `--yes` flag on `verify` or `complete`.

## Derived states

Computed on every read and never stored:

| State | Condition |
|---|---|
| `ACTIVE` | Node ID matches `active_node_id`. |
| `READY` | Status is `pending` and all dependencies are `completed` or `skipped`. |
| `PENDING` | Status is `pending` and one or more dependencies are incomplete. |
| `COMPLETED` / `SKIPPED` | Stored status values. |

## Status transitions

| From → To | Method |
|---|---|
| `pending → completed` | Run `complete` (verification check must pass). |
| `pending → skipped` | Run `jump <target> --skip-ancestors`. |
| `completed → pending` | Run `jump <target> --reset-descendants`. |
| `skipped → pending` | Run `jump <target> --reset-descendants`. |
| `skipped → completed` | Run `jump <node>` and then `complete <node>`. |
| `completed → skipped` | Invalid transition. Reset to pending first. |

Resetting descendants clears their `commit_sha`. Reverting Git files (`--git-rollback branch|hard`) is a separate option. A rollback refuses (exit 1, no changes) when a completed node outside the target and its descendants has a `commit_sha` that is not an ancestor of the rollback commit, because that node would stay `completed` without its files.

## Side effects, undo, and residue (schema 1.1)

Node fields:

| Field | Type | Notes |
|---|---|---|
| `side_effects` | array | Non-empty unique subset of `repo`, `user`, `system`, `remote`. Absent means `["repo"]`. |
| `undo` | array of strings | Commands that revert the node's non-repo effects. Each must be non-empty. |

Root field `residue` (optional array) holds entries `{"node_id", "scopes", "undo", "reason", "recorded_at"}`. `reason` is `not-run`, `failed`, `irreversible`, or `remote`.

- `jump <id> --reset-descendants` lists each rewound node (status `completed` to `pending`) that has non-`repo` scopes, newest first: `side effects: <id> [<scopes>] undo: <cmd; …> | irreversible`.
- `jump --undo` (requires `--reset-descendants`) runs each listed node's `undo` commands, newest node first, when its scopes minus `repo` are within `user` and `system`. Nodes with `remote` become `remote` residue and are not undone. Nodes with no `undo` become `irreversible`. On the first failure, that node and the remaining listed nodes become residue (`failed`, then `not-run`) and the command exits 1.
- Without `--undo`, every listed node becomes `not-run` residue and the command exits 0.
- `residue clear [<node_id> | --all]` removes entries.
- `status` lists residue entries. The header appends ` | residue: N` inside the closing `]` when N > 0.
- `validate` warns (not an error) when a node has non-`repo` side effects but no `undo`.
- Schema 1.0 and 1.1 documents remain valid.

## Risk tiers and approval (C-G6)

Node fields (both optional):

| Field | Type | Notes |
|---|---|---|
| `risk` | string | `low`, `elevated`, or `high`. A declared value can raise a node's tier, never lower it. |
| `approved` | object | `{"tier": tier, "at": iso8601, "deviations": int}`, written by `approve <id>`. `deviations` (optional, non-negative) counts the node's deviations it covers. |

Tiers are ordered `low < elevated < high`. The effective tier is the higher of the computed tier and `risk`.

- Computed `low`: `side_effects` is `["repo"]` (or absent). Computed `elevated`: any of `user`, `system`, `remote`.
- Computed `high`: any inspected command matches a HIGH pattern. Inspected commands are `commands`, `undo`, and a `verification.command` check. Each match adds its label:

| Label | Matches |
|---|---|
| `sudo` | `sudo` as a word |
| `pipe-to-shell` | `curl`/`wget` piped into `sh`/`bash`/`zsh` (optionally via `sudo`) |
| `recursive-delete` | `rm -r…` targeting `/`, `~`, or `$HOME` |
| `chmod-777` | `chmod 777` or `chmod -R 777` |
| `system-path-write` | `>` or `tee` into `/etc`, `/usr`, `/opt`, `/boot`, or `/var` |
| `mkfs` | `mkfs` |
| `dd` | `dd if=` |

- `approve <id>` records `approved` with the node's current effective tier. The approval covers the node while its effective tier is at or below the recorded tier. A later edit that raises the tier needs a new approval.
- Gate: in `hands-off` mode only, `next` (explicit or implicit), `jump <id>`, `verify`, and `service start` refuse an effective-`high` node without covering approval (exit 1, message names `approve <id>` and the labels). `jump` refuses before it changes any state or files. `jump --undo` in `hands-off` records `not-run` residue instead of undoing such a node. In `guided` and `assisted` nothing is blocked; `next`, `jump`, and `verify` print `risk: high (<labels>)`.
- `status` shows a `RISK` column (`low`, `elev`, `HIGH`).
- `validate` errors on an unknown `risk` value or a malformed `approved` object.
- Hands-off `complete` also runs the risk gate: an effective-`high` node without covering approval is refused (exit 1, names `approve <id>`), even when `next` did not activate it.

## Deviations (C-G8)

`deviate` records a fix to a stale tutorial. Each record in a node's `deviations` array has these fields:

| Field | Type | Notes |
|---|---|---|
| `target` | string | `check` (the `command` verification) or `command:<N>` (0-based index into `commands`). Pattern `^(check\|command:[0-9]+)$`. |
| `original` | string | Text before the change. |
| `replacement` | string | Text after the change. Non-empty. |
| `reason` | string | Why the tutorial is wrong. Non-empty. |
| `recorded_at` | string | ISO 8601 time. |

- `deviate [<node_id>] (--command N | --check) --to "<cmd>" --reason "<text>"` replaces the text and appends the record. The default node is the active node. `--check` needs a `command` check. Out-of-range `N`, `--to` equal to the current text, or an empty `--to` or `--reason` exits 2. A completed node exits 1; run `jump <id> --reset-descendants` first. Output: `deviation recorded: <id> <target>`, plus `risk: high (...)` when the new text is high-risk.
- `approve <id>` sets `approved.deviations` to the node's deviation count. A later deviation makes the count stale.
- Hands-off gate: `complete` refuses a node whose deviations are not all covered by `approve` (exit 1, names `approve <id>`). A later deviation blocks again. `guided` and `assisted` are never blocked.
- `status` shows `deviations: N` in the active block and a `deviations  <id>: N` line per node with deviations.
- `export [--out DIR]` (default `<workspace>/.tutorial-export`, git-excluded) always writes `deviations.md`. It writes `tutorial.corrected.md` when `source_doc` is a readable local file and at least one deviation is recorded: each `original` is replaced by its `replacement` (first occurrence, in recorded order). Originals not found are listed under `## Not applied`. Exit 0; prints the written paths.

## Draft (C-G10)

`draft <path> [--write]` builds a skeleton DAG from a Markdown tutorial. It is a starting point to check against the tutorial, not a finished DAG.

- Each H2/H3 heading starts a node. If the file has no H2/H3, each top-level numbered item (`N. `) does. Text before the first section is ignored; the H1 is the title hint.
- Nodes form a linear chain (`dependencies` = previous node). `id` is `<n>-<slug>`. Shell fences become `commands`; `action_type` is guessed (`execute` for shell fences, `mutate` for other fences, else `read-only`).
- Each node gets a placeholder check: `{"type": "confirm", "prompt": "DRAFT: replace with a real check for <title>"}`, and `source_ref` = `{line, heading}`.
- Output is always JSON (`{"title", "nodes"}`). There is no `--json` flag.
- `--write` requires an existing state file with an empty `nodes` array (otherwise exit 1, `nodes already present`). It writes the nodes, validates, and prints the header line.
- `validate` warns `node '<id>' still has a draft check` for any `confirm` prompt starting with `DRAFT:`. Replace these before completing the tutorial.

## Sample state file

```json
{
  "schema_version": "1.0",
  "title": "Build a tiny todo CLI",
  "source_doc": "examples/sample-tutorial.md",
  "mode": "assisted",
  "persistence": "file",
  "active_node_id": "1-concept",
  "created_at": "2026-01-31T09:12:00Z",
  "updated_at": "2026-01-31T09:20:41Z",
  "git": {
    "branch": "tutorial/todo-cli",
    "initial_commit_sha": "4c1f9a2b8e0d3f6a7b1c5d8e9f0a1b2c3d4e5f60",
    "initial_dirty": false,
    "exclude_configured": true
  },
  "nodes": [
    {
      "id": "1-concept",
      "title": "Read the command-dispatch design",
      "summary": "Review design goals. No files change.",
      "dependencies": [],
      "action_type": "read-only",
      "status": "completed",
      "commit_sha": null,
      "artifacts": [],
      "commands": [],
      "verification": { "type": "confirm", "prompt": "Explain the dispatch design in one sentence." }
    },
    {
      "id": "2-scaffold",
      "title": "Scaffold the package",
      "summary": "Create package structure and main CLI file.",
      "dependencies": ["1-concept"],
      "action_type": "mutate",
      "status": "pending",
      "commit_sha": null,
      "artifacts": ["src/todo/cli.py"],
      "commands": ["mkdir -p src/todo"],
      "verification": { "type": "file_exists", "path": "src/todo/cli.py" }
    },
    {
      "id": "3a-persist",
      "title": "Add JSON persistence (optional branch A)",
      "summary": "Add disk storage support.",
      "dependencies": ["2-scaffold"],
      "action_type": "mutate",
      "status": "pending",
      "commit_sha": null,
      "artifacts": ["src/todo/store.py"],
      "commands": [],
      "verification": { "type": "file_contains", "path": "src/todo/store.py", "pattern": "def save" }
    },
    {
      "id": "3b-format",
      "title": "Add table output (optional branch B)",
      "summary": "Add terminal table output.",
      "dependencies": ["2-scaffold"],
      "action_type": "mutate",
      "status": "pending",
      "commit_sha": null,
      "artifacts": ["src/todo/render.py"],
      "commands": [],
      "verification": { "type": "file_contains", "path": "src/todo/render.py", "pattern": "def render" }
    },
    {
      "id": "4-test",
      "title": "Run the test suite",
      "summary": "Verify both features work together.",
      "dependencies": ["3a-persist", "3b-format"],
      "action_type": "execute",
      "status": "pending",
      "commit_sha": null,
      "artifacts": [],
      "commands": ["python3 -m pytest -q"],
      "verification": { "type": "command", "command": "python3 -m pytest -q", "expect_exit": 0 }
    }
  ]
}
```

This graph uses a diamond pattern (`1 → 2 → {3a, 3b} → 4`). Steps `3a` and `3b` are sibling nodes that depend on `2`. Both must complete before step `4`.

## Background services and http/port checks (C-G3)

Verification types `http` and `port_listening` can gate any node.

| Type | Fields | Passes when |
|---|---|---|
| `http` | `url` (`http://` or `https://`), `expect_status` (default 200), `pattern` (optional regex on the body), `timeout` (seconds, default 5) | The response status equals `expect_status`, and the body matches `pattern` if given. Proxies are ignored. |
| `port_listening` | `port` (integer 1-65535), `host` (default `127.0.0.1`), `timeout` (seconds, default 2) | A TCP connection to `host:port` is accepted. |

Node field `service` (optional) declares a background process the CLI starts and stops:

| Field | Type | Notes |
|---|---|---|
| `command` | string | Run through the shell in the workspace. |
| `ready` | verification | Any type except `confirm`. Polled until it passes. |
| `ready_timeout` | integer | Seconds to wait for `ready`. Default 30. |
| `stop_signal` | `TERM` \| `INT` \| `KILL` | Sent first on stop. Default `TERM`. |

Root field `services` (optional) maps node ids to `{"pid", "pgid", "command", "log", "started_at"}`. Logs go to `.tutorial-logs/<id>.log`.

- `service start [<id>]` (default: the active node) starts the command detached in its own process group with stdin from `/dev/null` and output to the log. It polls `ready` until it passes or `ready_timeout` elapses. On failure it stops the process, drops the record, prints the log tail, and exits 1. It refuses if the recorded process is still alive.
- `service stop [<id>]` signals the process group, waits up to 5 seconds, then sends `KILL`. An already-dead process is not an error.
- `teardown` stops every recorded service. Run it at the end of a session.
- `jump <id> --reset-descendants` stops the services of every node it resets, including the target when it is reset.
- `status` lists `services: <id> (pid N, running|dead)`. The header appends ` | svc: N` when N > 0.
- `service` commands are POSIX only. Elsewhere they exit 2 with `services require a POSIX system`.
- `service.command` counts as an inspected command for risk tiers (C-G6).
- The CLI adds `.tutorial-logs/` to `.git/info/exclude`. It never edits a learner's `.gitignore`.

## Interactive steps and command-check stdin (C-G11)

- A `command` check accepts two new keys. `stdin` is text fed to the command's standard input. `timeout` is seconds (default 600). Without `stdin`, the command runs with `/dev/null` as stdin, so it reads EOF instead of inheriting the CLI's stdin. This is a behavior change from earlier versions.
- Node `interactive` (optional): `{"answers": [str…], "human": bool}`.
  - `answers` prints a hint on `next` and `status`: `feed answers: printf '%s\n' <quoted answers> | <command>`.
  - `human: true` prints `a person must run this step (interactive)`. `validate` warns when `human` is true and the verification is not `confirm`.
- `validate` warns `node '<id>' command looks interactive: <cmd>` when a command matches a known interactive pattern (`npm init` without `-y`, an unflagged `npx create-*`, `vim`/`nano`, a bare REPL such as `python3`, `git rebase -i`, `ssh-keygen` without `-N`, `passwd`, `read`). The warning does not fire when the node has an `interactive` block.

## Preflight: requires and doctor (C-G4)

Root field `requires` (optional) declares what the workspace must provide:

- `tools`: object of command name to spec. A spec is `"*"` (any version, or none if the tool runs), `">=X[.Y[.Z]]"`, or `{"min": "X.Y", "version_command": "<shell command>"}`. The default version command is `<name> --version`. The version is the first dotted number in its stdout or stderr. Versions compare as tuples, padded to three parts.
- `os` (optional): a unique array of `linux`, `darwin`, `windows`. Use together with `node.variants` when a tutorial supports multiple OSes that need different `commands`, `verification`, or `undo` (matched via `platform.system().lower()` with fallback to top-level node fields).

Root field `preflight` (optional) holds `{"passed", "at", "missing"}`, written by `doctor` and by the first `next`.

- `doctor [--json]` prints one row per tool: `ok`, `missing`, `too-old`, or `unknown-version`, then `name`, the found version, and `(need <spec>)`. It also prints an `os` row. It exits 0 when every check passes, otherwise 1. Each command times out after 10 seconds.
- `unknown-version` passes only for the `"*"` spec.
- `next` runs the check first when `requires` exists, no node is completed or skipped, and `preflight.passed` is not true. On failure it exits 1 with `run doctor, or pass --skip-doctor`. `next --skip-doctor` bypasses the check.
