# DAG State Reference (`.tutorial-dag.json`)

Machine contract: `dag-schema.json` (JSON Schema Draft 2020-12). `scripts/dag_manager.py validate` enforces these rules using the Python standard library.

## Root object

| Field | Type | Notes |
|---|---|---|
| `schema_version` | string | `"1.0"`. Unsupported versions fail validation. |
| `title` | string | Tutorial title from `init --title`. |
| `source_doc` | string | Path or URL of the tutorial. |
| `mode` | `guided` \| `assisted` \| `hands-off` | Autonomy level. Default `assisted`. |
| `persistence` | `file` \| `in-context` | `file` writes `.tutorial-dag.json`. `in-context` stores state in conversation memory. |
| `active_node_id` | string \| null | Current active node. `null` before initial `next`. |
| `created_at`, `updated_at` | string | ISO-8601 UTC timestamp. |
| `git` | object | Git repository tracking metadata. |
| `nodes` | array | List of node objects. Cannot be empty. |

## git block

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

Resetting descendants clears their `commit_sha`. Reverting Git files (`--git-rollback branch|hard`) is a separate option.

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
- Schema 1.1 is written by `init`. Schema 1.0 documents remain valid.

## Risk tiers and approval (C-G6)

Node fields (both optional):

| Field | Type | Notes |
|---|---|---|
| `risk` | string | `low`, `elevated`, or `high`. A declared value can raise a node's tier, never lower it. |
| `approved` | object | `{"tier": tier, "at": iso8601}`, written by `approve <id>`. |

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
- Gate: in `hands-off` mode only, `next` (explicit or implicit) refuses an effective-`high` node without covering approval (exit 1, message names `approve <id>` and the labels). `jump --undo` in `hands-off` records `not-run` residue instead of undoing such a node. In `guided` and `assisted` nothing is blocked; `next` prints `risk: high (<labels>)`.
- `status` shows a `RISK` column (`low`, `elev`, `HIGH`).
- `validate` errors on an unknown `risk` value or a malformed `approved` object.

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
