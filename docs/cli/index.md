# The command line

`pb` is one command with a verb after it:

```bash
pb run demo.py
pb repl
pb service log -f
```

Every command that acts on a hub takes `--hub NAME` to say which one, and every
command takes `-v` for more detail — `-vv` and `-vvv` when something is going
wrong and you want to see why. The [Commands](commands.md) page lists them all,
generated from the tool itself, so it says exactly what your copy accepts.

## Where an option comes from

Most of the time you type it. When you do not, `pb` looks in three more places
before the command decides for itself. Highest wins:

| | Where | Good for |
| --- | --- | --- |
| 1 | What you type | This one command |
| 2 | [The environment](environment.md) — `PB_HUB` and friends | One shell, or one CI job |
| 3 | [The project file](pyproject.md) — `[tool.pb]` in `pyproject.toml` | Everyone who clones the project |
| 4 | The hub chosen with `pb use` | You, everywhere, until you change it |
| 5 | The only hub connected | Not having to think about it |

A project beats a standing preference because it is the more specific
statement: whoever clones a robot project should get that robot's hub without
having to know that you once ran `pb use`.

## Running from somewhere else

`-C` runs a command as if you were in another directory, the way `git -C` does:

```bash
pb -C robot-a run src/robot/demo.py
```

Point it at the directory holding a project's `pyproject.toml`, or at the top of
your source when there is no project file. It settles three things at once:
which project's settings apply, what a relative path means, and
[where a program's imports are looked for](multi-file.md#where-modules-are-looked-for).

It earns its place when one repository holds more than one robot:

```text
robots/
  robot-a/pyproject.toml     [tool.pb] hub = "Bumblebee"
  robot-b/pyproject.toml     [tool.pb] hub = "Rover"
```

From the top of that tree, neither project's settings apply to anything, and
from inside `robot-b` they are *robot-b's* — which is the wrong hub, not merely
a missing one. `pb -C robot-a` says which you mean, and it means the same thing
from any shell.

## What a command exits with

| Code | Meaning |
| --- | --- |
| `0` | It worked |
| `1` | Something on this computer went wrong |
| `3` | The hub, or the program on it, was the problem |
| `4` | No hub is connected |
| `130` | Interrupted |

A program that ends in an unhandled exception exits `3`, and one interrupted
with ++ctrl+c++ exits `130`, which is what CPython does.

## Where output goes

Data goes to stdout and everything else to stderr, so a command can always be
piped:

```bash
pb scan > hubs.txt          # the hubs, and nothing else
pb service log > problem.txt
pb run demo.py | tee run.log
```

Down a pipe the tables become tab-separated fields and nothing is coloured, so
`cut` and `grep` work on them. Progress, spinners, prompts and log lines are on
stderr and stay out of the way.
