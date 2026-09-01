# pybricks-cli

The `pb` command: run Python programs on a [Pybricks](https://pybricks.com) hub
from your computer.

```bash
uv tool install pybricks-cli
```

Then switch a hub on:

```bash
pb connect          # find a hub and connect to it
pb run demo.py      # compile the script, send it, and run it
```

**The hub stays connected after the command finishes**, so the second `pb run`
starts instantly, and so does the hundredth.

`pb repl` talks to the hub's interpreter a line at a time, and `pb run -c` runs
a line or two of Python without a file. `pb --help` lists the rest.

> **Status:** early development. Nothing is stable yet.
