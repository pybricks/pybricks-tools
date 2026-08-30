# Pybricks Tools

Run Python programs on your [Pybricks](https://pybricks.com) hub from your
computer — from the command line, or from your own Python code.

```bash
uv tool install pybricks-cli

pb connect          # find a hub and connect to it
pb run demo.py      # compile the script, send it, and run it
```

`pb connect` only needs doing once. Later commands reuse the connection, so
repeating `pb run` while you work on a program is quick.

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

The same things are available as a library, so you can drive a hub from your
own program:

```python
async with pb_client.connect() as daemon:
    hub = await daemon.connect_hub(name="Pybricks Hub")
    program = await pb_client.compile_script(Path("demo.py"))
    await daemon.run_program(program)
```

> **Status:** early development. Nothing is stable yet.

## Documentation

Full documentation, including how the tools are put together, is in `docs/`.
See [CONTRIBUTING.md](CONTRIBUTING.md) to get set up.
