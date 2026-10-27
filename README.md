# interactive-tutorial

An [Agent Skills](https://agentskills.io) package that turns a coding
tutorial into an interactive graph. The agent breaks the document into
verifiable nodes, runs one node per turn, checks each step, and commits
every file change to Git. You can pause, resume, jump ahead, or rewind
to an earlier checkpoint.

State lives in `.tutorial-dag.json` in the target workspace. The tool
adds this file to `.git/info/exclude`. Git will not track it, and
`git reset` will not wipe your progress record.

- **Autonomy**: `guided` (user runs commands), `assisted` (agent proposes, user approves), `hands-off` (agent runs unattended).
- **Persistence**: `file` (writes `.tutorial-dag.json`) or `in-context` (keeps state in the conversation).
- **Navigation**: jump ahead by completing prerequisites or skipping them; rewind by moving the active pointer, branching, or running a hard reset.
- **Runtime**: Python ≥ 3.9 standard library and Git ≥ 2.25. No dependencies.

## Repository layout

This repository is a container for the skill. Source files live in `interactive-tutorial/`. The `.agents/skills/interactive-tutorial/` directory contains relative symlinks to them. The root `Makefile` forwards all commands to the skill directory.

```text
.
├── Makefile                                  forwards to the skill
├── README.md                                 you are here
├── LICENSE                                   MIT, Andrew Gene Brown 2026
├── TODO.md                                   development plan
├── .github/workflows/ci.yml                  CI workflow (lint, examples, tests)
├── planning/INITIAL_PROMPT.md                original specification
├── interactive-tutorial/                     skill source directory
│   ├── SKILL.md                              agent instructions
│   ├── README.md                             CLI reference and troubleshooting
│   ├── Makefile                              lint, test, validate-examples, install
│   ├── references/dag-schema.md              schema and state transition rules
│   ├── references/dag-schema.json            JSON Schema (Draft 2020-12)
│   ├── scripts/dag_manager.py                CLI tool (Python stdlib)
│   ├── examples/sample-tutorial.md           sample tutorial with two branches
│   ├── examples/expected-dag.json            DAG decomposition of sample tutorial
│   └── tests/test_dag_manager.py             CLI test suite
└── .agents/skills/interactive-tutorial/      symlinks to interactive-tutorial/*
```

## Install

```bash
make install                       # symlink to ~/.agents/skills/interactive-tutorial
make install LOCAL=1               # symlink to ./.agents/skills (already present)
make install SKILLS_DIR=/tmp/x     # symlink to custom path
make uninstall                     # remove symlink
```

The install target creates a symlink pointing to the skill directory. It refuses to overwrite regular files or directories.

## Quick start

```bash
mkdir -p /tmp/todo && cd /tmp/todo && git init -b main && git commit -q --allow-empty -m base
SKILL=$HOME/.agents/skills/interactive-tutorial/scripts/dag_manager.py

python3 "$SKILL" init --title "Todo CLI" --source tutorial.md --git-branch tutorial/todo
python3 "$SKILL" status
[assisted | file | tutorial/todo | 3/7 (1 skip) | active: 3-smoke | next: 4-store, 5-render]

python3 "$SKILL" next                      # stops if multiple branches are ready
python3 "$SKILL" complete --commit         # checks step and commits changes
python3 "$SKILL" jump 7-converge           # stops if prerequisite nodes are incomplete
python3 "$SKILL" jump 3-smoke --reset-descendants --git-rollback hard --force
```

Test the tool on the bundled example:
[`examples/sample-tutorial.md`](interactive-tutorial/examples/sample-tutorial.md).
See the sample DAG in
[`examples/expected-dag.json`](interactive-tutorial/examples/expected-dag.json).
Read the full command reference in
[the skill README](interactive-tutorial/README.md).

## Using as a template

Copy the skill directory to build a tutorial-specific skill:

```bash
cp -r interactive-tutorial .agents/skills/build-a-vector-store
```

Update step 2 in `SKILL.md` with your tutorial's nodes, files, and verification commands. The schema, CLI, tests, and Makefile work without modifications.

## Testing

```bash
make lint               # byte-compile scripts and tests
make validate-examples  # validate and print sample DAG
make test               # run test suite in temporary git repos
make all                # run lint, validate-examples, and test
```

CI tests on Python 3.9 and 3.12. Tests isolate Git by pointing `HOME` and `GIT_CONFIG_GLOBAL` to a temporary directory.

## Constraints

- The CLI only runs verification checks. It does not run the commands within a node. The agent or user runs the commands directly.
- The `--commit` flag uses `git add -A`. It commits all current workspace changes. Complete and commit each node before editing files for the next step.
- `jump --git-rollback hard` resets the working tree and requires `--force`. Use `--git-rollback branch` to preserve uncommitted work.
- Resetting Git history and resetting DAG node status are separate operations.

## License

MIT. See [LICENSE](LICENSE). Copyright 2026 Andrew Gene Brown.
