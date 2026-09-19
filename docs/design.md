# How it works

This page is about the internals. Nothing here is needed to use the tools.

## Why a background service

**A hub that nothing is connected to switches itself off.** Three minutes of it,
and the hub powers down and has to be picked up and switched on again by hand.
The firmware resets that timer on every pass of its idle loop for as long as a
host is attached, so a hub with something connected to it never times out at
all — which makes holding the connection the difference between a hub that is
still there when you come back and one that is not.

Connecting over Bluetooth is also slow — seconds, not milliseconds — so a tool
that connects afresh every time pays that on every run, which is exactly the
thing you do most while writing a program. And a hub can only be held by one
program at once, so a tool that takes it for the length of a command is a tool
nothing else can share it with.

So connection state lives in a background process, the `pb-service` command,
which outlives any single command:

```
pb-cli ──> pb-client ──> JSON-RPC ──> pb-service ──> hub
```

`pb connect` hands the hub to the service and exits. `pb run` connects to the
service, not to the hub. Two shells, an editor plugin and a telemetry script can
all be attached to one hub at once, each seeing the same output.

The service starts itself the first time something needs it, and exits on its
own once it has been idle for a while with no hubs connected. A connected hub
keeps it alive indefinitely, because holding that connection open is the point.

## The packages

| Package | Import | What it holds |
| --- | --- | --- |
| `pybricks-core` | `pb_core` | Wire protocol, transport, logging, paths |
| `pybricks-client` | `pb_client` | The library, and compilation |
| `pybricks-service` | `pb_service` | The service and hub protocol |
| `pybricks-cli` | `pb_cli` | The `pb` command |

`pb-cli` depends on `pb-service` so that installing the CLI alone gives a working
autostart, but never imports it: loading the Bluetooth stack on every
invocation would be slow, and there is a test asserting it does not happen.

## Talking to the service

The service listens on a per-user endpoint that the client finds by itself — a
UNIX socket on Linux and macOS, a named pipe on Windows. You never type it. TCP
is also supported, for a service on another machine.

Every connection authenticates with a shared secret from a file only your user
can read. This is not only for TCP: the default permissions on a Windows named
pipe would otherwise let another local user read your hub's output.

The protocol is JSON-RPC 2.0, one message per line, so it is readable with
ordinary tools and usable from languages other than Python. Binary payloads
travel as base64.

## Compilation happens on your computer

`mpy-cross` runs in the client, not the service. The script is on your
filesystem, and having the service read files on your behalf stops being
reasonable as soon as it is on a different machine. It also keeps a native
compiler binary out of the service.

## Why a hub stays connected

The service's hub manager is entered *before* the listener that accepts clients,
and left *after* it. Hub connections therefore live in a scope that is a
sibling of the client sessions rather than a child of any one of them. When a
client disconnects, only that client's tasks are cancelled; the hubs it was
using are untouched.

That property is structural rather than something the code remembers to do,
which is why it is worth arranging the scopes this way round.

## Buffering

Program output arrives in a Bluetooth callback that must never block — stalling
there stalls the radio for every hub. So that callback only ever hands bytes
over without waiting, into an unbounded queue, which means the handover cannot
fail. Losing a status report would leave the service disagreeing with the hub
about whether a program is running, and that must not happen.

What keeps an unbounded queue safe is byte accounting: past a high-water mark,
the callback discards program output only, and counts what it discarded.

Queues to individual clients are the other way round: bounded, and lossy. A
client that stops reading loses its own output and nothing else. It cannot slow
the hub, and so cannot stall a robot.

## Transports

A hub can be reached over Bluetooth or, on the larger hubs, over USB. Both sit
behind one seam, which speaks in terms of reading and writing characteristics
because that is the Pybricks protocol's own vocabulary — the USB interface
deliberately mirrors the same characteristics, so the handshake gathers the same
facts either way and nothing above the seam knows which answered.

The service holds both at once and prefers USB, because a cable is quicker to
find, quicker to use, and can reach a hub that Bluetooth cannot see at all: a
hub running a program stops advertising, while its USB port is there throughout.
`--transport` overrides that. A transport that cannot work on a machine — no
Bluetooth adapter, no permission on a serial port — is reported and stepped
over rather than stopping the others, so a computer with no radio still finds
the hub plugged into it.

USB is a plain serial port, so it needs no driver: Windows' own `usbser.sys`,
`/dev/ttyACM*` on Linux. Connecting over it also takes the hub off the air,
which is why a hub held that way does not appear in a scan — the same as one
held over Bluetooth.

**A port that will not open is not the same as no hub.** Stepping over a
transport is right for one that cannot work at all; a single port that
enumerates as a hub and then refuses to open is a different thing, and the
difference is the whole answer: the hub is plugged in and the computer is what
is in the way. A scan that found nothing else says so, with its own error code,
naming the port and repeating what the operating system said. The advice is the
client's, because the same refusal means a group to join on Linux and another
program holding the port on Windows.

**A cable pulled out is waited for.** If a hub the service was holding over USB
loses its cable, plugging it back in connects it again, with no second
`pb connect`. Putting a cable back is a deliberate act by somebody who wants to
carry on, which is what makes it safe to act on; a hub coming back into radio
range says nothing of the sort, and taking it could be taking it from whoever
else was reaching for it, so Bluetooth is only ever reported as gone. Nothing
bounds the wait, because the service's own idle shutdown already does: a hub
that is not connected does not hold the service open, so a hub that was
unplugged and carried away is waited for only as long as the service has some
other reason to be running.

The connection is what comes back, not the session. Whatever was attached is
told the hub went away, as it is today, and a program or an interpreter that
was running is the hub's business rather than something to restore.

A simulated hub implements the same seam. It is what lets the whole service be
tested on a machine with no Bluetooth adapter and nothing plugged in, including
cases that are otherwise hard to reach: a hub that refuses a command, or
disconnects halfway through a download.
