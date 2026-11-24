# Changelog — pybricks-service

Notable changes to `pybricks-service`. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Changed

- `hub.connect` with no hub named connects to the chosen hub, even when
  another hub is already connected.
- `hub.connect` refuses with `HUB_AMBIGUOUS` when several hubs are found and
  none is named or chosen, rather than connecting to the first, and when
  several share the name asked for. It listens for 1 s after the first hub
  heard, so that a second hub that advertises less often is still heard, and
  for a second hub on USB too. Hubs with different names count as several even
  when heard on different transports. A hub only paired over RFCOMM counts, and
  is chosen, only when no other transport heard one, and it waits up to 3 s for
  one to be heard rather than starting the 1 s wait itself.
- `hub.connect` with a name hands back a connected hub with that name rather
  than scanning past it and connecting another hub with the same name. That
  hub, or with nothing named the connected one, is not handed back when
  `transport` asks for another route, and `exclusive` refuses it.

### Added

- `already_connected` in the `hub.connect` result, saying whether the hub was
  connected before the call.
- `chosen` in the data of `hub.connect`'s `HUB_NOT_FOUND`, naming the hub when
  it was the one chosen with `hub.use` that was not found.

### Fixed

- Input too big for the buffer of a hub on Pybricks 4.0 is refused at once,
  saying to update the firmware, rather than after 10 s as a hub not reading
  its input. 4.0 takes 20 bytes at a time, or 63 on
  hubs with USB, whatever write size it reports.
- A Bluetooth hub was sometimes first reported without its hub type, before
  its scan response arrived. Hubs are now reported only once it has, and one
  that never sends it is logged as a firmware bug rather than listed.

## [1.0.0b2] - 2026-10-24

### Fixed

- Connecting over Bluetooth on macOS, which failed reading the hub's name.

## [1.0.0b1] - 2026-10-18

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
