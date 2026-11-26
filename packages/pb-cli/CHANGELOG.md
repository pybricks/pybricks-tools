# Changelog — pybricks-cli

Notable changes to `pybricks-cli`, the `pb` command. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

## [1.0.0b3] - 2026-11-25

### Added

- `pb run -n` and `pb start -n` leave stdin alone, for use inside a loop that
  reads its own input, as with `ssh -n`.

### Changed

- Without a terminal, `pb run` and `pb start --wait` send piped or redirected
  stdin to the program, so `input()` works in a script:
  `printf 'a\n' | pb run prog.py`. Line endings become `\r`. A program used to
  wait in `input()` for ever. A `pb run` inside a loop that reads its own input
  now takes the rest of it; use `-n` there.

## [1.0.0b2] - 2026-11-23

### Changed

- `pb connect` with no `--hub` connects to the hub chosen with `pb use`
  ([support#2866](https://github.com/pybricks/support/issues/2866)).
- `pb connect` asks which hub when several are switched on and none is chosen,
  instead of taking the first one heard. The same goes for several hubs
  sharing the name asked for or chosen, as unnamed hubs all do. A hub only
  paired over RFCOMM is not offered unless the service counted it.
- `pb connect` says "Already connected" when the hub already was, rather than
  claiming to have connected it.
- When the hub chosen with `pb use` is not found, `pb connect` says how to
  choose another.

### Fixed

- `pb connect` printed a hub name with square brackets in it as rich markup.

## [1.0.0b1] - 2026-10-18

### Added

- `pb connect`, `disconnect`, `list`, `status`, `use` and `scan`.
- `pb run`, `download`, `start`, `stop`, `check` and `repl`.
- `pb service start`, `stop`, `status` and `log`.
- A hub connected by one command is still connected for the next.
- `pb run` and `pb start` stay with the program, showing output as it prints.
- Every keystroke goes to the hub; `Ctrl-]` introduces the ones that do not.
- Escape pressed twice stops what the hub is running, for stopping in a hurry.
- Typing keeps up with the hub: a burst of keystrokes goes in one write.
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
- `--transport usb`, `ble` or `rfcomm`, the last for EV3 and NXT hubs.
