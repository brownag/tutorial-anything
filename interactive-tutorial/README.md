# interactive-tutorial

Run any coding tutorial as a graph you can step through, jump ahead in, and
rewind out of. The skill teaches an agent to decompose a tutorial into verifiable
nodes, gate each one behind its own check, and checkpoint every file-changing
step as a Git commit.

State lives in `.tutorial-dag.json` inside the workspace you are tutorialing, and
is excluded from Git by that repository's own `.git/info/exclude` — so it can
never be committed, and `git reset` can never rewind your progress record.

- Spec and workflow: [`SKILL.md`](SKILL.md)
- State contract: [`references/dag-schema.md`](references/dag-schema.md)
- Requirements: Python ≥ 3.9 (stdlib only) and Git ≥ 2.25. No installs, no venv.

## Install

The skill is a directory; drop it where your agent looks for skills.

```bash
make install                      # symlink into ~/.agents/skills/interactive-tutorial
make install LOCAL=1              # link into ./.agents/skills instead
make install SKILLS_DIR=/tmp/x    # anywhere (also how CI and the tests target it)
make uninstall                    # remove the symlink
```

Working *inside* this repository, no install is needed: the real skill files live
in `interactive-tutorial/` and `.agents/skills/interactive-tutorial/` contains
symlinks to them, so `.agents/skills/` remains a standard discoverable Agent
Skills location. For Claude Code or Codex, link or copy `interactive-tutorial/`
into their skills path — the layout is the portable one (`SKILL.md` +
`references/` + `scripts/`).

## Quick start

```bash
mkdir -p /tmp/todo && cd /tmp/todo && git init -b main && git commit -q --allow-empty -m base
SKILL_DIR=~/.agents/skills/interactive-tutorial     # or ./  when inside this repo

python3 "$SKILL_DIR/scripts/dag_manager.py" init \
  --title "Todo CLI" --source "$SKILL_DIR/examples/sample-tutorial.md" \
  --mode assisted --git-branch tutorial/todo

# Write your decomposition into the "nodes" array of .tutorial-dag.json, e.g.
# by copying the "nodes" array from examples/expected-dag.json, then:
python3 "$SKILL_DIR/scripts/dag_manager.py" validate
python3 "$SKILL_DIR/scripts/dag_manager.py" status
python3 "$SKILL_DIR/scripts/dag_manager.py" next
```

Then the loop: do the node, `verify`, `complete --commit`. Full worked example:
[`examples/sample-tutorial.md`](examples/sample-tutorial.md) with its answer in
[`examples/expected-dag.json`](examples/expected-dag.json).

## CLI

```bash
python3 scripts/dag_manager.py [--file PATH] [--cwd DIR] <command> ...
```

Global flags go **before** the command. `--file` selects the state document
(default `./.tutorial-dag.json`); `--cwd` pins the workspace that verification
paths and commands use (default: current directory).

| Command | What it does | Exit |
|---|---|---|
| `init --title T --source S [--mode M] [--persistence P] [--git-branch B] [--force]` | Create the state document, add the exclude entry, record the branch, HEAD sha, and dirty flag | 1 if a state file exists and `--force` is absent |
| `validate` | Structure, enums, duplicate ids, unknown dependencies, cycle detection | 1 on any problem |
| `status [--json]` | Turn header + topologically ordered node table; `--json` prints the document | 1 if the document is invalid |
| `next [<id>]` | Activate the only ready node, or a chosen ready node | 1 at a branch point or on an unfinished active node |
| `verify [<id>] [--yes]` | Run the node's gate without changing state | 1 on failure |
| `complete [<id>] [--commit] [--message M] [--no-verify] [--yes]` | Gate, optional checkpoint, mark completed, recompute readiness | 1 if the gate fails, deps are unmet, or `--commit` has nothing to commit |
| `jump <id> [--skip-ancestors] [--reset-descendants] [--git-rollback none\|branch\|hard] [--new-branch NAME] [--force]` | Move the pointer anywhere; fast-forward and workspace rollback | 1 if ancestors are unmet or a rollback is unsafe |

`verify`/`complete` act on the active node when no id is given. Exit codes: `0`
success, `1` operation or verification failure, `2` usage error.

Derived states are computed, never stored: `READY` means pending with every
dependency completed or skipped; `ACTIVE` is the pointer; anything else is
`PENDING` (blocked), `COMPLETED`, or `SKIPPED`.

## Modes

| | `guided` | `assisted` (default) | `hands-off` |
|---|---|---|---|
| Who runs the node's commands | The user | Agent after approval | Agent |
| Who passes a `confirm` gate | The user, explicitly | The user, explicitly | Agent only if pre-approved |
| Who commits | Agent, when the step changed files | Agent | Agent |
| Good for | Machine without a toolchain, teaching, high-stakes hosts | Typical local work | Long local tutorials you are watching |

Persistence: `file` (default) keeps `.tutorial-dag.json`; `in-context` keeps the
same document in the conversation and passes it with `--file` per command,
re-reading it with `status --json`. The graph, gates, and checkpoints are
identical either way.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `no state file at ./.tutorial-dag.json` | Run from the tutorial workspace, or pass `--file`. |
| `git add -A` picked up `.tutorial-dag.json` | The workspace was used before this skill existed; re-run `init` (it re-adds the exclude entry) or append `.tutorial-dag.json` to `.git/info/exclude` by hand. |
| `branch point: 2 nodes are ready` | Working as designed — pick a branch with `next <id>`. |
| `refusing to jump over unmet ancestors` | Fast-forward properly, or pass `--skip-ancestors` when the prerequisites genuinely exist already. |
| `nothing to commit: the workspace is clean` | The node changed no files, or an earlier `--commit` already swept them. Complete one node per turn and write only that node's files; drop `--commit` for nodes that change nothing. |
| `--git-rollback hard` refused | It needs `--reset-descendants` (so the DAG stops claiming reverted work is done) and `--force` if the tree is dirty. |
| Rollback says no ancestor checkpoint exists | The repo had no commits at `init` and nothing was committed since. Commit the current state, or use `--git-rollback none`. |
| Verification never passes | Re-read the gate: `file_contains` is a literal substring unless `"regex": true`. Run `verify` to see the captured output. |
| A `confirm` gate fails with no output shown | Expected: it needs `--yes`, which means a human said yes this turn. |

## Development

```bash
make lint               # compile check, no third-party tools
make validate-examples  # validate + render examples/expected-dag.json
make test               # python3 -m unittest discover -s tests -v
```

Tests create their own temporary Git repository per case with `HOME` and
`GIT_CONFIG_GLOBAL` pointed at it, so they never touch your identity or config.

## License

MIT — see the `LICENSE` file at the repository root. Copyright 2026 Andrew Gene
Brown.
