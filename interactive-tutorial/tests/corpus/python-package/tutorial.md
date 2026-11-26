# Build the `greetpkg` package

A small src-layout package with one function, a unittest suite, and an optional entry point. Stdlib only.

## 1. Layout

The package lives in `src/greetpkg/`. The `src/` layout keeps the import path honest: tests import the code the way users will. Read this section, then name the file that defines `greet()`.

## 2. Write `greet()`

Create `src/greetpkg/__init__.py` with a `greet(name)` function that returns `Hello, <name>!`.

## 3. Write `pyproject.toml`

Write `pyproject.toml` with the project name `greetpkg` and version `0.1.0`. Do not install it; this tutorial only writes it.

## 4. Write the unit test

Create `tests/test_greet.py` with a unittest case for `greet('Ada')`.

## 5. Run the tests

Run the suite from the repository root:

```sh
PYTHONPATH=src python3 -m unittest discover -s tests
```

It should end with `OK`.

## 6. Optional: `__main__.py`

Add `src/greetpkg/__main__.py` with a `main()` that prints the greeting for its first argument. Then run `PYTHONPATH=src python3 -m greetpkg Ada`.

## 7. Release note

Write the 0.1.0 note in your own changelog.
