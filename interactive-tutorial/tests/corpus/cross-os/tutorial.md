# Record and verify the host platform

A short cross-platform tutorial for Linux and macOS (`darwin`).

## 1. Record the active platform

Write your operating system name to `platform.txt`.

On Linux:

```sh
printf 'linux\n' > platform.txt
```

On macOS (`darwin`):

```sh
printf 'darwin\n' > platform.txt
```

## 2. Normalize the platform note

Summarize `platform.txt` into `note.txt` using Python:

```sh
python3 -c "from pathlib import Path; Path('note.txt').write_text('os=' + Path('platform.txt').read_text().strip() + chr(10))"
```

## 3. Confirm the platform setup

Confirm that `platform.txt` and `note.txt` reflect your operating system.
