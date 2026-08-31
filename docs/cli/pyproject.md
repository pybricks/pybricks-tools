# The project file

A project can name the hub it belongs to, so that everybody who clones it gets
the right one without having to remember a flag. Put it in the project's
`pyproject.toml`:

```toml
[tool.pb]
hub = "Bumblebee"
```

Every command run anywhere inside that project acts on that hub. `pb` looks for
the nearest `pyproject.toml` at or above the directory you are in, so it does
not matter how deep in the tree you are working, and
[`-C`](index.md#running-from-somewhere-else) is how you point at a different one.

A `pyproject.toml` with no `[tool.pb]` in it is stepped over rather than
stopping the search. So a repository holding several projects can name a hub
once at the top and override it where it matters:

```text
robots/pyproject.toml            [tool.pb] hub = "Bumblebee"
robots/robot-a/pyproject.toml    a package, nothing about pb  -> Bumblebee
robots/robot-b/pyproject.toml    [tool.pb] hub = "Rover"      -> Rover
```

It beats the hub chosen with `pb use`, because a project is the more specific
statement, and it loses to `PB_HUB` and to anything you type. Inside a project
that names a hub, `pb use` on its own pins that hub rather than offering a list
— the hub is no longer missing, so there is nothing to choose. `pb use --clear`
first if you want the list.

## What the table understands

| Key | Meaning |
| --- | --- |
| `hub` | The name of the hub this project's commands act on |

That is the whole list at the moment, and an unknown key is refused by name
rather than ignored:

```console
$ pb list
[tool.pb] does not understand hubb.
Check /home/you/robot/pyproject.toml. [tool.pb] understands hub.
```

Being strict here is deliberate, and it is the one place `pb` is stricter than
the environment. This file is committed and read by everyone who clones the
project, which makes it documented surface rather than a convenience. A
misspelled key that was quietly ignored would leave the hub silently wrong,
which is the kind of thing somebody loses an afternoon to.

## Where your source is, it already knows

There is no key for this. If your project keeps its modules in `src/`, or your
build backend has been told to look somewhere else, `pb` reads the same setting
your backend does and looks for
[a program's imports](multi-file.md#where-modules-are-looked-for) there.

| Backend | What it reads |
| --- | --- |
| uv | `[tool.uv.build-backend] module-root` |
| setuptools | `package-dir`, or `[tool.setuptools.packages.find] where` |
| hatchling | `[tool.hatch.build.targets.wheel]` `packages`, `sources` or `only-include` |
| poetry | `[tool.poetry] packages = [{include = "...", from = "..."}]` |
| pdm | `[tool.pdm.build] package-dir` |
| maturin | `[tool.maturin] python-source` |

None of that is standard — where a project keeps its source is left to the
build backend, and every backend spells it differently — but they all agree on
`src/`, so a project with a `src/` directory needs no configuration at all.

## It does not have to be a Python project

`pyproject.toml` is where Python projects keep their tool settings, and `pb` is
a Python tool, so that is where it looks. A file holding nothing but the two
lines above is a perfectly good way to mark the top of a robot project that is
mostly LEGO.
