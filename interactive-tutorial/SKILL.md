---
name: interactive-tutorial
description: Turn a static coding tutorial, README, workshop doc, or technical walkthrough into an interactive graph you step through one step at a time, jump ahead into, and rewind out of. Use when the user asks to follow, work through, run, or execute a tutorial or walkthrough, wants to resume a half-finished tutorial, asks to convert documentation into a checkable step-by-step plan, or mentions a tutorial DAG, nodes, fast-forward, jump ahead, or rewind. Decomposes the document into verifiable nodes with real dependencies, tracks progress in .tutorial-dag.json, gates every step behind its own verification command, and checkpoints each file-changing step as a git commit so steps can be replayed or reverted. Supports guided, assisted, and hands-off autonomy plus file and in-context state persistence.
license: MIT
author: Andrew Gene Brown
version: 1.2.0
compatibility: python>=3.9, git>=2.25
---

# Interactive Tutorial Runner

Tutorials often have core steps, optional paths, and distinct dependencies. Running them as a strict sequence breaks when late steps depend on skipped sections. This skill models tutorials as a DAG. It checks every step with an automated verification rule and commits each file change to Git.

Run the CLI from the tutorial workspace. Set `$SKILL_DIR` once per session:

```bash
SKILL_DIR="<absolute path of this skill directory>"
python3 "$SKILL_DIR/scripts/dag_manager.py" --help
```

Commands read `./.tutorial-dag.json` by default. Override the path with `--file`. Verification checks run inside `--cwd` (defaults to current directory).

## Step 1 — Session initialization and mode selection

1. Check for existing state. If `.tutorial-dag.json` exists, read it with `status` and resume. Do not overwrite it without explicit confirmation.
2. Set up Git. Checkpoints and rewinds require it.
   - Run `git init` if the workspace is not a repository.
   - Add a `.gitignore` for build output the tutorial creates (for example `__pycache__/`, `node_modules/`).
   - Check `git config user.name` and `git config user.email` before committing; only set repo-local values (`git config --local ...`) if unset, and never override a configured global identity.
   - Make a baseline commit of the existing files before `init`. If the workspace is dirty, commit the sources first.
   - Run `init` with a tutorial branch:

   ```bash
   python3 "$SKILL_DIR/scripts/dag_manager.py" init \
     --title "<tutorial title>" --source <path-or-url> \
     --mode assisted --git-branch tutorial/<slug>
   ```

   The baseline commit sets `initial_commit_sha`, so checkpoints hold only tutorial changes. `--git-branch` works in a repository with no commits, but rollback needs the baseline commit. `init` appends `.tutorial-dag.json` and `.tutorial-logs/` to `.git/info/exclude`, so Git does not track progress records or service logs (do not add them to `.gitignore`).
3. If the DAG declares `requires`, run `doctor` before the first `next`. `next` runs it too and stops on failure. Pass `--skip-doctor` only after the user accepts the missing tools.
4. Select autonomy and persistence modes:

   | Mode | Command execution | `confirm` check handler |
   |---|---|---|
   | `guided` | User runs commands | User |
   | `assisted` (default) | Agent proposes, user approves | User |
   | `hands-off` | Agent runs unattended | Agent (if pre-approved) |

   | Persistence | State location | Best for |
   |---|---|---|
   | `file` (default) | `.tutorial-dag.json` | Standard environments |
   | `in-context` | Conversation memory | Ephemeral or read-only filesystems |

   In `hands-off` mode:
   - At a branch point, take the paths—including `"(optional)"` sections—in document order unless the user said otherwise, and say which you picked.
   - A blanket pre-approval covers every `confirm` check. Before passing `--yes`, state in your reply what you confirmed. For an unverifiable `confirm` step with no workspace artifact or command output to inspect (for example, writing in a personal changelog), pass `--yes` and note in your reply that no verifiable artifact exists.
   - Pre-approval never covers `approve`.

## Step 2 — DAG decomposition

Read the tutorial first. Then run `draft` after `init` to get a skeleton:

```bash
python3 "$SKILL_DIR/scripts/dag_manager.py" draft <tutorial.md> --write
```

`draft` makes one node per H2/H3 heading (or numbered item) in a linear chain. It only knows fenced shell commands, so prose-only steps come out `read-only` with no `commands`. Treat the draft as a skeleton to check against the tutorial. Refine the `nodes` array in `.tutorial-dag.json` (an editor or a short script) with real dependencies and branches, real checks in place of the `DRAFT:` placeholders, and `side_effects`, `service`, and `interactive` on nodes, and the root `requires`, where they apply. Then run `validate` until no `still has a draft check` warnings remain:

```bash
python3 "$SKILL_DIR/scripts/dag_manager.py" validate
```

Rules:
- Keep nodes atomic. Ensure each node has a clear verification check.
- Use explicit dependencies. Model optional steps as branches off their parent and title them "(optional)". Nothing downstream of a required step may depend on an optional one.
- Set `action_type`: `read-only` (review), `instruct` (manual external task), `mutate` (file changes), or `execute` (build or test command).
- Populate `artifacts` for `mutate` nodes and `commands` with the tutorial's own shell commands. The CLI does not run node commands. Never put `dag_manager.py` calls in `commands`; the agent runs `service` through the CLI.
- Model manual stops (for example, "press Ctrl-C") as an `execute` node with `commands: []` that runs `service stop <id>` via the CLI and verifies the port is closed with a `command` check (for example `expect_exit: 1`).

Verification types and fields (prefer an automated check; map tutorial "confirm" or "verify" wording about observable output, files, or endpoints to automated checks, and reserve `confirm` for steps with no command or file check):

- `command`: `command` (required); optional `expect_exit` (default `0`), `pattern` (regex on combined stdout/stderr), `stdin`, `timeout` (seconds, default `600`).
- `file_exists`: `path` (required, workspace-relative).
- `file_contains`: `path`, `pattern` (required — not `text`), optional `regex` (boolean, default `false` — literal substring unless `true`).
- `port_listening`: `port` (required, `1`–`65535`); optional `host` (default `127.0.0.1`), `timeout` (seconds, default `2`).
- `http`: `url` (required, `http://` or `https://`); optional `expect_status` (default `200`), `pattern` (regex on response body), `timeout` (seconds, default `5`).
- `confirm`: `prompt` (required).

- `file_exists`, `file_contains`, and `artifacts` are workspace-relative. For files outside the workspace, use a `command` check, declare `side_effects`, and leave `artifacts` empty.
- `side_effects` + `undo`: writes outside the workspace. See `references/dag-schema.md`, "Side effects, undo, and residue".
- `service` + `ready`: long-running processes. See "Background services and http/port checks".
- `interactive`, and the command check `stdin`: steps that prompt for input. See "Interactive steps and command-check stdin".
- Root `requires`: tools the tutorial needs. See "Preflight: requires and doctor".

Compact node examples (other required fields omitted from the first; the schema requires `id`, `title`, `summary`, `dependencies`, `action_type`, `status`, `commit_sha`, `artifacts`, `commands`, `verification`):

```json
{"id": "3-user-bin", "title": "Install into ~/.local/bin", "summary": "Copy the binary to the user bin.", "dependencies": ["2-build"], "action_type": "mutate", "status": "pending", "commit_sha": null, "artifacts": [], "commands": ["cp dist/greet ~/.local/bin/greet"], "side_effects": ["user"], "undo": ["rm -f ~/.local/bin/greet"], "verification": {"type": "command", "command": "test -x ~/.local/bin/greet", "expect_exit": 0}}
```

```json
{"id": "4-serve", "title": "Start the server", "summary": "Run the dev server in the background.", "dependencies": ["3-build"], "action_type": "execute", "status": "pending", "commit_sha": null, "artifacts": [], "commands": [], "service": {"command": "python3 -m http.server 8000", "ready": {"type": "port_listening", "port": 8000}}, "verification": {"type": "http", "url": "http://127.0.0.1:8000/", "expect_status": 200}}
```

After nodes exist, change progress fields (`status`, `commit_sha`, `approved`, `active_node_id`) only through the CLI.

## Step 3 — Status check

Run `status` at the start of each turn:

```bash
python3 "$SKILL_DIR/scripts/dag_manager.py" status
```

```text
[assisted | file | main | 3/7 (1 skip) | active: 3-smoke | next: 4-store, 5-render | residue: 1 | svc: 1]
```

The line shows mode, persistence, branch, progress, active node, and unlocked next steps. `residue: N` appears only when N > 0 unrevertable side effects are recorded. `svc: N` appears only when N > 0 services are recorded. Use `--json` for full raw state.

## Step 4 — Execution loop

Process one node per turn:

```bash
python3 "$SKILL_DIR/scripts/dag_manager.py" next
python3 "$SKILL_DIR/scripts/dag_manager.py" verify
python3 "$SKILL_DIR/scripts/dag_manager.py" complete --commit
```

- Finish the node's work before `complete`. Run declared `commands` verbatim (or exact behavioral equivalents if shell quoting or environment paths require it) before `verify`; never silently substitute a different script. `complete` runs verification and blocks the update on failure.
- `complete` makes the next node active when exactly one is ready (or prints `branch point, pick one: …` when several are ready—choose with `next <id>`). Running `next` with no argument when a pending node is already active prints that active node and exits 0.
- Use `--commit` when the node changed workspace files. It commits as `tutorial(<node-id>): <title>`. On a clean workspace, `complete --commit` completes the node without a checkpoint: `nothing to commit; completed <id> without a checkpoint` (exit 0, `commit_sha` stays null). Rollback then targets the nearest completed ancestor checkpoint.
- Complete `read-only` and `instruct` nodes without `--commit`.
- The `next:` header lists ready nodes other than the active one.
- Use `--yes` to pass `confirm` checks only after the user confirms (see Step 1 for hands-off).
- For a step that prompts for input, follow the `feed answers:` or `a person must run this step` hint that `next` prints. Command checks get no stdin unless the check sets `stdin`.
- If a check fails, fix your work first. Do not bypass checks by editing the state file.
- If the tutorial itself is wrong (a declared command is broken/outdated, it contradicts itself, or the tool changed), not your work, record the fix: `deviate [<id>] (--command N | --check) --to "<cmd>" --reason "<why the tutorial is wrong>"`, then `verify` again. Use `deviate` only for a stale tutorial. Never invent behavior to make a stale check pass.
- Run `export` at the end of the session. It writes `.tutorial-export/deviations.md` and, for a local source with at least one deviation, `tutorial.corrected.md`.

### Services

A node that starts a long-running process (a dev server, a watcher) declares a `service` block instead of a foreground command:

```bash
python3 "$SKILL_DIR/scripts/dag_manager.py" service start    # active node, or pass <id>
python3 "$SKILL_DIR/scripts/dag_manager.py" service stop <id>
python3 "$SKILL_DIR/scripts/dag_manager.py" teardown         # stop all services
```

- `service start` runs the command detached, waits for `ready`, and logs to `.tutorial-logs/<id>.log` (already git-excluded by `init`). If `ready` never passes, it prints the log tail and exits 1.
- Complete the node after `service start`. Its `verification` confirms the service works.
- Run `teardown` when the session ends. `jump --reset-descendants` also stops the services it rewinds.
- Services are POSIX only.

## Step 5 — Navigation and rewinds

**Jump ahead.** `jump <target>` blocks jumps over incomplete prerequisites:

```bash
python3 "$SKILL_DIR/scripts/dag_manager.py" status
# Complete each prerequisite node in order:
python3 "$SKILL_DIR/scripts/dag_manager.py" complete <ancestor> --commit
# Or mark prerequisites satisfied:
python3 "$SKILL_DIR/scripts/dag_manager.py" jump <target> --skip-ancestors
```

Use `--skip-ancestors` only if the prerequisites are already satisfied in the environment.

**Rewind.** Determine the intended action before running changes:

| Goal | Command | Result |
|---|---|---|
| Review earlier step | `jump <target>` | Moves active pointer. Working files remain unchanged. |
| Redo step from clean state | `jump <target> --reset-descendants --git-rollback hard` | Runs `git reset --hard` to target checkpoint. Resets downstream nodes to `pending`. |
| Branch to test an alternative | `jump <target> --reset-descendants --git-rollback branch [--new-branch tutorial/alt]` | Creates a new branch at target checkpoint. Keeps current work intact. |
| Redo and revert non-repo side effects | `jump <target> --reset-descendants --git-rollback hard --force --undo` | Resets as above, then runs each rewound node's `undo` commands. Failures and unrevertable nodes become `residue`. |

- Rollback targets the nearest completed parent commit.
- `--git-rollback hard` deletes uncommitted workspace changes and requires `--force`.
- Confirm with the user before rolling back completed `mutate` or `execute` nodes, and before `--undo` on `system` scope.
- `--undo` requires `--reset-descendants`, otherwise exit 2. Without `--undo`, rewound nodes with non-repo `side_effects` are listed and recorded as `residue`.
- `residue clear [<node_id> | --all]` removes residue entries once handled.
- Risk tiers in `status` are `low` (workspace-only), `elev` (`elevated`, non-repo `side_effects` such as `user`/`system`/`remote`), and `HIGH` (`high`, matches high-risk commands or declared `risk: "high"`). Only `high` gates `hands-off` mode (`low` and `elev` are never blocked). `next` prints `risk: high (<labels>)` for a high-risk node, and in `hands-off` mode refuses it until approved.
- Run `approve <node_id>` only after the user says yes to that node's listed risk labels. Never approve on your own.
- In `hands-off` mode, `complete` also refuses an unapproved high-risk node, and a node with deviations until `approve <node_id>` covers them. Ask the user first; the approval covers the recorded deviations.

## In-context persistence

When running without a local state file, hold state in conversation memory. Pass it via a temporary file:

```bash
python3 "$SKILL_DIR/scripts/dag_manager.py" --file /tmp/<slug>.dag.json init --title … --source …
python3 "$SKILL_DIR/scripts/dag_manager.py" --file /tmp/<slug>.dag.json status --json
```

Use `file` mode if the workspace supports local file storage.

## Scaffolding a tutorial skill

Turn a finished DAG into its own skill:

```bash
python3 "$SKILL_DIR/scripts/dag_manager.py" scaffold <name> --from <dag.json> [--dest DIR] [--force]
```

- Writes `<dest>/<name>/` (default `.agents/skills`) with `SKILL.md` (rendered from `templates/SKILL.md.tmpl`), `dag.json` (progress stripped), `scripts/`, and `references/`.
- The generated skill starts with `init --from "$SKILL_DIR/dag.json"`. Run `scaffold` from this source skill; it needs `templates/`.
- An existing target exits 1 unless `--force` replaces it. Exit 2 on a bad name.
- Starting a session from a prebuilt DAG without scaffolding: `init --from <dag.json>` (`--title` and `--source` default from the file; nodes, `requires`, and `mode` are copied; progress is stripped).

## Safety rules

- Do not execute node `commands` directly in `guided` mode.
- The CLI never runs node `commands`. It runs only a declared `service` command (`service start`) and declared `undo` commands (`jump --undo`).
- Run `teardown` before ending a session that started services.
- In `hands-off` mode, run `approve <node_id>` for a high-risk node only after a person says yes.
- Do not complete a node without running its verification check.
- Never run `git reset --hard` or `git clean` without user approval.
- Do not commit `.tutorial-dag.json`.
- Do not invent nodes omitted from the source document.
- Do not edit `.tutorial-dag.json` to record a deviation. Use `deviate`.

## Reference files

| File | Purpose |
|---|---|
| `references/dag-schema.md` | Schema specification and state transition rules |
| `references/dag-schema.json` | JSON Schema Draft 2020-12 |
| `scripts/dag_manager.py` | CLI tool |
| `examples/` | Sample tutorial and reference DAG |
| `README.md` | CLI reference and troubleshooting guide |
