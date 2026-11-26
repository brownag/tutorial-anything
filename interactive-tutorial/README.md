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
| `init [--title T --source S] [--from DAG.JSON] [--mode M] [--persistence P] [--git-branch B] [--force]` | Create the state file, add exclude entry, record branch, HEAD commit, and dirty status. `--from` copies nodes, `requires`, and `mode` from a DAG file and strips progress; `--title`/`--source` default from the file | 1 if file exists without `--force`, or if the `--from` source is missing or invalid |
| `scaffold <name> --from DAG.JSON [--dest DIR] [--force]` | Write a new skill to `<dest>/<name>/` (default `.agents/skills`): `SKILL.md` from `templates/SKILL.md.tmpl`, `dag.json` with progress stripped, `scripts/`, `references/`. The generated skill starts with `init --from` | 0 on success; 1 if the target exists without `--force`, the DAG is invalid, or the template is missing; 2 on a bad name |
| `validate` | Check schema, duplicate IDs, missing dependencies, and cycles | 1 on any error |
| `status [--json]` | Print status header and topologically sorted node table; `--json` prints raw state | 1 if state is invalid |
| `next [<id>] [--skip-doctor]` | Activate the next ready node, or pick a specific ready node. When the DAG declares `requires`, the first `next` runs `doctor` first; `--skip-doctor` bypasses that check | 1 if multiple nodes are ready, the current node is incomplete, or the preflight failed |
| `doctor [--json]` | Check the tools and OS declared in `requires`, one row per check, and record the result as `preflight` | 1 if any check fails |
| `verify [<id>] [--yes]` | Run the verification check for a node | 1 on check failure |
| `complete [<id>] [--commit] [--message M] [--no-verify] [--yes]` | Verify, commit changes, mark completed, update ready states | 1 on check failure or missing dependencies (a clean workspace with `--commit` completes without a checkpoint) |
| `jump <id> [--skip-ancestors] [--reset-descendants] [--git-rollback none\|branch\|hard] [--new-branch NAME] [--force] [--undo]` | Move active pointer, fast-forward, or rollback workspace. `--undo` (requires `--reset-descendants`, else exit 2) runs the `undo` commands of rewound nodes with non-repo side effects; failures and unrevertable nodes are recorded as residue | 1 if prerequisites are incomplete, rollback is unsafe, or an undo fails |
| `residue clear [<id> \| --all]` | Remove residue entries left by `jump` once handled | 0 |
| `approve <id>` | Record a person's approval of a node's current risk tier and its deviations; clears the `hands-off` gate for that node while its tier does not rise and no new deviation is recorded | 1 if the state is invalid |
| `deviate [<id>] (--command N \| --check) --to CMD --reason TEXT` | Replace a stale check or command (default: active node) and record the original, replacement, and reason. Use only when the tutorial is wrong | 0 on success; 1 if the node is completed (`jump <id> --reset-descendants` first); 2 on bad target, same or empty text, or a non-command check |
| `export [--out DIR]` | Write `deviations.md` and, for a readable local `source_doc` with at least one deviation, `tutorial.corrected.md` to `<workspace>/.tutorial-export` (git-excluded) | 0 |
| `draft <path> [--write]` | Print a skeleton DAG (JSON `{"title", "nodes"}`) from a Markdown tutorial: one node per H2/H3 heading (or numbered item), linear chain, `DRAFT:` placeholder checks. `--write` fills an empty `nodes` array, then validates | 1 with `--write` if `nodes` is not empty (`nodes already present`) |
| `service start [<id>]` | Start the node's `service` command detached, log to `.tutorial-logs/<id>.log`, and wait for its `ready` check (default: active node) | 1 if `ready` never passes or the process exits; 2 on non-POSIX systems |
| `service stop [<id>]` | Stop a recorded service: `SIGTERM` to its process group, then `SIGKILL` after 5 s. A dead process is not an error | 0 |
| `teardown` | Stop every recorded service | 0 |

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
| High-risk nodes and deviations | Not gated | Not gated | `next` refuses high-risk nodes; `complete` refuses deviated nodes; both until `approve <id>` |
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
| `nothing to commit; completed <id> without a checkpoint` | The workspace was clean, so `complete --commit` completed the node with `commit_sha` null (exit 0). Expected for nodes that change no files. |
| `--git-rollback hard` refused | Add `--reset-descendants` and `--force` if the working tree has uncommitted changes. |
| No ancestor checkpoint exists | Git repository had no commits at `init`. Make a baseline commit of the existing files, then re-run `init` (or use `--git-rollback none`). |
| Verification fails | Check rule format: `file_contains` is a literal string unless `"regex": true`. Run `verify` to inspect output. |
| `confirm` check fails with no output | Needs `--yes` flag to confirm human approval. |
| A command check hangs or reads nothing | Command checks no longer inherit the CLI's stdin. Their stdin is `/dev/null` unless the check sets `"stdin"`, so prompts see EOF. Add `"stdin": "answer\n"` to feed input, and `"timeout"` (seconds) to bound the run. |
| `node '<id>' still has a draft check` warning | A `confirm` check still has a `DRAFT:` prompt from `draft`. Replace it with a real verification. |
| `command looks interactive` warning | The command may wait for input. Add `"interactive": {"answers": [...]}`, or `"human": true` for a step a person must run, or use the non-interactive flag (for example `npm init -y`). |
| `services require a POSIX system` | `service` commands are POSIX only. Run the service step on Linux or macOS. |
| `has deviated and hands-off mode needs a person` | A hands-off node has deviations that `approve` does not cover. After a person says yes, run `approve <id>`. |
| `is already completed. Jump back` | `deviate` refuses completed nodes. Run `jump <id> --reset-descendants` first. |
| `service start` fails with `ready check did not pass` | The printed log tail shows why. Raise `ready_timeout` if the server is slow to start. |

## Development

```bash
make lint               # compile checks
make validate-examples  # test sample DAG
make test               # run test suite
make test-corpus        # run corpus fixture tests
```

Tests run in temporary Git repositories. They isolate global Git configurations.

Corpus: `make test-corpus` runs `tests/test_corpus.py` against the fixtures in `tests/corpus/`, one directory per tutorial scenario. `make test` excludes these tests (`test_dag*.py` only), and `make all` runs both.

## License

MIT. See `LICENSE` in repository root. Copyright 2026 Andrew Gene Brown.
