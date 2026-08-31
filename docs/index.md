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

`pb run` stays with the program: what it prints appears as it prints it, and
what you type goes to the hub, so `input()` works and ++ctrl+c++ interrupts.
Press ++ctrl+"]"++ ++q++ to leave, or ++ctrl+"]"++ ++h++ for the rest. Down a
pipe or in a build there is no terminal to do that with, so the output simply
comes through on stdout.

`pb connect` only needs doing once. Later commands reuse the connection, so
`pb run` is quick to repeat while you are working on a program.

```bash
pb run demo.py      # again, without reconnecting
```

## Everyday commands

| Command | What it does |
| --- | --- |
| `pb connect` | Connect to a hub and keep it connected |
| `pb run SCRIPT` | Compile a script, send it, and run it |
| `pb run -c CODE` | Run a line or two of Python without a file |
| `pb repl` | Talk to the hub's interpreter, a line at a time |

That is most of what a day looks like. The rest — stopping a program, sending
one without running it, seeing which hubs are connected — is on the
[Commands](cli/commands.md) page, along with every option each one takes.

You never have to run `pb scan`: `pb connect` does its own looking. It is there
for when you want to know what is out there without connecting to any of it. It
fills its table as hubs are heard rather than when it finishes, so a hub appears
the moment you switch it on. Each row says what kind of hub it is
and how strong its signal is:

```
  Name           Type           Signal
  Bumblebee      Inventor hub     ▁▃▅▇
  Rover          City hub         ▁▃▅·
```

A hub is named when its firmware is installed, and a hub flashed without a name
being chosen answers to `Pybricks Hub` like every other one. So the two columns
are what tell those apart: which kind of hub it is, and whether it is on the
desk or through a wall. Both are known without connecting to anything.

`pb check` needs no hub and no service at all -- it is the compiler on its own,
which makes it the right thing to put in a commit hook or a build:

```bash
pb check demo.py
```

To pick a hub from that list instead of reading it, use `pb connect --scan`.
++up++ and ++down++ move the selection and ++enter++ connects. There is no time
limit, so you can switch the hub on while it is watching, and ++ctrl+c++ gives
up without connecting to anything.

With more than one hub connected, name the one you mean with `--hub`, which
every command takes:

```bash
pb run demo.py --hub "Rover"
```

If it is always the same hub, choose it once instead of saying so every time:

```bash
pb use                      # choose from the hubs connected and nearby
pb use --hub "Rover"        # or name it
```

What is remembered is the hub's name, so the choice survives switching the hub
off and on, and outlives the background service; every shell sees the same one.
The hub does not have to be connected, or even switched on, so the hub a
project belongs to can be chosen before any of it is plugged in.
`pb use --clear` forgets it again.

When the project is a shared one, it should name its own hub instead, in its
`pyproject.toml`:

```toml
[tool.pb]
hub = "Bumblebee"
```

Then every command run inside that project uses that hub, and a robot project
works for whoever clones it without anybody having to remember a flag. See
[the project file](cli/pyproject.md) for what else goes in there, and
[environment variables](cli/environment.md) for `PB_HUB`, which sits between
the two and is how you point one shell or one CI job somewhere else.

Every command takes `-v` for more detail, and `-vv` or `-vvv` when something
is going wrong and you want to see why.

## From Python

The same things are available as a library, so you can drive a hub from your
own program:

```python
from pathlib import Path

import anyio
import pb_client


async def main() -> None:
    async with pb_client.connect() as service:
        hub = await service.connect_hub(name="Rover")
        print(f"connected to {hub.name}, firmware {hub.firmware}")

        program = await pb_client.compile_script(Path("demo.py"))
        reason = await service.run_program(program)
        print(f"program {reason}")


anyio.run(main)
```

Nothing in the library takes a `timeout` argument. Wrap a call in a cancel
scope instead, which bounds the whole operation rather than one step inside it:

```python
with anyio.move_on_after(30):
    await service.run_program(program)
```

Cancelling a run also stops the program on the hub, rather than leaving it
running.

## When something goes wrong

`pb` tries to say what happened in one line and, where there is one, what to do
about it:

```console
$ pb run demo.py
No hub found.
Switch the hub on and press its button so that its light flashes.
```

If you ever see a traceback instead, that is a bug in `pb` rather than something
you did -- please report it, including the traceback and what you were doing.

Commands exit `0` when they succeed, and non-zero when they do not: `3` if the
hub or your program was the problem, `4` if no hub is connected, `1` otherwise.

## Where to next

- [The command line](cli/index.md) — every command, every option, and where an
  option comes from when you do not type it.
- [API reference](reference/pb_client/index.md) — the library, generated from the code.
- [How it works](design.md) — what keeps a hub connected between commands, and
  why the tools are split the way they are.
- [Contributing](contributing.md) — getting set up, and the rules the code
  follows.
