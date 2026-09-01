# pybricks-core

The pieces shared by the [Pybricks](https://pybricks.com) tools: the JSON-RPC
protocol the client and the service speak, the transports underneath it, the
compiler that turns a script into something a hub can run, and the logging and
file locations all of them use.

**You do not install this yourself.** It comes with `pybricks-cli` and with
`pybricks-client`, which is the one to reach for if you want to drive a hub from
your own Python.

> **Status:** early development. Nothing is stable yet.
