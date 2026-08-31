# API reference

Generated from the code, one page per module, class, function, method,
property and constant. Everything with a docstring has a page; anything
without one is not listed, because a page saying only a name and a type is
worse than no page.

Four packages, which is the same split the tools themselves have. Only the
first is meant for your programs.

## [`pb_client`](pb_client/index.md) — the library

What to import. It talks to the background service, which is what keeps a hub
connected between commands, and gives you a hub to download programs to, run
them on, and read the output of. Compiling happens here too, so a program is
turned into bytecode by your machine rather than by the hub.

Start at [`connect`][pb_client.daemon.connect], which is an async context
manager yielding a [`Daemon`][pb_client.daemon.Daemon].

## [`pb_core`](pb_core/index.md) — what both halves agree on

The wire: the JSON-RPC protocol, its version, the transports it runs over, and
the platform paths and logging setup that the client and the service must not
disagree about. You would read this to write a client in another language, or
to understand what is going over the socket.

## [`pbd`](pbd/index.md) — the background service

The process that owns every hub connection and outlives any single command. It
holds the Bluetooth transport, the hub registry, and the RPC methods the
protocol describes. Documented for whoever works on it; nothing here is
something you import.

## [`pb`](pb/index.md) — the command line

The `pb` command's own internals: the commands, the terminal session, the
console handling and the error mapping. See [the command line](../cli/index.md)
for how to *use* it — this is how it is built.

!!! note "What is public"
    `pb_client` is the only one of the four with a public surface, and even it
    is early: nothing is stable yet. The other three are documented so that the
    tools can be worked on, not so that they can be imported.
