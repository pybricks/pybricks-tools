# Pybricks Tools

A suite for talking to [Pybricks](https://pybricks.com) hubs from a computer:

- **`pbd`** — a daemon that owns every hub connection and outlives any single
  command, so a hub stays connected between invocations and several clients can
  talk to one hub at once.
- **`pb-client`** — a structured-async library for driving the daemon.
- **`pb`** — a command line interface built on that library.

## Why a daemon?

With `pybricksdev`, every invocation reconnects over BLE, which is slow, and
nothing else can talk to the hub at the same time. Keeping connection state in a
daemon means `pb run foo.py` twice in a row reuses one connection, and a
terminal session can stay attached to a hub's stdio while other commands run
against it.

!!! note "Status"
    Early development. Nothing here is stable yet.
