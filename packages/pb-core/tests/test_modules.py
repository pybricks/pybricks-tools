# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

from pathlib import Path

import pytest

from pb_core import modules


def write(path: Path, source: str) -> Path:
    """Write a module, making its parent directories."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")
    return path


# ------------------------------------------------------------------ imports


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("import a", ("a",)),
        ("import a.b", ("a.b",)),
        ("import a as x", ("a",)),
        ("import a.b as x", ("a.b",)),
        ("import a, b", ("a", "b")),
        ("from a import b", ("a",)),
        ("from a.b import c", ("a.b",)),
        ("from a import (b, c)", ("a",)),
        ("from a import *", ("a",)),
        ("", ()),
        ("x = 1", ()),
    ],
)
def test_every_import_form(source: str, expected: tuple[str, ...]) -> None:
    assert modules.find_imports(source) == expected


def test_only_the_name_as_written() -> None:
    # The hub looks up a dotted name whole, so a.b is one module and there is
    # no module "a" to go looking for.
    assert modules.find_imports("from a.b.c import d") == ("a.b.c",)


def test_imports_are_in_order_and_deduplicated() -> None:
    source = "import b\nimport a\nimport b\nfrom a import thing\n"
    assert modules.find_imports(source) == ("b", "a")


def test_an_import_inside_a_function_counts() -> None:
    # Deferred imports are a real pattern, and the module still has to be
    # packed or the call fails on the hub rather than at compile time.
    source = "def go():\n    import helpers\n    return helpers\n"
    assert modules.find_imports(source) == ("helpers",)


def test_conditional_imports_all_count() -> None:
    source = "if x:\n    import a\nelse:\n    import b\n"
    assert modules.find_imports(source) == ("a", "b")


@pytest.mark.parametrize(
    "source",
    [
        "from . import x",
        "from .helpers import spin",
        "from .. import x",
        "from ...pkg.mod import x",
    ],
)
def test_relative_imports_are_refused(source: str) -> None:
    # The firmware raises NotImplementedError for any import with a level
    # above zero, so it is refused here where the line can be named.
    with pytest.raises(modules.RelativeImportError):
        modules.find_imports(source)


def test_a_refused_import_says_where_it_is() -> None:
    source = "import a\n\nfrom .helpers import spin\n"

    with pytest.raises(modules.RelativeImportError) as caught:
        modules.find_imports(source, path=Path("demo.py"))

    assert caught.value.lineno == 3
    assert "demo.py" in str(caught.value)


def test_source_that_does_not_parse_finds_nothing() -> None:
    # The compiler is about to be handed the same source and says which line
    # is wrong, which is a better message than anything possible here.
    assert modules.find_imports("def (:\n") == ()


# ----------------------------------------------------------------- resolving


@pytest.mark.anyio
async def test_resolve_finds_a_plain_module(tmp_path: Path) -> None:
    write(tmp_path / "helpers.py", "")
    assert await modules.resolve("helpers", [tmp_path]) == tmp_path / "helpers.py"


@pytest.mark.anyio
async def test_resolve_turns_dots_into_directories(tmp_path: Path) -> None:
    write(tmp_path / "robot" / "motors.py", "")
    assert (
        await modules.resolve("robot.motors", [tmp_path])
        == tmp_path / "robot" / "motors.py"
    )


@pytest.mark.anyio
async def test_resolve_needs_no_init_file(tmp_path: Path) -> None:
    # The hub has no notion of a package, so an __init__.py would mean nothing
    # to either side. This is the whole reason modulefinder is not used.
    write(tmp_path / "robot" / "motors.py", "")
    assert not (tmp_path / "robot" / "__init__.py").exists()
    assert await modules.resolve("robot.motors", [tmp_path]) is not None


@pytest.mark.anyio
async def test_resolve_finds_a_precompiled_module(tmp_path: Path) -> None:
    (tmp_path / "native.mpy").write_bytes(b"M\x06\x00\x1f")
    assert await modules.resolve("native", [tmp_path]) == tmp_path / "native.mpy"


@pytest.mark.anyio
async def test_source_wins_over_a_stale_precompiled_module(tmp_path: Path) -> None:
    write(tmp_path / "helpers.py", "")
    (tmp_path / "helpers.mpy").write_bytes(b"M\x06\x00\x1f")
    assert await modules.resolve("helpers", [tmp_path]) == tmp_path / "helpers.py"


@pytest.mark.anyio
async def test_resolve_gives_nothing_for_a_firmware_module(tmp_path: Path) -> None:
    # pybricks.hubs is on the hub already. Not finding it is the normal case.
    assert await modules.resolve("pybricks.hubs", [tmp_path]) is None


@pytest.mark.anyio
async def test_resolve_ignores_a_directory_of_the_right_name(tmp_path: Path) -> None:
    (tmp_path / "robot").mkdir()
    assert await modules.resolve("robot", [tmp_path]) is None


# -------------------------------------------------------------- entry naming


def test_entry_at_the_root_is_named_by_its_stem(tmp_path: Path) -> None:
    entry = write(tmp_path / "demo.py", "")
    assert modules.entry_name(entry, [tmp_path]) == "demo"


def test_entry_inside_a_package_gets_its_dotted_name(tmp_path: Path) -> None:
    entry = write(tmp_path / "src" / "robot" / "demo.py", "")
    assert modules.entry_name(entry, [tmp_path / "src"]) == "robot.demo"


def test_entry_outside_the_root_keeps_its_stem(tmp_path: Path) -> None:
    # There is no dotted name for it, and nothing can import it anyway.
    entry = write(tmp_path / "scratch" / "demo.py", "")
    root = tmp_path / "src"
    root.mkdir()
    assert modules.entry_name(entry, [root]) == "demo"


def test_a_stem_that_is_not_an_identifier_is_still_a_name(tmp_path: Path) -> None:
    # Nothing imports the entry point, so its name only has to be something
    # the hub can show.
    entry = write(tmp_path / "2048.py", "")
    assert modules.entry_name(entry, [tmp_path]) == "2048"


# ----------------------------------------------------------------- discovery


@pytest.mark.anyio
async def test_one_file_is_one_module(tmp_path: Path) -> None:
    entry = write(tmp_path / "demo.py", "from pybricks.hubs import ThisHub\n")
    found = await modules.discover(entry, [tmp_path])
    assert found == (modules.Module("demo", entry, precompiled=False),)


@pytest.mark.anyio
async def test_the_entry_module_comes_first(tmp_path: Path) -> None:
    # Position is what the firmware uses to pick the program to run, so this
    # is a correctness property rather than a tidy detail.
    entry = write(tmp_path / "demo.py", "import aaa\n")
    write(tmp_path / "aaa.py", "")
    found = await modules.discover(entry, [tmp_path])
    assert [module.name for module in found] == ["demo", "aaa"]


@pytest.mark.anyio
async def test_imports_of_imports_are_found(tmp_path: Path) -> None:
    entry = write(tmp_path / "demo.py", "import one\n")
    write(tmp_path / "one.py", "import two\n")
    write(tmp_path / "two.py", "import three\n")
    write(tmp_path / "three.py", "")
    found = await modules.discover(entry, [tmp_path])
    assert [module.name for module in found] == ["demo", "one", "two", "three"]


@pytest.mark.anyio
async def test_a_cycle_terminates(tmp_path: Path) -> None:
    entry = write(tmp_path / "demo.py", "import one\n")
    write(tmp_path / "one.py", "import two\n")
    write(tmp_path / "two.py", "import one\n")
    found = await modules.discover(entry, [tmp_path])
    assert [module.name for module in found] == ["demo", "one", "two"]


@pytest.mark.anyio
async def test_firmware_modules_are_left_to_the_firmware(tmp_path: Path) -> None:
    entry = write(
        tmp_path / "demo.py",
        "from pybricks.hubs import ThisHub\nimport umath\nimport helpers\n",
    )
    write(tmp_path / "helpers.py", "")
    found = await modules.discover(entry, [tmp_path])
    assert [module.name for module in found] == ["demo", "helpers"]


@pytest.mark.anyio
async def test_a_package_needs_no_init_file(tmp_path: Path) -> None:
    entry = write(tmp_path / "demo.py", "from robot.motors import spin\n")
    write(tmp_path / "robot" / "motors.py", "")
    found = await modules.discover(entry, [tmp_path])
    assert [module.name for module in found] == ["demo", "robot.motors"]


@pytest.mark.anyio
async def test_the_program_is_not_packed_twice(tmp_path: Path) -> None:
    # A module importing the program by name resolves to the entry's own
    # file. Packing it again would give two modules of the same name, and
    # importing it would re-execute the program.
    entry = write(tmp_path / "demo.py", "import helpers\n")
    write(tmp_path / "helpers.py", "import demo\n")
    found = await modules.discover(entry, [tmp_path])
    assert [module.name for module in found] == ["demo", "helpers"]


@pytest.mark.anyio
async def test_a_precompiled_module_is_a_leaf(tmp_path: Path) -> None:
    entry = write(tmp_path / "demo.py", "import native\n")
    (tmp_path / "native.mpy").write_bytes(b"M\x06\x00\x1f")
    found = await modules.discover(entry, [tmp_path])
    assert found[1].precompiled is True
    assert found[1].path.suffix == ".mpy"


@pytest.mark.anyio
async def test_a_relative_import_in_a_module_is_refused(tmp_path: Path) -> None:
    entry = write(tmp_path / "demo.py", "import robot.motors\n")
    write(tmp_path / "robot" / "motors.py", "from . import wheels\n")

    with pytest.raises(modules.RelativeImportError) as caught:
        await modules.discover(entry, [tmp_path])

    assert caught.value.path == tmp_path / "robot" / "motors.py"


@pytest.mark.anyio
async def test_a_root_above_the_script_gives_dotted_names(tmp_path: Path) -> None:
    entry = write(tmp_path / "src" / "robot" / "demo.py", "import robot.motors\n")
    write(tmp_path / "src" / "robot" / "motors.py", "")
    found = await modules.discover(entry, [tmp_path / "src"])
    assert [module.name for module in found] == ["robot.demo", "robot.motors"]


@pytest.mark.anyio
async def test_a_missing_script_says_so(tmp_path: Path) -> None:
    with pytest.raises(OSError):
        await modules.discover(tmp_path / "nope.py", [tmp_path])


@pytest.mark.anyio
async def test_more_than_one_root_is_tried_in_order(tmp_path: Path) -> None:
    # The case that made roots plural: a project with a src layout, and a
    # script kept outside it whose helper sits beside it. With src as the only
    # root the helper is silently left out, and the program fails on the hub.
    entry = write(tmp_path / "scratch" / "demo.py", "import helpers\nimport shared\n")
    write(tmp_path / "scratch" / "helpers.py", "")
    write(tmp_path / "src" / "shared.py", "")

    found = await modules.discover(entry, [tmp_path / "scratch", tmp_path / "src"])

    assert [module.name for module in found] == ["demo", "helpers", "shared"]


@pytest.mark.anyio
async def test_the_first_root_wins_a_module_two_of_them_have(tmp_path: Path) -> None:
    write(tmp_path / "a" / "helpers.py", "")
    write(tmp_path / "b" / "helpers.py", "")
    entry = write(tmp_path / "b" / "demo.py", "import helpers\n")

    found = await modules.discover(entry, [tmp_path / "b", tmp_path / "a"])

    assert found[1].path == tmp_path / "b" / "helpers.py"


def test_the_entry_gets_the_most_qualified_name_available(tmp_path: Path) -> None:
    # Both roots contain it. The dotted name is what another module would have
    # to write to import the program, and the more informative thing for the
    # hub to show -- so it wins over the stem whichever order they are in.
    entry = write(tmp_path / "src" / "robot" / "demo.py", "")
    inner = tmp_path / "src" / "robot"
    outer = tmp_path / "src"
    assert modules.entry_name(entry, [inner, outer]) == "robot.demo"
    assert modules.entry_name(entry, [outer, inner]) == "robot.demo"
