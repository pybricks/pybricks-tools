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

pytestmark = pytest.mark.skipif(
    sys.platform != "win32", reason="console windows are a Windows problem"
)

_PROBE = """
import ctypes, pathlib, sys

# GetConsoleWindow returns the console window this process is attached to, or
# NULL when it has none. Anything non-zero is a window a user would see.
handle = ctypes.windll.kernel32.GetConsoleWindow()
pathlib.Path(sys.argv[1]).write_text(str(handle), encoding="utf-8")
"""


def test_the_service_starts_without_a_console_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A window flashing up on every command is the thing being prevented.

    The property is measured rather than asserted about the flags, because the
    flags do not mean what they appear to: DETACHED_PROCESS reads like the way
    to start something quietly and in fact gets a visible console allocated for
    it.

    ``PB_DAEMON_CMD`` puts a probe where the daemon would go, so this exercises
    the real spawn path. The probe ignores the ``--endpoint`` argument that
    :func:`spawn_daemon` appends.
    """
    script = tmp_path / "probe.py"
    script.write_text(_PROBE, encoding="utf-8")
    report = tmp_path / "console.txt"

    command = subprocess.list2cmdline([sys.executable, str(script), str(report)])
    monkeypatch.setenv(autostart.DAEMON_COMMAND_ENV_VAR, command)
    autostart.spawn_daemon(Endpoint("memory", "unused"))

    deadline = time.monotonic() + 30
    while not report.exists() and time.monotonic() < deadline:
        time.sleep(0.02)

    assert report.exists(), "the probe never ran"
    assert report.read_text(encoding="utf-8") == "0", (
        "the service was started with a console window attached"
    )
