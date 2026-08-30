# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

import sys
from pathlib import Path

import pytest

from pb_core import paths


def test_all_runtime_files_share_one_directory() -> None:
    parent = paths.runtime_dir()
    assert paths.socket_path().parent == parent
    assert paths.lock_file().parent == parent
    assert paths.cookie_file().parent == parent


@pytest.mark.skipif(sys.platform == "win32", reason="AF_UNIX path limit is POSIX-only")
def test_socket_path_fits_in_sun_path() -> None:
    # sockaddr_un.sun_path is 104 bytes on macOS and 108 on Linux. Exceeding it
    # fails at bind() time with a confusing error, so keep well clear.
    assert len(str(paths.socket_path()).encode()) < 100


@pytest.mark.skipif(sys.platform != "linux", reason="XDG is Linux-only")
def test_runtime_dir_honours_xdg(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    assert paths.runtime_dir() == tmp_path / paths.APP_NAME


@pytest.mark.skipif(sys.platform != "linux", reason="XDG is Linux-only")
def test_runtime_dir_falls_back_without_xdg(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    assert paths.runtime_dir().name.startswith(f"{paths.APP_NAME}-")


@pytest.mark.skipif(sys.platform != "win32", reason="named pipes are Windows-only")
def test_windows_pipe_name_is_well_formed() -> None:
    name = paths.windows_pipe_name()
    assert name.startswith(r"\.\pipe\\".rstrip("\\"))
    # Pipe names are machine-global, so they must be per-user.
    assert paths.APP_NAME in name


def test_ensure_private_dir_creates_and_is_idempotent(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "runtime"
    assert paths.ensure_private_dir(target) == target
    assert target.is_dir()
    paths.ensure_private_dir(target)  # must not raise


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX mode bits")
def test_ensure_private_dir_is_private(tmp_path: Path) -> None:
    target = paths.ensure_private_dir(tmp_path / "runtime")
    assert target.stat().st_mode & 0o777 == 0o700


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX mode bits")
def test_ensure_private_dir_rejects_world_readable(tmp_path: Path) -> None:
    # Another user pre-creating the directory in a shared /tmp is the attack
    # this guards against.
    target = tmp_path / "runtime"
    target.mkdir(mode=0o755)
    target.chmod(0o755)
    with pytest.raises(PermissionError, match="accessible to other users"):
        paths.ensure_private_dir(target)


@pytest.mark.skipif(sys.platform != "linux", reason="XDG is Linux-only")
def test_state_dir_honours_xdg(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # What the service remembers between runs -- which hub the user chose --
    # is state rather than cache or config, so XDG_STATE_HOME is where the
    # spec puts it.
    monkeypatch.delenv(paths.STATE_DIR_ENV_VAR, raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    assert paths.state_dir() == tmp_path / paths.APP_NAME


def test_state_dir_can_be_moved(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # So a test never rewrites the choice of whoever is running it.
    monkeypatch.setenv(paths.STATE_DIR_ENV_VAR, str(tmp_path))
    assert paths.state_dir() == tmp_path
