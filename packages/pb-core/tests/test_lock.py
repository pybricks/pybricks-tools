# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from pb_core.lock import AlreadyRunningError, single_instance


def test_lock_is_exclusive_within_a_process(tmp_path: Path) -> None:
    lock = tmp_path / "pb-service.lock"
    with (
        single_instance(lock),
        pytest.raises(AlreadyRunningError),
        single_instance(lock),
    ):
        pass


def test_lock_is_released_on_exit(tmp_path: Path) -> None:
    lock = tmp_path / "pb-service.lock"
    with single_instance(lock):
        pass
    with single_instance(lock):
        pass  # must be reacquirable


def test_lock_records_the_holding_pid(tmp_path: Path) -> None:
    # On Windows msvcrt takes a mandatory lock, so byte 0 cannot be read while
    # it is held. That is exactly why byte 0 carries no information and the pid
    # starts at offset 1: diagnostics stay readable on every platform.
    lock = tmp_path / "pb-service.lock"
    with single_instance(lock), lock.open("r", encoding="ascii") as handle:
        handle.seek(1)
        assert handle.read().strip() == str(os.getpid())


def test_lock_excludes_another_process(tmp_path: Path) -> None:
    # The case that actually matters: two daemons started at once by two
    # clients racing. Within one process the check is nearly free; across
    # processes it is the kernel doing the work.
    lock = tmp_path / "pb-service.lock"
    script = textwrap.dedent(f"""
        import sys
        from pathlib import Path
        from pb_core.lock import AlreadyRunningError, single_instance
        try:
            with single_instance(Path({str(lock)!r})):
                print("acquired")
        except AlreadyRunningError:
            print("refused")
    """)
    with single_instance(lock):
        result = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True, check=True
        )
    assert result.stdout.strip() == "refused"


def test_lock_is_freed_when_the_holder_dies(tmp_path: Path) -> None:
    # A PID file would need validating against a possibly-reused pid; an
    # advisory lock is released by the kernel however the process exits.
    lock = tmp_path / "pb-service.lock"
    script = textwrap.dedent(f"""
        from pathlib import Path
        from pb_core.lock import single_instance
        with single_instance(Path({str(lock)!r})):
            pass
    """)
    subprocess.run([sys.executable, "-c", script], check=True, capture_output=True)
    with single_instance(lock):
        pass
