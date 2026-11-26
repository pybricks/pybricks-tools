# Changelog — pybricks-client

Notable changes to `pybricks-client`. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

## [1.0.0b3] - 2026-11-25

### Changed

- The protocol version this client speaks is declared in pb-client, as
  `pb_client.service.PROTOCOL_VERSION`, rather than read from pybricks-core.

## [1.0.0b2] - 2026-11-23

### Changed

- `Service.connect_hub()` with no hub named connects to the chosen hub, and
  raises `HUB_AMBIGUOUS` when several are switched on and none is chosen.

### Added

- `HubInfo.already_connected`, set when `Service.connect_hub()` handed back a
  hub that was connected before.

## [1.0.0b1] - 2026-10-18

### Added

- `connect()` gives an async handle to the service, starting one if needed.
- Ongoing events are async context managers yielding async iterators.
- `watching_adapters()` says which Bluetooth adapters a search cannot use.
- No function takes a `timeout`; callers scope cancellation themselves.
- Compiles a script and every module of its own that it imports.
- Passes a precompiled `.mpy` through, for modules built for native code.
- Refuses a relative import, which no hub can perform, naming the line.
- Names a program's entry module for the hub's Pybricks profile version.
- Reports the compiler's path and architecture, which matters on ARM64.
