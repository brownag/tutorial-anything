# AGENTS.md - tutorial-anything

## Project Overview
Container repository [`tutorial-anything`](https://github.com/brownag/tutorial-anything) for the `interactive-tutorial` Agent Skill (the bundled skill name and directory remain `interactive-tutorial/`): a Python stdlib CLI plus agent instructions that turn a static coding tutorial into a verifiable DAG, tracked in `.tutorial-dag.json` and checkpointed with Git commits.

## Quick Start
Run from the repo root. The root `Makefile` forwards every target to `interactive-tutorial/Makefile`.

* **Install deps**: none (Python >= 3.9 stdlib, Git >= 2.25)
* **Everything (CI equivalent)**: `make all` -> lint + validate-examples + test
* **Test all**: `make test`
* **Test one case**: `cd interactive-tutorial && python3 -m unittest tests.test_dag_manager.DagManagerTest.<test_name> -v`
* **Lint**: `make lint` (byte-compile only; no formatter or linter configured)
* **Validate examples**: `make validate-examples`
* **Run the CLI**: `python3 interactive-tutorial/scripts/dag_manager.py --help`
* **Install skill**: `make install` (symlink to `~/.agents/skills`), `make install SKILLS_DIR=<dir>`, `make uninstall`
* **List targets**: `make help`, `make -C interactive-tutorial help`

## Find Things
```text
.
├── Makefile                          forwards targets to interactive-tutorial/
├── README.md                         repo overview, install, constraints
├── .github/workflows/ci.yml          CI: lint, validate-examples, test (Python 3.9, 3.12)
├── planning/                         local-only notes (git-ignored via planning/.gitignore)
├── interactive-tutorial/             THE SKILL SOURCE - edit files here
│   ├── SKILL.md                      agent instructions (frontmatter: name, version, compatibility)
│   ├── README.md                     CLI reference and troubleshooting
│   ├── Makefile                      lint, test, validate-examples, install
│   ├── scripts/dag_manager.py        single-file CLI (subcommands in build_parser())
│   ├── references/                   DAG schema: prose (.md) + JSON Schema (.json)
│   ├── templates/                    SKILL.md template for scaffolded skills
│   ├── examples/                     sample tutorial and its expected DAG
│   └── tests/test_dag_manager.py     unittest suite, runs in temp git repos
└── .agents/skills/interactive-tutorial/   relative symlinks into interactive-tutorial/
```

CLI subcommands: `init`, `draft`, `validate`, `status`, `next`, `doctor`, `verify`, `complete`, `jump`, `service` (`start`, `stop`), `teardown`, `export`, `approve`, `deviate`, `residue` (`clear`), `scaffold`. Each maps to a `cmd_<name>` function in `scripts/dag_manager.py`.

## Workflows

### Change CLI behavior
1. Edit `interactive-tutorial/scripts/dag_manager.py`.
2. Add or update a test in `interactive-tutorial/tests/test_dag_manager.py`.
3. If the state document shape changes, update both `references/dag-schema.md` and `references/dag-schema.json`, then `examples/expected-dag.json`.
4. Update user-facing docs: `SKILL.md` (agent workflow) and `interactive-tutorial/README.md` (CLI reference).
5. `make all`.

### Change agent instructions
Edit `interactive-tutorial/SKILL.md`. Keep CLI flags in it in sync with `build_parser()`.

### Release
Bump `version` in the `SKILL.md` frontmatter.

## Non-Obvious System Behaviors
* `.agents/skills/interactive-tutorial/` is symlinks only. Never edit through it or replace links with copies; edit `interactive-tutorial/`.
* `interactive-tutorial/` must stay self-contained: it is copied into other repos as a template and must build, test, and install on its own. Don't make it depend on root files.
* `init` adds `.tutorial-dag.json` to `.git/info/exclude` in the target workspace; the repo `.gitignore` also ignores it.
* `complete --commit` runs `git add -A`. `jump --git-rollback hard` resets the working tree and needs `--force`.
* Tests isolate Git by pointing `HOME` and `GIT_CONFIG_GLOBAL` at a temp dir. Keep new tests doing the same.
* No third-party dependencies, in the CLI or the tests. Code must run on Python 3.9.

## System Boundaries & Operational Rules
### Always
* Run `make all` before declaring a change done.
### Ask First
* Adding any dependency or tooling (pytest, ruff, etc.).
* Changing the DAG schema in a non-backward-compatible way.
### Never
* Run `jump --git-rollback hard` or `make install`/`uninstall` against real user directories during testing; use temp repos and `SKILLS_DIR=<tmp>`.

## References
* `interactive-tutorial/SKILL.md` - agent workflow (session init, decomposition, stepping, navigation)
* `interactive-tutorial/references/dag-schema.md` - schema and state transition rules
* `interactive-tutorial/README.md` - CLI reference and troubleshooting
* `README.md` - repository layout, install, constraints
