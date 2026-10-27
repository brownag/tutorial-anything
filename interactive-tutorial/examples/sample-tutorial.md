# Build a Tiny `todo` CLI

Practice running tutorials as non-linear graphs. This tutorial has a core path, two optional features, an external step, and a convergence check. See `examples/expected-dag.json` for the reference DAG.

Uses the Python standard library. No extra dependencies required.

---

## 1. Design: subcommands and dispatch

`argparse` subparsers give each verb its own flags and `--help` output. The dispatch table maps command names to functions.

Package structure:

```text
src/todo/
├── __init__.py
├── cli.py      # parser + dispatch
├── store.py    # JSON persistence   (optional feature A)
└── render.py   # table formatting    (optional feature B)
```

No code to write here. Identify which file requires edits to add a `--done` flag to `list` (`cli.py` for flags, `render.py` for rendering).

## 2. Scaffold the CLI

Create the package files and argument parser:

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

Done when `python3 src/todo/cli.py --help` lists both subcommands.

## 3. Verify scaffold execution

Run the CLI:

```bash
python3 src/todo/cli.py list
```

Expected output: `running: list`.

## 4. Optional: JSON persistence

You can skip this step. The CLI defaults to in-memory state. Add file storage:

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

Independent of step 4. Add formatted table output:

```python
# src/todo/render.py
"""Fixed-width table rendering."""


def render(tasks):
    rows = ["{:<3} {}".format(i + 1, task) for i, task in enumerate(tasks)]
    return "\n".join(rows) if rows else "(no tasks)"
```

Done when `render([])` outputs `(no tasks)`.

## 6. Configure database path

Configure storage outside the codebase. Set a database path in your current shell:

```bash
export TODO_DB="$PWD/todos.json"
```

No files change in this step. Confirm by printing the environment variable in your terminal.

## 7. Converge features

Verify that both optional files exist without conflicts:

```bash
grep -c "def " src/todo/store.py src/todo/render.py
```

Done when each existing file defines at least one function.

---

## Common mistakes

- Editing `cli.py` across both optional sections at once. Edit `store.py` or `render.py` first, then wire into `cli.py`.
- Trying to automate step 6 with a script. This is an external configuration step that requires manual confirmation.
- Skipping step 4 without marking it skipped in the DAG.
