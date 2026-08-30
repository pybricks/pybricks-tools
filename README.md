# Pybricks Tools

A daemon, client library and CLI for talking to [Pybricks](https://pybricks.com)
hubs.

| Package | Distribution | What it is |
| --- | --- | --- |
| `pb_core` | `pybricks-core` | Shared wire protocol, IPC transport, logging |
| `pb_client` | `pybricks-client` | Structured-async client library |
| `pbd` | `pybricks-daemon` | The daemon that owns hub connections |
| `pb` | `pybricks-cli` | The `pb` command line interface |

The daemon keeps hubs connected between commands, so `pb run foo.py` twice in a
row reuses one BLE connection, and a terminal session can stay attached to a
hub's stdio while other commands run against it.

> **Status:** early development. Nothing is stable yet.

See [CONTRIBUTING.md](CONTRIBUTING.md) to get set up.
