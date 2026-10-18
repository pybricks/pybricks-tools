# pybricks-service

The background service behind the [Pybricks](https://pybricks.com) tools. It
owns the Bluetooth connection to a hub and outlives any single command, which
is what lets one `pb run` follow another without waiting to reconnect.

It also fans a hub's output out to several clients at once, so an interactive
session and a program watching the same hub do not get in each other's way.

**You do not install this yourself.** It comes with `pybricks-cli`, and it
starts when something needs it and stops when nothing has for a while.

> **Status:** beta. Commands and APIs may still change before 1.0.0.
