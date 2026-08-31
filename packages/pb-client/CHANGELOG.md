# Changelog — pybricks-client

Notable changes to `pybricks-client`. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added

- `connect()` gives an async handle to the service, starting one if needed.
- Ongoing events are async context managers yielding async iterators.
- No function takes a `timeout`; callers scope cancellation themselves.
- Compiles a script and every module of its own that it imports.
- Passes a precompiled `.mpy` through, for modules built for native code.
- Refuses a relative import, which no hub can perform, naming the line.
- Names a program's entry module for the hub's Pybricks profile version.
- Reports the compiler's path and architecture, which matters on ARM64.
