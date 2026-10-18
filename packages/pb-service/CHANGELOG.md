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
- EV3 and NXT hubs over Bluetooth Classic, found among the paired devices.
- Says when macOS is asking for Bluetooth permission, and waits for the answer.
- A serial port that will not open is reported, not taken for a missing hub.
- The Pybricks hub protocol, gated by profile version wherever it changed.
- Speaks every Pybricks Profile version up to v1.7.0.
- Scanning that reports each hub as it is heard, with type and signal strength.
- Remembers the hub chosen with `pb use`, in the user's state directory.
- A simulated hub, so the whole service can be exercised without a radio.
- Input is retried at the size the hub advertised, never cut down to fit.
- Starts at the filesystem root, holding no directory open behind it.
- USB, as a serial port, preferred over Bluetooth for a hub reachable both ways.
- Goes back to a hub over USB when its cable is plugged in again.
- A simulated serial port, so the USB link is covered without a hub attached.
