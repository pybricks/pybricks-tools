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
not matter how deep in the tree you are working.

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

## It does not have to be a Python project

`pyproject.toml` is where Python projects keep their tool settings, and `pb` is
a Python tool, so that is where it looks. A file holding nothing but the two
lines above is a perfectly good way to mark the top of a robot project that is
mostly LEGO.
