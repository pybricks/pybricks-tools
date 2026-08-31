# Programs of several files

A program does not have to be one file. `pb run` follows the imports in your
script, compiles what it finds, and sends the whole lot as one program:

```text
robot/
  demo.py        import helpers, from robot.motors import spin
  helpers.py
  robot/
    motors.py
```

```bash
pb run demo.py
```

Nothing has to be listed anywhere. Anything your program imports that `pb` can
find next to it goes along; anything it cannot find is assumed to be part of
the firmware, which is what `from pybricks.hubs import ThisHub` is.

## Where modules are looked for

One directory, and which one depends on how your project is laid out.

| Your project | Modules are looked for in |
| --- | --- |
| Scripts in a folder, no `pyproject.toml` | The script's own folder |
| A project with a `src/` directory | `src/` |
| A project whose build backend names a directory | That directory |
| Anything else | The project's top directory |

You do not configure this. If your project keeps its code in `src/` — because
uv, setuptools, hatch or poetry put it there — `pb` reads that from the same
`pyproject.toml` your build backend does, so `import robot.motors` finds
`src/robot/motors.py` and nothing has to be said twice.

When there are several projects in one repository, or when you are running from
somewhere else entirely, [`-C`](index.md#running-from-somewhere-else) says which
project you mean.

## There are no packages on the hub

A hub looks a module up by its whole name. `robot.motors` is simply a module
whose name has a dot in it, which has two consequences worth knowing.

**You do not need `__init__.py`.** A folder of modules works as it stands. If
you have `__init__.py` files because your project is also a Python package,
they are ignored — importing `robot` on its own is importing a module called
`robot`, which is `robot.py`, not the folder.

**Relative imports do not work.** `from . import helpers` is refused, and `pb`
says which line it is on rather than letting the hub fail halfway through your
program:

```console
$ pb run demo.py
The hub cannot do relative imports (robot/motors.py:3).
```

Write `from robot import helpers` instead, or `import helpers` if it sits at the
top.

One more oddity from the flat namespace: `import robot.motors` gives you the
`robot.motors` module under the name `robot`, so `robot.motors.spin()` will not
work. `from robot.motors import spin` and `import robot.motors as motors` both
do what you expect, and one of those is what most people write anyway.

## Modules that are already compiled

A `.mpy` file beside your script is sent as it is, without being compiled:

```text
demo.py
fast.mpy       import fast
```

This is the only way to use a module built for a hub's native architecture,
which cannot be produced from Python source. A `.py` wins over a `.mpy` of the
same name, so a stale one cannot shadow the file you are editing, and `pb`
checks the `.mpy` is one this hub's firmware will accept rather than letting the
hub refuse the whole program without saying which file was wrong.

## What the hub calls your program

The first module is the one that runs, and its name is what the hub shows —
so `pb run demo.py` gives a program called `demo`. On firmware older than
Pybricks profile v1.5.0 the entry module has to be called `__main__` instead,
and `pb` does that for you when it sees such a hub; you will only notice
because the hub calls the program `__main__`.

`pb run -c 'print(1)'` has no file to be named after, so its one module is
always `__main__`.
