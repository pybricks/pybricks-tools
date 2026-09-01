# Changelog — pybricks-service

Notable changes to `pybricks-service`. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added

- Keeps hubs connected between commands, so nothing reconnects each time.
- Fans hub output and status out to several clients at once, losslessly.
- Single-instance lock, shared-secret authentication, and a startup log line.
- Exits when idle, but never while a hub is still connected.
- Bluetooth through bleak, behind a seam that USB and RFCOMM can reuse.
- The Pybricks hub protocol, gated by profile version wherever it changed.
- Scanning that reports each hub as it is heard, with type and signal strength.
- Remembers the hub chosen with `pb use`, in the user's state directory.
- A simulated hub, so the whole service can be exercised without a radio.
- Input is retried at the size the hub advertised, never cut down to fit.
- Starts at the filesystem root, holding no directory open behind it.
