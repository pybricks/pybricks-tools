# Changelog — pybricks-core

Notable changes to `pybricks-core`. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

## [1.0.0b3] - 2026-11-25

### Removed

- `PROTOCOL_VERSION` and `MIN_PROTOCOL_VERSION`. pb-client and pb-service
  declare the protocol version they implement themselves, so a new
  pybricks-core can no longer make an older one claim a version it does not
  speak.

## [1.0.0b2] - 2026-11-23

### Added

- `ErrorCode.HUB_AMBIGUOUS`, for a hub that could be any of several.

### Changed

- `PROTOCOL_VERSION` and `MIN_PROTOCOL_VERSION` are 4, for the changes to
  `hub.connect`. A client and a service of different versions refuse each
  other, and `pb service stop` replaces a service left over from before.

## [1.0.0b1] - 2026-10-18

### Added

- IPC as byte streams over AF_UNIX, Windows named pipes, TCP or memory.
- JSON-RPC 2.0 with newline framing, a symmetric peer, and `$/cancelRequest`.
- The wire schema both the client and the service are built from.
- Platform paths for the socket, lock, cookie, log and cache directories.
- Logging setup, a TRACE level, and request and hub ids from contextvars.
- Finds the modules a program imports, for programs made of several files.
- Reads where a project keeps its source from whichever build backend it uses.
