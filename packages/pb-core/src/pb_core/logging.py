# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""Logging setup shared by the daemon, the library and the CLI.

This code talks to physical hubs over BLE, where failures are intermittent and
hard to reproduce, so a trace after the fact is often the only diagnostic
available. Logging is therefore pervasive by design rather than an afterthought.

Two conventions matter:

* Only entry points call :func:`setup_logging`. Libraries never install
  handlers; they only ever call ``logging.getLogger(__name__)``.
* Context such as the hub id is attached with :func:`log_context` rather than
  threaded through call signatures. It is stored in :mod:`contextvars`, which
  composes correctly with anyio task groups: a task started with ``start_soon``
  inherits a copy of the context, so its records carry the right ids
  automatically.
"""

from __future__ import annotations

import contextlib
import logging
import logging.handlers
import os
import platform
import sys
from collections.abc import Generator, Mapping, Sequence
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Final, Literal, TextIO

__all__ = [
    "TRACE",
    "LogStyle",
    "elide",
    "log_context",
    "setup_logging",
]

TRACE: Final = 5
"""Level below ``DEBUG`` used for wire traffic: JSON-RPC frames and GATT bytes.

Separate from ``DEBUG`` because it is voluminous enough to drown everything
else, and is normally enabled only for one logger at a time.
"""

LogStyle = Literal["rich", "plain", "file", "null"]
"""How log records should be rendered. See :func:`setup_logging`."""

_CONTEXT: ContextVar[Mapping[str, object]] = ContextVar("pb_log_context")

_FORMAT = "%(asctime)s.%(msecs)03d %(levelname)-5s %(name)-28s %(context)s%(message)s"
_DATEFMT = "%H:%M:%S"

_MAX_TRACE_CHARS: Final = 512

logging.addLevelName(TRACE, "TRACE")


class _ContextFilter(logging.Filter):
    """Inject the current :func:`log_context` fields into every record."""

    def filter(self, record: logging.LogRecord) -> bool:
        fields = _CONTEXT.get({})
        record.context = (
            "[" + " ".join(f"{k}={v}" for k, v in fields.items()) + "] "
            if fields
            else ""
        )
        return True


@contextlib.contextmanager
def log_context(**fields: object) -> Generator[None, None, None]:
    """Attach ``fields`` to every log record emitted inside this block.

    Nested calls merge rather than replace, so an inner ``hub`` context adds to
    an outer ``client`` context.

    Args:
        **fields: Key/value pairs to attach, for example ``hub="ble:AA:BB"``.

    Yields:
        ``None``. The context is restored on exit.
    """
    merged = {**_CONTEXT.get({}), **fields}
    token = _CONTEXT.set(merged)
    try:
        yield
    finally:
        _CONTEXT.reset(token)


def elide(data: object, limit: int = _MAX_TRACE_CHARS) -> str:
    """Render ``data`` for a trace log, truncating it to ``limit`` characters.

    Wire tracing would otherwise format entire program downloads and every byte
    of a chatty program's output.

    Args:
        data: The value to render.
        limit: Maximum number of characters to keep.

    Returns:
        The rendered value, suffixed with an elision marker if truncated.
    """
    text = data if isinstance(data, str) else repr(data)
    if len(text) <= limit:
        return text
    return f"{text[:limit]}... ({len(text)} chars)"


def _make_handler(
    style: LogStyle, stream: TextIO | None, log_file: Path | None
) -> logging.Handler:
    """Build the single handler for ``style``."""
    if style == "null":
        return logging.NullHandler()

    if style == "file":
        if log_file is None:
            raise ValueError("style='file' requires log_file")
        log_file.parent.mkdir(parents=True, exist_ok=True)
        return logging.handlers.RotatingFileHandler(
            log_file, maxBytes=2 * 1024 * 1024, backupCount=5, encoding="utf-8"
        )

    if style == "rich":
        # Imported lazily: rich is a CLI dependency, not a pb-core one.
        from rich.console import Console
        from rich.logging import RichHandler

        return RichHandler(
            console=Console(stderr=True),
            rich_tracebacks=True,
            show_path=False,
            markup=False,
        )

    return logging.StreamHandler(stream if stream is not None else sys.stderr)


def setup_logging(
    *,
    level: int = logging.WARNING,
    style: LogStyle = "plain",
    stream: TextIO | None = None,
    log_file: Path | None = None,
    quiet_loggers: Sequence[str] = ("bleak", "asyncio"),
    quiet_level: int | None = None,
) -> logging.Handler:
    """Install a single log handler on the root logger.

    All output goes to stderr (or a file), never stdout, so that piping data out
    of the CLI stays clean.

    Args:
        level: Level for this project's loggers.
        style: ``"rich"`` for a TTY, ``"plain"`` when piped, ``"file"`` for the
            daemon, ``"null"`` to discard.
        stream: Override the stream for ``"plain"``. Defaults to stderr.
        log_file: Destination for ``"file"``.
        quiet_loggers: Third-party loggers to hold at a higher level, so that
            turning on debug logging here does not drown the output in BLE
            chatter.
        quiet_level: Level for ``quiet_loggers``. Defaults to one step above
            ``level``.

    Returns:
        The installed handler, so that callers can remove it again.
    """
    handler = _make_handler(style, stream, log_file)
    handler.addFilter(_ContextFilter())
    if not isinstance(handler, logging.NullHandler):
        handler.setFormatter(logging.Formatter(_FORMAT, datefmt=_DATEFMT))

    root = logging.getLogger()
    root.setLevel(level)
    root.addHandler(handler)

    effective_quiet = (
        quiet_level if quiet_level is not None else min(level + 10, logging.CRITICAL)
    )
    for name in quiet_loggers:
        logging.getLogger(name).setLevel(effective_quiet)

    return handler


def log_startup_banner(
    logger: logging.Logger, name: str, version: str, **extra: Any
) -> None:
    """Log the environment facts needed to interpret the rest of a log.

    The machine architecture is included deliberately: several known issues are
    specific to Windows on ARM64, and having it in the log turns a mystery
    report into an obvious one.

    Args:
        logger: Logger to write to.
        name: Program name.
        version: Program version.
        **extra: Additional key/value pairs to log.
    """
    fields = {
        "pid": os.getpid(),
        "python": platform.python_version(),
        "platform": sys.platform,
        "machine": platform.machine(),
        **extra,
    }
    logger.info(
        "%s %s starting (%s)",
        name,
        version,
        " ".join(f"{k}={v}" for k, v in fields.items()),
    )
