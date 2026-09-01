# Changelog — pybricks-cli

Notable changes to `pybricks-cli`, the `pb` command. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added

- `pb connect`, `disconnect`, `list`, `status`, `use` and `scan`.
- `pb run`, `download`, `start`, `stop`, `check` and `repl`.
- `pb service start`, `stop`, `status` and `log`.
- A hub connected by one command is still connected for the next.
- `pb run` and `pb start` stay with the program, showing output as it prints.
- Every keystroke goes to the hub; `Ctrl-]` introduces the ones that do not.
- Escape pressed twice stops what the hub is running, for stopping in a hurry.
- `--wait` says how long to stay with a program and how much terminal to take.
- Exit code 3 when a program crashes and 130 when it is interrupted.
- `--slot` states where a program should land, and is refused if it cannot.
- `--hub NAME` on every command that acts on a hub, never a positional.
- `pb -C DIR` runs a command as if from another directory, as `git -C` does.
- Options fall back to `PB_*`, then to `[tool.pb]` in `pyproject.toml`.
- A program's imports resolve against the project's own source directory.
- Data on stdout and everything else on stderr, so every command can be piped.
- One-line errors with a suggested fix; a traceback only when it is our bug.
- An arrow-key picker wherever the tool would otherwise have to guess.
