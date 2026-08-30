# Pybricks Tools

Run Python programs on your [Pybricks](https://pybricks.com) hub from your
computer — from the command line, or from your own Python code.

!!! note "Status"
    Early development. Nothing here is stable yet.

## Install

```bash
uv tool install pybricks-cli
```

That gives you the `pb` command.

## Run a program

Switch your hub on, then:

```bash
pb connect          # find a hub and connect to it
pb run demo.py      # compile the script, send it, and run it
```

`pb connect` only needs doing once. Later commands reuse the connection, so
`pb run` is quick to repeat while you are working on a program.

```bash
pb run demo.py      # again, without reconnecting
```

## Everyday commands

| Command | What it does |
| --- | --- |
| `pb scan` | List hubs that are switched on and nearby |
| `pb connect [HUB]` | Connect to a hub and keep it connected |
| `pb run SCRIPT` | Compile a script, send it, and run it |
| `pb download SCRIPT` | Send a script without running it |
| `pb stop` | Stop the running program |
| `pb status` | Show what a hub is doing |
| `pb list` | Show which hubs are connected |
| `pb disconnect` | Disconnect a hub |

With more than one hub connected, add the hub's name to any command:

```bash
pb run demo.py "Spare Hub"
```

Every command takes `-v` for more detail, and `-vv` or `-vvv` when something
is going wrong and you want to see why.

## From Python

The same things are available as a library, so you can drive a hub from your
own program:

```python
import anyio
import pb_client


async def main() -> None:
    async with pb_client.connect() as pybricks:
        hub = await pybricks.connect_hub(name="Pybricks Hub")
        print(f"connected to {hub.name}, firmware {hub.firmware}")

        program = await pb_client.compile_script(Path("demo.py"))
        reason = await pybricks.run_program(program)
        print(f"program {reason}")


anyio.run(main)
```

Nothing in the library takes a `timeout` argument. Wrap a call in a cancel
scope instead, which bounds the whole operation rather than one step inside it:

```python
with anyio.move_on_after(30):
    await pybricks.run_program(program)
```

Cancelling a run also stops the program on the hub, rather than leaving it
running.

## Where to next

- [How it works](design.md) — what keeps a hub connected between commands, and
  why the tools are split the way they are.
- [API reference](reference/pb_client.md) — the library, generated from the code.
- [Contributing](contributing.md) — getting set up, and the rules the code
  follows.
