# Changelog — pybricks-core

Notable changes to `pybricks-core`. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added

- IPC as byte streams over AF_UNIX, Windows named pipes, TCP or memory.
- JSON-RPC 2.0 with newline framing, a symmetric peer, and `$/cancelRequest`.
- The wire schema both the client and the service are built from.
- Platform paths for the socket, lock, cookie, log and cache directories.
- Logging setup, a TRACE level, and request and hub ids from contextvars.
- Finds the modules a program imports, for programs made of several files.
- Reads where a project keeps its source from whichever build backend it uses.
