# interactive-tutorial

Run any coding tutorial as a step-by-step DAG. Break a tutorial into verifiable nodes, check each step, and commit each file change to Git.

State lives in `.tutorial-dag.json` in the target workspace. The tool adds this file to `.git/info/exclude`. Git will not track it, and `git reset` will not wipe your progress record.

- Spec and workflow: [`SKILL.md`](SKILL.md)
- Schema and transitions: [`references/dag-schema.md`](references/dag-schema.md)
- Requirements: Python ≥ 3.9 and Git ≥ 2.25. No dependencies.

## Install

```bash
make install                      # symlink to ~/.agents/skills/interactive-tutorial
make install LOCAL=1              # symlink to ./.agents/skills
make install SKILLS_DIR=/tmp/x    # symlink to custom path
make uninstall                    # remove symlink
```

Inside this repository, the skill is already available in `.agents/skills/interactive-tutorial/`. For Claude Code or Codex, copy or symlink `interactive-tutorial/` into their skill directory.

## Quick start

```bash
mkdir -p /tmp/todo && cd /tmp/todo && git init -b main && git commit -q --allow-empty -m base
SKILL_DIR=~/.agents/skills/interactive-tutorial

python3 "$SKILL_DIR/scripts/dag_manager.py" init \
  --title "Todo CLI" --source "$SKILL_DIR/examples/sample-tutorial.md" \
  --mode assisted --git-branch tutorial/todo

# Add nodes to .tutorial-dag.json (see examples/expected-dag.json for sample structure)
python3 "$SKILL_DIR/scripts/dag_manager.py" validate
python3 "$SKILL_DIR/scripts/dag_manager.py" status
python3 "$SKILL_DIR/scripts/dag_manager.py" next
```

Run each step, test with `verify`, and save progress with `complete --commit`. See [`examples/sample-tutorial.md`](examples/sample-tutorial.md) and [`examples/expected-dag.json`](examples/expected-dag.json).

## CLI reference

```bash
python3 scripts/dag_manager.py [--file PATH] [--cwd DIR] <command> ...
```

Place global flags before the command. `--file` specifies the state file (default: `./.tutorial-dag.json`). `--cwd` sets the workspace root for verification checks (default: current directory).

| Command | Action | Exit code |
|---|---|---|
| `init --title T --source S [--mode M] [--persistence P] [--git-branch B] [--force]` | Create the state file, add exclude entry, record branch, HEAD commit, and dirty status | 1 if file exists without `--force` |
| `validate` | Check schema, duplicate IDs, missing dependencies, and cycles | 1 on any error |
| `status [--json]` | Print status header and topologically sorted node table; `--json` prints raw state | 1 if state is invalid |
| `next [<id>]` | Activate the next ready node, or pick a specific ready node | 1 if multiple nodes are ready or current node is incomplete |
| `verify [<id>] [--yes]` | Run the verification check for a node | 1 on check failure |
| `complete [<id>] [--commit] [--message M] [--no-verify] [--yes]` | Verify, commit changes, mark completed, update ready states | 1 on check failure, missing dependencies, or empty commit |
| `jump <id> [--skip-ancestors] [--reset-descendants] [--git-rollback none\|branch\|hard] [--new-branch NAME] [--force]` | Move active pointer, fast-forward, or rollback workspace | 1 if prerequisites are incomplete or rollback is unsafe |

`verify` and `complete` target the active node if you omit the node ID. Exit codes: `0` success, `1` check or command failure, `2` argument error.

Node states:
- `ACTIVE`: current node.
- `READY`: pending node with all dependencies completed or skipped.
- `PENDING`: pending node with incomplete dependencies.
- `COMPLETED`: step finished.
- `SKIPPED`: step bypassed.

## Autonomy modes

| | `guided` | `assisted` (default) | `hands-off` |
|---|---|---|---|
| Command runner | User | Agent after user approval | Agent |
| `confirm` checks | User | User | Agent (if pre-approved) |
| Git commit | Agent | Agent | Agent |
| Best used for | Missing toolchains, learning | Standard local development | Long automated tutorials |

Persistence modes:
- `file` (default): stores state in `.tutorial-dag.json`.
- `in-context`: keeps state in the conversation, passes it via `--file`, and reads it back via `status --json`.

## Troubleshooting

| Problem | Cause and fix |
|---|---|
| `no state file at ./.tutorial-dag.json` | Run command from workspace root, or pass `--file`. |
| Git tracks `.tutorial-dag.json` | Re-run `init` or append `.tutorial-dag.json` to `.git/info/exclude`. |
| `branch point: 2 nodes are ready` | Multiple paths open. Select one with `next <id>`. |
| `refusing to jump over unmet ancestors` | Complete earlier nodes, or pass `--skip-ancestors` if already satisfied. |
| `nothing to commit: the workspace is clean` | The node made no file changes, or files were already committed. Remove `--commit` for nodes that do not touch files. |
| `--git-rollback hard` refused | Add `--reset-descendants` and `--force` if the working tree has uncommitted changes. |
| No ancestor checkpoint exists | Git repository had no commits at `init`. Commit current state first, or use `--git-rollback none`. |
| Verification fails | Check rule format: `file_contains` is a literal string unless `"regex": true`. Run `verify` to inspect output. |
| `confirm` check fails with no output | Needs `--yes` flag to confirm human approval. |

## Development

```bash
make lint               # compile checks
make validate-examples  # test sample DAG
make test               # run test suite
```

Tests run in temporary Git repositories. They isolate global Git configurations.

## License

MIT. See `LICENSE` in repository root. Copyright 2026 Andrew Gene Brown.
