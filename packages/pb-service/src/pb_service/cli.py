# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""The service's command line.

Plain argparse rather than the CLI framework used by ``pb``: the service must not
depend on the presentation layer, and it is not a program people interact with
directly.
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
from typing import Any

import anyio

# Imported for the side effect: importing a method module registers its RPCs,
# so nothing here reads the names. Said on each line, because a comment is
# not something a linter can read.
import pb_service.methods.hub  # pyright: ignore[reportUnusedImport]
import pb_service.methods.program  # pyright: ignore[reportUnusedImport]
import pb_service.methods.service  # pyright: ignore[reportUnusedImport]
import pb_service.methods.stdio  # noqa: F401 # pyright: ignore[reportUnusedImport]
from pb_core import ipc, lock
from pb_core.ipc import AddressInUseError
from pb_core.logging import setup_logging
from pb_core.paths import log_file
from pb_service import __version__
from pb_service import service as service_module

logger = logging.getLogger(__name__)

__all__ = ["main"]

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_ALREADY_RUNNING = 3

_LEVELS = {
    "trace": 5,
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pb-service",
        description="The Pybricks service. Normally started automatically.",
    )
    parser.add_argument(
        "--version", action="version", version=f"pb-service {__version__}"
    )
    parser.add_argument(
        "--endpoint",
        help="Where to listen, for example tcp://127.0.0.1:47823. "
        "Defaults to the platform's socket or named pipe.",
    )
    parser.add_argument(
        "--foreground",
        action="store_true",
        help="Log to stderr instead of only to the log file.",
    )
    parser.add_argument(
        "--log-level",
        choices=sorted(_LEVELS),
        default="info",
        help="Verbosity (default: info).",
    )
    parser.add_argument(
        "--idle-timeout",
        type=float,
        default=service_module.DEFAULT_IDLE_TIMEOUT,
        help="Seconds with no clients and no hubs before exiting. "
        "Use 0 to run until stopped.",
    )
    return parser


async def _watch_signals(service: service_module.Service) -> None:
    """Turn termination signals into an orderly shutdown.

    On Windows ``anyio.open_signal_receiver`` is unavailable, because the
    proactor loop has no ``add_signal_handler``. A plain handler that pokes the
    loop is used instead, and it needs nothing else to make it prompt: asyncio
    sets the signal wakeup socket, so the byte written by the C-level handler
    -- from the thread Windows spawns to deliver a console control event --
    completes the loop's own overlapped read and the wait returns at once.
    Measured on Windows 11 against a loop parked in ``sleep_forever``: the
    handler ran 0.7 ms after the event was sent, with nothing else waking the
    loop.
    """

    if sys.platform == "win32":
        import asyncio

        loop = asyncio.get_running_loop()
        event = anyio.Event()

        def handler(signum: int, frame: Any) -> None:
            loop.call_soon_threadsafe(event.set)

        previous = [
            (sig, signal.signal(sig, handler))
            for sig in (signal.SIGINT, signal.SIGBREAK)
        ]

        try:
            await event.wait()
            service.request_shutdown("signal")
        finally:
            for sig, old in previous:
                signal.signal(sig, old)

        return

    with anyio.open_signal_receiver(signal.SIGINT, signal.SIGTERM) as signals:
        async for _ in signals:
            service.request_shutdown("signal")
            return


async def _serve(args: argparse.Namespace) -> int:
    endpoint = ipc.resolve(args.endpoint)
    service_module.log_banner(endpoint, __version__)

    try:
        async with service_module.run_service(
            endpoint, idle_timeout=args.idle_timeout
        ) as service:
            async with anyio.create_task_group() as tg:
                tg.start_soon(_watch_signals, service)
                await service.wait_for_shutdown()
                tg.cancel_scope.cancel()

            logger.info("shutting down: %s", service.shutdown_reason)
    except AddressInUseError:
        # The common case is two clients racing to start a service. One wins,
        # and both clients then connect to it, so this is not an error.
        logger.info("another service is already listening on %s", endpoint)
        return EXIT_ALREADY_RUNNING

    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    """Run the service.

    Args:
        argv: Command line arguments, defaulting to ``sys.argv``.

    Returns:
        The process exit code.
    """
    args = _parser().parse_args(argv)
    level = _LEVELS[args.log_level]

    setup_logging(
        level=level,
        style="plain" if args.foreground else "file",
        log_file=None if args.foreground else log_file(),
    )

    # The first thing written, before anything that could fail or block.
    # A process that gets no further than here still leaves evidence that
    # it existed, which is exactly what a leftover service with nothing in
    # the log did not.
    logger.info(
        "pb-service starting (pid=%d, argv=%s)",
        os.getpid(),
        argv if argv is not None else sys.argv[1:],
    )

    try:
        with lock.single_instance():
            return anyio.run(_serve, args, backend="asyncio")
    except lock.AlreadyRunningError:
        logger.info("another service already holds the lock; exiting")
        return EXIT_ALREADY_RUNNING
    except Exception:
        logger.exception("service failed")
        return EXIT_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
