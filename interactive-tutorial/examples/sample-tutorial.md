# Build a Tiny `todo` CLI

A short tutorial for practising non-linear tutorial execution. It has a linear
core, two genuinely optional features, one external step, and one convergence
check. Use it as the input to the `interactive-tutorial` skill: the expected
decomposition is `examples/expected-dag.json`, and it is a diamond, not a line.

Everything here is stdlib-only Python so any workspace can run it.

---

## 1. The design: one parser, one function per verb

`argparse` subparsers beat positional "mode" strings because each verb gets its
own flags and its own `--help`, and the dispatch table stays a dictionary of
`name -> function`. The package layout used below:

```text
src/todo/
├── __init__.py
├── cli.py      # parser + dispatch
├── store.py    # JSON persistence   (optional feature A)
└── render.py   # table formatting    (optional feature B)
```

Nothing to write in this section. You are done when you can say, out loud, which
file you would edit to add a `--done` flag to the `list` command
(`cli.py` for the flag, `render.py` for the display).

## 2. Scaffold the CLI

Create the package and the dispatch table:

```python
# src/todo/__init__.py
"""Tiny todo CLI."""
```

```python
# src/todo/cli.py
"""Command dispatch for the todo CLI."""

import argparse


def build_parser():
    parser = argparse.ArgumentParser(prog="todo", description="Tiny todo list.")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("add", help="Add a task")
    sub.add_parser("list", help="List tasks")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    print("running:", args.command)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

Done when `python3 src/todo/cli.py --help` prints both subcommands.

## 3. Prove the scaffold runs

Run the CLI and confirm argparse is wired correctly:

```bash
python3 src/todo/cli.py list
```

Expected output: `running: list`. A usage error instead means the subparser
block is outside `build_parser()`.

## 4. Optional: JSON persistence

You can skip this section and still finish the tutorial; the CLI stays
in-memory. Add storage:

```python
# src/todo/store.py
"""JSON file persistence."""

import json
import os


def load():
    path = os.environ.get("TODO_DB", "todos.json")
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def save(tasks):
    path = os.environ.get("TODO_DB", "todos.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(tasks, handle, indent=2)
```

Done when `store.py` defines `load()` and `save()`.

## 5. Optional: table output

Also independent of section 4 — render a column table:

```python
# src/todo/render.py
"""Fixed-width table rendering."""


def render(tasks):
    rows = ["{:<3} {}".format(i + 1, task) for i, task in enumerate(tasks)]
    return "\n".join(rows) if rows else "(no tasks)"
```

Done when `render([])` returns `(no tasks)`.

## 6. Point the CLI at a real database file

This happens outside the code. Export a database path in **your own shell** so
the tutorial never writes into your home directory, and confirm your shell
startup file still parses afterwards:

```bash
export TODO_DB="$PWD/todos.json"   # add to your shell profile only if you want it sticky
```

There is no file in the workspace to check. Done when you have set the variable
in the shell running the tutorial and can echo it back.

## 7. Converge: both features coexist

The two optional sections must not fight over the same file. Check they landed
together, or that you deliberately skipped one:

```bash
grep -c "def " src/todo/store.py src/todo/render.py
```

Done when every file that exists reports at least one function definition, and
the ones you skipped are skipped on purpose rather than forgotten.

---

## Where people go wrong

- Editing `cli.py` in both optional sections at once. They are siblings; touch
  `store.py` or `render.py`, and only wire `cli.py` once both exist.
- Treating section 6 as verifiable by a script. It is an external step: the
  honest gate is a human confirmation.
- Skipping section 4 silently. A skipped node and a completed node look the same
  downstream and feel very different six nodes later.
