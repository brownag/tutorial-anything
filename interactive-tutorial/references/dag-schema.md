# DAG State Reference (`.tutorial-dag.json`)

Machine contract: `dag-schema.json` (JSON Schema Draft 2020-12). This file is the
short version an agent reads in one pass. `scripts/dag_manager.py validate` enforces
the same rules with the standard library only (it does not import `jsonschema`).

## Root object

| Field | Type | Notes |
|---|---|---|
| `schema_version` | string | `"1.0"`. Unknown majors are a hard validation error. |
| `title` | string | Tutorial title, as given to `init --title`. |
| `source_doc` | string | Path or URL of the tutorial being run. |
| `mode` | `guided` \| `assisted` \| `hands-off` | Autonomy level. Default `assisted`. |
| `persistence` | `file` \| `in-context` | `file` keeps state in `.tutorial-dag.json`; `in-context` keeps the same document in the conversation and hands it to the CLI with `--file`. |
| `active_node_id` | string \| null | The node the user is on. `null` before the first `next`. |
| `created_at`, `updated_at` | string | ISO-8601 UTC, `2026-01-31T09:12:00Z`. |
| `git` | object | See **git block**. |
| `nodes` | array | See **node object**. Non-empty. |

## git block

| Field | Type | Notes |
|---|---|---|
| `branch` | string \| null | Branch recorded at `init`; `tutorial/<slug>` when the agent created one. |
| `initial_commit_sha` | string | Full SHA of `HEAD` at `init`. `""` when the repo has no commits yet — rollback then falls back to a warning, not a reset. |
| `initial_dirty` | boolean | Whether the tree had uncommitted changes at `init`. |
| `exclude_configured` | boolean | `true` once `.tutorial-dag.json` is in `.git/info/exclude`. |

## node object

| Field | Type | Notes |
|---|---|---|
| `id` | string | `^[a-z0-9][a-z0-9._-]*$`, unique. Use `1-setup`, `2-scaffold`, `3a-persist` style ids so display order reads sensibly. |
| `title` | string | One line, imperative. |
| `summary` | string | What to do and what "done" looks like, 1-3 sentences. |
| `dependencies` | string[] | Ids that must be `completed` or `skipped` first. `[]` for a root. Model parallel/optional sections as true branches here, not as extra linear steps. |
| `action_type` | `read-only` \| `instruct` \| `mutate` \| `execute` | See taxonomy below. |
| `status` | `pending` \| `completed` \| `skipped` | Stored state only; `ready`/`active` are derived. |
| `commit_sha` | string \| null | Checkpoint created by `complete --commit`. |
| `artifacts` | string[] | Files the node creates or edits. |
| `commands` | string[] | Suggested shell lines, in order. The CLI never runs these automatically. |
| `verification` | object | Required for every node. See below. |

### action_type taxonomy

| Value | Meaning | Checkpointed? |
|---|---|---|
| `read-only` | Concepts, code review, discussion. No file changes. | No |
| `instruct` | Manual step outside the workspace (sign up, click, restart a service). | No |
| `mutate` | Creates/edits/appends to files. | Yes, with `--commit` |
| `execute` | Runs a build/test/command. Stateful ones (generates files, migrations) commit too. | Yes, with `--commit` |

### verification variants

`verification.type` selects the required keys; the CLI implements exactly these four.

```json
{ "type": "command", "command": "python3 -m pytest -q", "pattern": "(?s).*passed.*", "expect_exit": 0 }
{ "type": "file_exists", "path": "src/todo/__main__.py" }
{ "type": "file_contains", "path": "src/todo/cli.py", "pattern": "add_parser(\"add\"", "regex": false }
{ "type": "confirm", "prompt": "Confirm the deploy log shows a green run." }
```

- `command` — runs through the shell in the workspace root. Passes on exit code `expect_exit`
  (default `0`) and, if `pattern` is set, when that regex matches combined stdout+stderr.
- `file_exists` / `file_contains` — `path` is workspace-relative and must stay inside it.
  `file_contains` matches a literal substring unless `regex: true`.
- `confirm` — no automated check. Fails until a human supplies `--yes` to `verify`/`complete`.
  Use only when nothing programmable exists.

## Derived states

Computed on every read; never stored.

| Derived | Rule |
|---|---|
| `ACTIVE` | `id == active_node_id` |
| `READY` | `status == pending` **and** every dependency is `completed` or `skipped` |
| `PENDING` | `status == pending` and at least one dependency is still `pending` (blocked) |
| `COMPLETED` / `SKIPPED` | stored status, shown as-is |

## Legal status transitions

| From → To | How |
|---|---|
| `pending → completed` | `complete` (verification must pass unless `--no-verify`) |
| `pending → skipped` | `jump <target> --skip-ancestors` |
| `completed → pending` | `jump <target> --reset-descendants` |
| `skipped → pending` | `jump <target> --reset-descendants` |
| `skipped → completed` | `jump <node>` then `complete <node>` (work done late) |
| `completed → skipped` | **illegal** — rewind with `--reset-descendants` instead |

`--reset-descendants` also nulls the affected nodes' `commit_sha`. Rolling back Git history
(`--git-rollback branch|hard`) is a separate, explicit decision from resetting node status.

## Annotated minimal example

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
      "summary": "No files change. Done when you can say why subcommands beat positional args here.",
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
      "summary": "Create the package skeleton so later nodes have somewhere to attach.",
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
      "summary": "Optional feature branch. Sibling of 3b-format, not its successor.",
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
      "summary": "Independent optional branch: both 3a and 3b unlock from 2-scaffold.",
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
      "summary": "Converges both optional branches before the tutorial calls itself done.",
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

This is a diamond: `1 → 2 → {3a, 3b} → 4`. A decomposition that instead listed
`1, 2, 3a, 3b, 4` in a line would be wrong — the two optional features are siblings, and
siblings are what makes jump-ahead and rewind worth having.
