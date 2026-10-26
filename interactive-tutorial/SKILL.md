---
name: interactive-tutorial
description: Turn a static coding tutorial, README, workshop doc, or technical walkthrough into an interactive graph you step through one step at a time, jump ahead into, and rewind out of. Use when the user asks to follow, work through, run, or execute a tutorial or walkthrough, wants to resume a half-finished tutorial, asks to convert documentation into a checkable step-by-step plan, or mentions a tutorial DAG, nodes, fast-forward, jump ahead, or rewind. Decomposes the document into verifiable nodes with real dependencies, tracks progress in .tutorial-dag.json, gates every step behind its own verification command, and checkpoints each file-changing step as a git commit so steps can be replayed or reverted. Supports guided, assisted, and hands-off autonomy plus file and in-context state persistence.
license: MIT
author: Andrew Gene Brown
version: 1.0.0
compatibility: python>=3.9, git>=2.25
---

# Interactive Tutorial Runner

A tutorial is not a list. It is a graph: a core path, optional features that
branch off it, and steps that only make sense once a dependency is done. Reading
it linearly works until step 14 depends on the optional section you skipped at
step 6. This skill keeps the graph shape, gates every node behind its own
verification, and commits every node that touches files — so "go back to before
I added persistence" is a command, not an archaeology trip.

Run the CLI from the workspace the tutorial builds in. `$SKILL_DIR` below is this
skill's directory; set it once per session:

```bash
SKILL_DIR="<absolute path of this skill directory>"
python3 "$SKILL_DIR/scripts/dag_manager.py" --help
```

Every command reads `./.tutorial-dag.json` in the current workspace unless you
pass `--file`. Verification paths and commands resolve against `--cwd`, which
defaults to the current directory.

## Step 1 — Session initialization and mode selection

1. **Resume or start.** If `.tutorial-dag.json` exists, read it and say what you
   found (`status` prints the header in one line), then continue. Never overwrite
   it; `init --force` discards recorded progress and needs the user's blessing.
2. **Check the workspace is Git.**

   ```bash
   git rev-parse --is-inside-work-tree || echo "not a repo"
   ```

   If it is not a repo, offer `git init` — without it, checkpoints and rewind do
   not exist and the tutorial degrades to a to-do list. If it is, propose a
   dedicated branch so tutorial commits stay out of the user's real history:

   ```bash
   python3 "$SKILL_DIR/scripts/dag_manager.py" init \
     --title "<tutorial title>" --source <path-or-url> \
     --mode assisted --git-branch tutorial/<slug>
   ```

   `init` also appends `.tutorial-dag.json` to `.git/info/exclude`, so state
   writes never pollute a tutorial commit and `git checkout` / `git reset` can
   never overwrite progress. It is idempotent and shared across work trees.
3. **Pick modes, then say them out loud.**

   | Mode | Who runs a step | Who confirms a `confirm` gate |
   |---|---|---|
   | `guided` | User runs the commands; agent reads output and interprets | Always the human |
   | `assisted` (default) | Agent proposes, user approves, agent runs | Human, unless they pre-approve |
   | `hands-off` | Agent runs nodes and verifications unattended | Agent may pass `--yes` only if the user said to treat confirmations as pre-approved |

   | Persistence | State lives in | Use when |
   |---|---|---|
   | `file` (default) | `.tutorial-dag.json` | Any workspace with a filesystem |
   | `in-context` | The conversation | Read-only or ephemeral workspaces; see the recipe below |

   A tutorial with 20+ nodes or more than one session's worth of work is worth a
   `file` document even in `in-context` sessions; say so and let the user choose.

## Step 2 — DAG decomposition

Read the whole document before writing any node. Then write the `nodes` array
into `.tutorial-dag.json` (a JSON edit — the shape is in
`references/dag-schema.md`, one screen) and validate it:

```bash
python3 "$SKILL_DIR/scripts/dag_manager.py" validate
```

Decomposition rules:

- **Atomic and verifiable.** One user-visible outcome per node. If you cannot
  write a check for it, the node is two nodes or it is prose.
- **Real dependencies.** Model parallel tracks, optional sections, and
  alternatives as *branches* — two nodes sharing one parent — never as extra
  linear steps that falsely imply ordering. A tutorial with no branches is
  suspicious: reread it for the "optionally, you can also…" paragraph.
- **Classify `action_type`** as `read-only` (concepts), `instruct` (manual
  external step: sign up, click, restart), `mutate` (create/edit/append files),
  or `execute` (build/test command).
- **Every node gets a `verification`**, one of `command` (exit code and optional
  stdout regex), `file_exists`, `file_contains`, or `confirm`. External steps get
  `confirm` — do not invent a check that cannot fail.
- Fill `artifacts` for every `mutate` node and `commands` for the shell lines the
  node expects, in order. The CLI never runs `commands` itself; you do, per the
  mode you are in.

Keep node count near the document's own structure: a 7-section tutorial is about
7 nodes, not 30. Copy `examples/expected-dag.json` as a worked example of a
diamond graph.

## Step 3 — Status on every turn

Start every turn with the header line, then act:

```bash
python3 "$SKILL_DIR/scripts/dag_manager.py" status
```

```text
[assisted | file | main | 3/7 (1 skip) | active: 3-smoke | next: 4-store, 5-render]
```

Mode, persistence, branch, progress, current node, and what the graph just
unlocked, in one line. Use it verbatim; the user needs the branch and the count
to feel safe. Use `status` (graph table) when the user asks where things stand,
and `--json` when you need to reason over the whole document.

## Step 4 — Incremental execution loop

One node per turn unless the user asked you to fast-forward.

```bash
python3 "$SKILL_DIR/scripts/dag_manager.py" next          # refuse to guess at a branch point
python3 "$SKILL_DIR/scripts/dag_manager.py" verify        # run the gate, change nothing
python3 "$SKILL_DIR/scripts/dag_manager.py" complete --commit
```

- Do the node's work first, then `complete`, which runs the gate and refuses to
  record progress when it fails. `--no-verify` exists for the rare
  "the check is wrong, I checked by hand" case; say when you use it.
- `--commit` stages everything in the workspace and commits `tutorial(<node-id>): <title>`,
  then stores the sha on the node. Use it for every `mutate` node and for stateful
  `execute` nodes; it refuses a clean tree, so checkpoints are never empty. Write
  only the current node's files: if the next node's work is already on disk it gets
  swept into this commit, and the next `--commit` then finds nothing to do.
- `read-only` and `instruct` nodes complete without `--commit`.
- Confirm gates need `--yes`, which means a human said yes in this turn.
- When the graph splits, `next` exits non-zero and lists the candidates. Ask the
  user which branch; do not pick silently.
- If a node cannot pass its gate, stop and fix the work or the gate. Never mark a
  node complete to move on, and never edit `status` by hand to skip a failure.

## Step 5 — Non-linear navigation

**Jump ahead.** `jump <target>` refuses to walk over unfinished ancestors and
lists them. Then offer both options, in this order:

```bash
python3 "$SKILL_DIR/scripts/dag_manager.py" status        # read the unmet ancestors
# (a) materialize them: complete each ancestor, in topological order, one per turn
python3 "$SKILL_DIR/scripts/dag_manager.py" complete <ancestor> --commit
# (b) or declare them satisfied by other work
python3 "$SKILL_DIR/scripts/dag_manager.py" jump <target> --skip-ancestors
```

Option (b) is honest only when the prerequisites really exist already — the user
installed the dependency themselves, or the workspace already has the file. If
you did not check, offer (a).

**Rewind.** Decide what the user means before touching anything:

| Intent | Command | Effect |
|---|---|---|
| Re-read / rethink a step | `jump <target>` | Pointer only. Files and history untouched. |
| Redo a step from clean files | `jump <target> --reset-descendants --git-rollback hard` | `git reset --hard` to the nearest ancestor checkpoint; downstream nodes back to `pending` |
| Try an alternative branch | `jump <target> --reset-descendants --git-rollback branch [--new-branch tutorial/alt]` | New branch at that checkpoint; current state survives |

- The rollback sha is the nearest completed ancestor's checkpoint, falling back
  to the sha recorded at `init`. With `--reset-descendants` and a real rollback,
  the target itself reopens too, because its own files were just reverted.
- `--git-rollback hard` destroys uncommitted work: it requires `--force`, and you
  show `git status --porcelain` and get explicit consent before using it.
  Prefer `branch` when the user might want the current state again.
- Warn before rolling back over completed `mutate`/`execute` descendants — that
  is deleting work, not rewinding a pointer.

## In-context persistence

When the user wants no state file, keep the same document in the conversation and
materialize it per command. Re-read it with `status --json` after every mutation
instead of trusting memory.

```bash
python3 "$SKILL_DIR/scripts/dag_manager.py" --file /tmp/<slug>.dag.json init --title … --source …
python3 "$SKILL_DIR/scripts/dag_manager.py" --file /tmp/<slug>.dag.json status --json   # re-read
```

The `file` variant becomes authoritative the moment the user opens an editor.

## Forking this skill as a tutorial-specific skill

Copy `interactive-tutorial/` to
`.agents/skills/<your-tutorial>/` in a repo and rewrite `SKILL.md`'s Step 2 with
that tutorial's node list, ids, and verification commands. (Inside this
repository, copy the real directory at the repo root, not the symlink tree under
`.agents/skills/`.) The schema, CLI, and Makefile carry over unchanged;
`make install LOCAL=1` links it for the repo.

## Guardrails

- Never run node `commands` in `guided` mode; the user runs them.
- Never complete a node whose gate you did not run in this session.
- Never `git reset --hard`, `git clean`, or `git push` without showing the cost.
- Never commit the state file: it is excluded on purpose, and a committed state
  file makes `git reset` rewind your progress record along with the code.
- Never invent nodes the document does not support; ask instead.

## Files

| Path | Purpose |
|---|---|
| `references/dag-schema.md` | Field contract, derived states, transition rules (read this first) |
| `references/dag-schema.json` | JSON Schema Draft 2020-12 for `.tutorial-dag.json` |
| `scripts/dag_manager.py` | The CLI: `init` `validate` `status` `next` `verify` `complete` `jump` |
| `examples/` | `sample-tutorial.md` to practise on, `expected-dag.json` as the worked answer |
| `README.md` | Install, CLI reference, troubleshooting |
