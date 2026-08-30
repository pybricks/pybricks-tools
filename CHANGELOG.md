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
- `pb daemon start|stop|status`. The daemon outlives the command that starts
  it, so a later `pb daemon status` reaches the same process.
- The Pybricks hub protocol, written against the firmware header, with the
  profile-version gating that `START_USER_PROGRAM`, `START_REPL` and the status
  report all need.
- Hub connections, event fan-out to several clients at once, and a simulated
  hub so the whole daemon can be exercised without a radio.
- Bluetooth transport built on bleak, behind a seam that USB and RFCOMM can
  reuse.
- `pb scan`, `pb connect`, `pb disconnect`, `pb list`, `pb status` and
  `pb stop`. A hub connected by one command stays connected for the next.
- `pb run` and `pb download`: compiling with mpy-cross, chunked download, and
  waiting for the program to finish. Compilation happens on the client, so the
  daemon never reads the caller's files.

### Notes

- `mkdocs` is pinned below 2.0 deliberately: 2.0 removes the plugin and theming
  systems with no migration path.
- On Windows ARM64 the `mpy-cross-v6` wheel contains an x86-64 binary, so it
  runs under emulation. The daemon logs the resolved compiler path so this is
  diagnosable rather than mysterious.
