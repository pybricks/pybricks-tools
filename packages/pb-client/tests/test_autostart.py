# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""How the background service is started."""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest

from pb_client import autostart
from pb_core.ipc import Endpoint

windows_only = pytest.mark.skipif(
    sys.platform != "win32", reason="console windows are a Windows problem"
)

_PROBE = """
import ctypes, pathlib, sys

# GetConsoleWindow returns the console window this process is attached to, or
# NULL when it has none. Anything non-zero is a window a user would see.
handle = ctypes.windll.kernel32.GetConsoleWindow()
pathlib.Path(sys.argv[1]).write_text(str(handle), encoding="utf-8")
"""


@windows_only
def test_the_service_starts_without_a_console_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A window flashing up on every command is the thing being prevented.

    The property is measured rather than asserted about the flags, because the
    flags do not mean what they appear to: DETACHED_PROCESS reads like the way
    to start something quietly and in fact gets a visible console allocated for
    it.

    ``PB_SERVICE_CMD`` puts a probe where the service would go, so this exercises
    the real spawn path. The probe ignores the ``--endpoint`` argument that
    :func:`spawn_service` appends.
    """
    script = tmp_path / "probe.py"
    script.write_text(_PROBE, encoding="utf-8")
    report = tmp_path / "console.txt"

    command = subprocess.list2cmdline([sys.executable, str(script), str(report)])
    monkeypatch.setenv(autostart.SERVICE_COMMAND_ENV_VAR, command)
    autostart.spawn_service(Endpoint("memory", "unused"))

    deadline = time.monotonic() + 30

    while not report.exists() and time.monotonic() < deadline:
        time.sleep(0.02)

    assert report.exists(), "the probe never ran"
    assert report.read_text(encoding="utf-8") == "0", (
        "the service was started with a console window attached"
    )


_CWD_PROBE = """
import os, pathlib, sys

pathlib.Path(sys.argv[1]).write_text(os.getcwd(), encoding="utf-8")
"""


def _run_probe(source: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """Put a probe where the service would go and return what it wrote."""
    script = tmp_path / "probe.py"
    script.write_text(source, encoding="utf-8")
    report = tmp_path / "report.txt"

    command = subprocess.list2cmdline([sys.executable, str(script), str(report)])
    monkeypatch.setenv(autostart.SERVICE_COMMAND_ENV_VAR, command)
    autostart.spawn_service(Endpoint("memory", "unused"))

    deadline = time.monotonic() + 30

    while not report.exists() and time.monotonic() < deadline:
        time.sleep(0.02)

    assert report.exists(), "the probe never ran"
    return report.read_text(encoding="utf-8")


def test_the_service_does_not_hold_the_directory_it_was_started_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A running process holds its working directory.

    The service outlives the shell that started it by design, so inheriting
    that shell's directory means the directory cannot be renamed or deleted
    for as long as the service runs -- and on Windows that is enforced rather
    than merely untidy. It matters more now that ``pb -C`` exists, since the
    directory handed over would be one the user named.

    Measured through the real spawn path rather than asserted about the
    arguments, for the same reason the console-window test is.
    """
    started_in = tmp_path / "project"
    started_in.mkdir()
    monkeypatch.chdir(started_in)

    where = _run_probe(_CWD_PROBE, tmp_path, monkeypatch)

    assert Path(where) != started_in
    assert Path(where) == autostart.neutral_directory()
