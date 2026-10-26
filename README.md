# interactive-tutorial

An [Agent Skills](https://agentskills.io) package that turns a static coding
tutorial into an interactive graph: the agent decomposes the document into
verifiable nodes, runs one node per turn, gates each node behind its own
verification command, and checkpoints every file-changing node as a Git commit.
The result is a tutorial you can leave, resume, jump ahead in, and rewind —
where "rewind to before I added persistence" is a command instead of a manual
archaeology dig.

State lives in `.tutorial-dag.json` in the workspace being tutorialled, excluded
from Git by that repository's own `.git/info/exclude`. Progress therefore cannot
be committed by accident, and `git reset` cannot rewind it.

- **Autonomy modes**: `guided` (human runs the commands), `assisted` (default:
  agent proposes, human approves), `hands-off` (agent runs unattended).
- **Persistence**: `file` (the DAG document) or `in-context` (same document kept
  in the conversation).
- **Navigation**: fast-forward through unfinished ancestors, or mark them
  `skipped`; rewind by moving the pointer, by branching at an ancestor
  checkpoint, or by `git reset --hard` to it.
- **Runtime**: Python ≥ 3.9 standard library and Git ≥ 2.25. Nothing to install.

## What is in this repository

This repo is a *container*, not the skill's build output. The skill's real files
live in `interactive-tutorial/` at the top level, and `.agents/skills/interactive-tutorial/`
holds relative symlinks to them — `.agents/skills/` is a standard Agent Skills
discovery path, so working inside this repository the skill is already live, with
no install step. The root `Makefile` only forwards to the skill's own `Makefile`.

```text
.
├── Makefile                                  forwards to the skill
├── README.md                                 you are here
├── LICENSE                                   MIT, Andrew Gene Brown 2026
├── TODO.md                                   the plan this repo was built from
├── .github/workflows/ci.yml                  lint + examples + tests, py 3.9/3.12
├── planning/INITIAL_PROMPT.md                original specification
├── interactive-tutorial/                     ← the skill source; copy this directory
│   ├── SKILL.md                              5-step workflow the agent follows
│   ├── README.md                             skill-level CLI reference + troubleshooting
│   ├── Makefile                              lint / test / validate-examples / install
│   ├── references/dag-schema.md              state contract, derived states, transitions
│   ├── references/dag-schema.json            JSON Schema (Draft 2020-12)
│   ├── scripts/dag_manager.py                the CLI (stdlib only, executable)
│   ├── examples/sample-tutorial.md           a tutorial with two optional branches
│   ├── examples/expected-dag.json            its decomposition: a diamond, not a line
│   └── tests/test_dag_manager.py             26 unittest cases in throwaway git repos
└── .agents/skills/interactive-tutorial/      symlinks to ../../../interactive-tutorial/*
```

## Install

```bash
make install                       # symlink into ~/.agents/skills/interactive-tutorial
make install LOCAL=1               # this repo's ./.agents/skills (already there: no-op)
make install SKILLS_DIR=/tmp/x     # anywhere else
make uninstall                     # remove that symlink
```

Nothing runs during install but a `ln -s`; the target must be a symlink this repo
created, or `install` refuses. `LOCAL=1` recognizes the committed symlink tree in
`.agents/skills/interactive-tutorial/` as already installed and is a no-op.

## Sixty-second tour

```bash
mkdir -p /tmp/todo && cd /tmp/todo && git init -b main && git commit -q --allow-empty -m base
SKILL=$HOME/.agents/skills/interactive-tutorial/scripts/dag_manager.py

python3 "$SKILL" init --title "Todo CLI" --source tutorial.md --git-branch tutorial/todo
python3 "$SKILL" status
[assisted | file | tutorial/todo | 3/7 (1 skip) | active: 3-smoke | next: 4-store, 5-render]

python3 "$SKILL" next                      # refuses to guess at a branch point
python3 "$SKILL" complete --commit         # gate, then: tutorial(2-scaffold): Scaffold ...
python3 "$SKILL" jump 7-converge           # refuses, listing the unmet ancestors
python3 "$SKILL" jump 3-smoke --reset-descendants --git-rollback hard --force
```

Run it for real on the bundled example:
[`examples/sample-tutorial.md`](interactive-tutorial/examples/sample-tutorial.md)
with the answer key in
[`examples/expected-dag.json`](interactive-tutorial/examples/expected-dag.json).
Full command reference:
[the skill README](interactive-tutorial/README.md).

## Using it as a template

Fork this repo and copy the skill directory once:

```bash
cp -r interactive-tutorial .agents/skills/build-a-vector-store
```

Then rewrite that copy's `SKILL.md` step 2 with your tutorial's node ids, files,
and verification commands. The schema, CLI, tests, and Makefile carry over: the
state format is tutorial-agnostic, so a tutorial-specific skill is data plus
prose. Keep the original if you also want the generic runner available.

## Checks

```bash
make lint               # byte-compile scripts and tests
make validate-examples  # validate + render the reference DAG
make test               # 26 unittest cases, each in its own temp git repo
make all                # all three, in that order
```

CI runs the same three targets on Python 3.9 and 3.12. Tests set `HOME` and
`GIT_CONFIG_GLOBAL` to a temporary directory, so they never read or write your
Git identity.

## Notes and limits

- The CLI never runs a node's own commands — only its verification gate. The
  agent runs the work, which is what keeps `guided` mode honest.
- Checkpoints use `git add -A`, so a node's commit contains whatever is in the
  workspace. Finish and commit one node before writing the next node's files.
- `jump --git-rollback hard` destroys uncommitted work and requires `--force`;
  `--git-rollback branch` is the safe default when the user might want the
  current state back.
- Rewinding Git history and rewinding DAG state are deliberately separate
  decisions: reverting files without lying about node status (or the reverse) is
  always expressible.
- Python 3.9 is declared and covered in CI but not exercised on the development
  machine (3.12 only).

## License

MIT — see [LICENSE](LICENSE). Copyright 2026 Andrew Gene Brown.
