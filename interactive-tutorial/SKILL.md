---
name: interactive-tutorial
description: Turn a static coding tutorial, README, workshop doc, or technical walkthrough into an interactive graph you step through one step at a time, jump ahead into, and rewind out of. Use when the user asks to follow, work through, run, or execute a tutorial or walkthrough, wants to resume a half-finished tutorial, asks to convert documentation into a checkable step-by-step plan, or mentions a tutorial DAG, nodes, fast-forward, jump ahead, or rewind. Decomposes the document into verifiable nodes with real dependencies, tracks progress in .tutorial-dag.json, gates every step behind its own verification command, and checkpoints each file-changing step as a git commit so steps can be replayed or reverted. Supports guided, assisted, and hands-off autonomy plus file and in-context state persistence.
license: MIT
author: Andrew Gene Brown
version: 1.0.0
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
2. Verify the workspace is a Git repository:

   ```bash
   git rev-parse --is-inside-work-tree || echo "not a repo"
   ```

   Run `git init` if no repository exists. Checkpoints and rewinds require Git. Initialize a branch for tutorial work:

   ```bash
   python3 "$SKILL_DIR/scripts/dag_manager.py" init \
     --title "<tutorial title>" --source <path-or-url> \
     --mode assisted --git-branch tutorial/<slug>
   ```

   `init` appends `.tutorial-dag.json` to `.git/info/exclude`. Git will not track this file, protecting your progress records across checkouts and resets.
3. Select autonomy and persistence modes:

   | Mode | Command execution | `confirm` check handler |
   |---|---|---|
   | `guided` | User runs commands | User |
   | `assisted` (default) | Agent proposes, user approves | User |
   | `hands-off` | Agent runs unattended | Agent (if pre-approved) |

   | Persistence | State location | Best for |
   |---|---|---|
   | `file` (default) | `.tutorial-dag.json` | Standard environments |
   | `in-context` | Conversation memory | Ephemeral or read-only filesystems |

## Step 2 — DAG decomposition

Read the tutorial before writing nodes. Add the `nodes` array to `.tutorial-dag.json` (see `references/dag-schema.md`) and validate:

```bash
python3 "$SKILL_DIR/scripts/dag_manager.py" validate
```

Rules:
- Keep nodes atomic. Ensure each node has a clear verification check.
- Use explicit dependencies. Model optional steps and parallel paths as branches sharing a parent node.
- Set `action_type`: `read-only` (review), `instruct` (manual external task), `mutate` (file changes), or `execute` (build or test command).
- Provide a `verification` check for every node: `command`, `file_exists`, `file_contains`, or `confirm`. Use `confirm` for external steps.
- Populate `artifacts` for `mutate` nodes and `commands` for step instructions. The CLI does not run node commands directly.

Keep node counts close to the document structure. See `examples/expected-dag.json` for a sample diamond graph.

## Step 3 — Status check

Run `status` at the start of each turn:

```bash
python3 "$SKILL_DIR/scripts/dag_manager.py" status
```

```text
[assisted | file | main | 3/7 (1 skip) | active: 3-smoke | next: 4-store, 5-render]
```

This line displays current mode, persistence type, branch, progress count, active node, and unlocked next steps. Use `--json` when you need full raw state.

## Step 4 — Execution loop

Process one node per turn:

```bash
python3 "$SKILL_DIR/scripts/dag_manager.py" next
python3 "$SKILL_DIR/scripts/dag_manager.py" verify
python3 "$SKILL_DIR/scripts/dag_manager.py" complete --commit
```

- Complete work for the node before running `complete`. The command runs verification and blocks updates on failure.
- `--commit` stages workspace changes and commits as `tutorial(<node-id>): <title>`. Use this for `mutate` and stateful `execute` nodes. Write files only for the current node.
- Complete `read-only` and `instruct` nodes without `--commit`.
- Use `--yes` to pass `confirm` checks after user confirmation.
- If multiple paths are ready, `next` stops and lists them. Ask the user which path to take.
- If a check fails, fix the code or update the test. Do not bypass checks by editing the state file directly.

## Step 5 — Navigation and rewinds

**Jump ahead.** `jump <target>` blocks jumps over incomplete prerequisites:

```bash
python3 "$SKILL_DIR/scripts/dag_manager.py" status
# Complete each prerequisite node in order:
python3 "$SKILL_DIR/scripts/dag_manager.py" complete <ancestor> --commit
# Or mark prerequisites satisfied:
python3 "$SKILL_DIR/scripts/dag_manager.py" jump <target> --skip-ancestors
```

Only use `--skip-ancestors` if prerequisite steps are already satisfied in the environment.

**Rewind.** Determine the intended action before running changes:

| Goal | Command | Result |
|---|---|---|
| Review earlier step | `jump <target>` | Moves active pointer. Working files remain unchanged. |
| Redo step from clean state | `jump <target> --reset-descendants --git-rollback hard` | Runs `git reset --hard` to target checkpoint. Resets downstream nodes to `pending`. |
| Branch to test an alternative | `jump <target> --reset-descendants --git-rollback branch [--new-branch tutorial/alt]` | Creates a new branch at target checkpoint. Keeps current work intact. |

- Rollback targets the nearest completed parent commit.
- `--git-rollback hard` deletes uncommitted workspace changes and requires `--force`.
- Confirm with the user before rolling back completed `mutate` or `execute` nodes.

## In-context persistence

When running without a local state file, hold state in conversation memory. Pass it via a temporary file:

```bash
python3 "$SKILL_DIR/scripts/dag_manager.py" --file /tmp/<slug>.dag.json init --title … --source …
python3 "$SKILL_DIR/scripts/dag_manager.py" --file /tmp/<slug>.dag.json status --json
```

Use `file` mode if the workspace supports local file storage.

## Creating a custom tutorial skill

Copy `interactive-tutorial/` to `.agents/skills/<your-tutorial>/`. Update Step 2 in `SKILL.md` with your tutorial's nodes, files, and verification commands. The CLI, schema, and tests remain unchanged.

## Safety rules

- Do not execute node `commands` directly in `guided` mode.
- Do not complete a node without running its verification check.
- Never run `git reset --hard` or `git clean` without user approval.
- Do not commit `.tutorial-dag.json`.
- Do not invent nodes omitted from the source document.

## Reference files

| File | Purpose |
|---|---|
| `references/dag-schema.md` | Schema specification and state transition rules |
| `references/dag-schema.json` | JSON Schema Draft 2020-12 |
| `scripts/dag_manager.py` | CLI tool |
| `examples/` | Sample tutorial and reference DAG |
| `README.md` | CLI reference and troubleshooting guide |
