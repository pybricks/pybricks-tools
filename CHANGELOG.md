# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added

- Workspace skeleton: `pb-core`, `pb-client`, `pbd` and `pb` packages, with
  ruff, pyright strict, pytest, MkDocs and CI across Linux, macOS, Windows and
  Windows ARM64.
- IPC transport with four backends behind one interface -- AF_UNIX, Windows
  named pipes, TCP for remote daemons, and an in-process one for tests -- all
  yielding anyio byte streams so nothing above the transport is platform
  specific.
- JSON-RPC 2.0 with newline framing, a symmetric peer, and `$/cancelRequest`
  so a client-side deadline also stops the daemon working.
- Daemon lifecycle: single-instance lock, shared-secret authentication,
  autostart on demand, hub-aware idle timeout.
- `pb service start|stop|status`. The background service outlives the command
  that starts it, so a later `pb service status` reaches the same process.
- The Pybricks hub protocol, written against the firmware header, with the
  profile-version gating that `START_USER_PROGRAM`, `START_REPL` and the status
  report all need.
- Hub connections, event fan-out to several clients at once, and a simulated
  hub so the whole daemon can be exercised without a radio.
- Bluetooth transport built on bleak, behind a seam that USB and RFCOMM can
  reuse.
- `pb scan`, `pb connect`, `pb disconnect`, `pb list`, `pb status` and
  `pb stop`. A hub connected by one command stays connected for the next.
- `pb run`, `pb download`, `pb start` and `pb check`: compiling with mpy-cross
  and chunked download. Compilation happens on the client, so the daemon never
  reads the caller's files, and `pb check` needs no hub at all.
- `pb repl`: a session with the hub's interpreter, with a status line pinned
  along the bottom saying which hub is being talked to, what is running on it
  and how its battery is doing. Every keystroke goes to the hub, Ctrl-C
  included; Ctrl-] introduces the commands that are not the hub's, and Ctrl-D
  ends the interpreter and the session with it. A hub already busy with a
  program of its own is refused rather than interrupted.
- `pb run` and `pb start` stay with the program in that same session, so its
  output appears as it is printed and the keyboard reaches it. `--wait` says
  how long to stay and how much of the terminal to use: left out, the session
  takes the terminal when there is one; given, it stays but prints only the
  output, which is what a script wants; `--no-wait` starts the program and
  returns.
- Exit code 3 when a program ends in an unhandled exception. Nothing in the
  protocol distinguishes a program that finished from one that failed, so this
  reads what the firmware printed on its way out.
- `--slot` on `pb download` and `pb run` states which slot a program is
  expected to land in, and is refused if the hub has another one selected. The
  hub chooses, with its own buttons, and nothing on the wire can change that --
  so a program would otherwise be stored in one slot and started from another.

### Notes

- Documentation is built with ProperDocs rather than MkDocs. MkDocs 2.0 removes
  the plugin and theming systems with no migration path; ProperDocs continues
  the 1.6 line and reads both entry point groups, so Material, mkdocstrings,
  gen-files and literate-nav all load unchanged. MkDocs stays installed as a
  library, pinned below 2.0, because those plugins import it.
- On Windows ARM64 the `mpy-cross-v6` wheel contains an x86-64 binary, so it
  runs under emulation. The daemon logs the resolved compiler path so this is
  diagnosable rather than mysterious.
