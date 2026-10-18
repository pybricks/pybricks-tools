# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""Turning a failure into something the user can act on.

There are two kinds of failure and they are presented differently.

An **expected** failure -- the hub is switched off, nothing is connected yet,
the program is too big -- is something the user can do something about. It is
reported as one plain sentence saying what happened, plus, where there is one, a
suggestion of how to fix it. A traceback would only bury the sentence that
matters. The wording follows Pybricks Code, which pairs every alert with a
``suggestion`` for the same reason; where it has already phrased one of these
well, its phrasing is reused so that the two tools tell users the same thing.

An **unexpected** failure is a bug in this tool. Nothing is rewritten and
nothing is swallowed: the original exception is re-raised and Python prints the
whole traceback, which is the most useful thing that can be shown, both to the
user and to whoever reads the bug report afterwards.

The reason this module has to exist at all is ``ExceptionGroup``. A task group
wraps whatever its body raised, so ``except JsonRpcError`` around an
``async with pb_client.connect()`` block quietly stops matching -- the error
arrives as a group of one. Everything here therefore starts by flattening.

Dispatch is keyed on the JSON-RPC error *code*, never on message text, so the
service's wording and the wording here stay independently editable and rephrasing
a service message cannot silently turn a friendly error back into a traceback.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import TypeVar, TypeVarTuple, cast

import anyio
from rich.console import Console

import pb_client
from pb_core.ipc import ENDPOINT_ENV_VAR, EndpointError
from pb_core.jsonrpc import ErrorCode, JsonRpcError, PeerClosedError

logger = logging.getLogger(__name__)

__all__ = [
    "EXIT_ERROR",
    "EXIT_HUB_ERROR",
    "EXIT_INTERRUPTED",
    "EXIT_NOTHING_CONNECTED",
    "EXIT_OK",
    "Problem",
    "explain",
    "find",
    "flatten",
    "render",
    "run",
    "serial_port_advice",
]

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_HUB_ERROR = 3
EXIT_NOTHING_CONNECTED = 4
EXIT_INTERRUPTED = 130

PosArgsT = TypeVarTuple("PosArgsT")
ExcT = TypeVar("ExcT", bound=BaseException)


@dataclass(frozen=True)
class Problem:
    """One failure, as the user should see it."""

    message: str
    """What went wrong, as a sentence."""

    hint: str | None = None
    """How to fix it, if there is anything useful to say."""

    detail: str = ""
    """Verbatim output to reproduce underneath, such as compiler diagnostics."""

    exit_code: int = EXIT_ERROR
    """What the process should exit with."""


@dataclass(frozen=True)
class _Rule:
    """How to present one JSON-RPC error code."""

    hint: str | None = None
    exit_code: int = EXIT_HUB_ERROR
    message: str | None = None
    """Replaces the service's own wording. ``None`` keeps it."""


_STALE = (
    "The background service is left over from an older install. "
    "Run [bold]pb service stop[/] and try again."
)

_OLD_CLIENT = (
    "Upgrade [bold]pb[/], or run [bold]pb service stop[/] to replace the "
    "service with one this version can talk to."
)
"""What to suggest when it is this copy of pb that is behind.

The service outlives the install that started it, so it is usually the
older of the two -- but not always, and telling somebody to stop the
service when their own pb is the old half sends them somewhere that
cannot help.
"""


def serial_port_advice(platform: str = sys.platform) -> str:
    """Say how to get at a serial port that would not open.

    The same refusal means quite different things on different systems, and the
    difference is what decides the advice, so this is one suggestion per
    platform rather than one covering every case at once.

    Args:
        platform: What to advise for. An argument, defaulted, so that the
            wording a user on another platform will see can be read back here
            rather than only on that platform.

    Returns:
        What to suggest.
    """
    if platform == "win32":
        # Windows has no permissions to grant on a COM port: EACCES here is
        # almost always another program holding it open.
        return (
            "Close anything else using the hub -- Pybricks Code, a serial "
            "terminal -- and try again."
        )

    if platform == "darwin":
        return "Unplug the hub and plug it in again, then try again."

    # Linux, and near enough for the BSDs: the device node belongs to a group,
    # and the fix is joining it. Stopping the service matters and is easy to
    # miss -- a service already running keeps the groups it started with, so
    # the hub stays out of reach until it is replaced, however many times the
    # user logs out and back in.
    return (
        "Add yourself to the [bold]dialout[/] group with [bold]sudo usermod "
        "-a -G dialout $USER[/], then log out and back in. If you have just "
        "done that, run [bold]pb service stop[/] as well."
    )


_RULES: Mapping[int, _Rule] = {
    # Hubs. The service's own wording is specific -- "no connected hub named
    # 'Spare Hub'" -- so it is usually kept as the headline and only the
    # suggestion is added here.
    ErrorCode.HUB_NOT_FOUND: _Rule(
        "Run [bold]pb connect[/] first, or [bold]pb list[/] to see what is connected."
    ),
    ErrorCode.HUB_NOT_CONNECTED: _Rule(
        "Check the hub is switched on and in range, then try again."
    ),
    ErrorCode.HUB_ALREADY_CONNECTED: _Rule(
        "It is connected already -- [bold]pb list[/] shows it."
    ),
    ErrorCode.HUB_DISCONNECTED: _Rule(
        "Run [bold]pb connect[/] to reconnect, then try again.",
        message="The hub is no longer connected.",
    ),
    ErrorCode.HUB_BUSY: _Rule(
        "Wait a moment, or stop what it is doing with [bold]pb stop[/]."
    ),
    ErrorCode.HUB_INVALID_COMMAND: _Rule(
        "If the hub's firmware is old, installing the latest version from "
        "code.pybricks.com may help."
    ),
    ErrorCode.HUB_UNSUPPORTED: _Rule(
        "This needs newer hub firmware. Install the latest version from "
        "code.pybricks.com."
    ),
    # Programs.
    ErrorCode.PROGRAM_SLOT_NOT_SELECTED: _Rule(
        "The hub chooses the slot, with the buttons on the hub itself. Select "
        "it there, or leave [bold]--slot[/] out to accept the one it has.",
    ),
    # A hub with one slot has no buttons to choose it with, so the suggestion
    # above would send the user looking for something that is not there.
    ErrorCode.PROGRAM_TOO_LARGE: _Rule(
        "Try removing unused code, or making names and strings smaller."
    ),
    ErrorCode.COMPILE_FAILED: _Rule(
        "Fix the error the compiler reported and try again.",
        exit_code=EXIT_ERROR,
    ),
    ErrorCode.POWER_BUTTON_PRESSED: _Rule(
        "You may release the button on the hub now.",
        message="The hub's button interrupted this.",
    ),
    # Transport and lifecycle.
    # The service repeats what the system said, which is what tells a radio
    # that is switched off from one this program may not use.
    ErrorCode.BLUETOOTH_UNAVAILABLE: _Rule(
        "Switch Bluetooth on, or plug in an adapter, and check that this "
        "computer lets pb use Bluetooth.",
        exit_code=EXIT_ERROR,
    ),
    ErrorCode.SCAN_TIMEOUT: _Rule(
        "Switch the hub on and press its button so that its light flashes."
    ),
    # The service names the port and repeats what the operating system said,
    # which is the half of this a user cannot guess; the suggestion is the
    # other half, and it is the only one here that depends on the platform.
    ErrorCode.SERIAL_PORT_UNAVAILABLE: _Rule(
        serial_port_advice(), exit_code=EXIT_ERROR
    ),
    ErrorCode.REQUEST_CANCELLED: _Rule(exit_code=EXIT_ERROR, message="Cancelled."),
    ErrorCode.SERVICE_SHUTTING_DOWN: _Rule(
        "Try again in a moment.",
        exit_code=EXIT_ERROR,
        message="The background service is shutting down.",
    ),
    # A stale service is the one failure a user cannot guess their way out of,
    # and it is likely whenever the tools are upgraded while a hub is being
    # held open, so all four of its shapes name the same fix.
    ErrorCode.NOT_AUTHENTICATED: _Rule(
        _STALE,
        exit_code=EXIT_ERROR,
        message="Could not sign in to the background service.",
    ),
    ErrorCode.HELLO_REQUIRED: _Rule(_STALE, exit_code=EXIT_ERROR),
    ErrorCode.UNSUPPORTED_PROTOCOL_VERSION: _Rule(_STALE, exit_code=EXIT_ERROR),
    ErrorCode.METHOD_NOT_FOUND: _Rule(
        _STALE,
        exit_code=EXIT_ERROR,
        message="The background service does not know that command.",
    ),
    # PARSE_ERROR, INVALID_REQUEST and INVALID_PARAMS are deliberately absent:
    # each means this CLI sent the service something it could not use, which no
    # user can act on and every developer wants a traceback for.
    ErrorCode.INTERNAL_ERROR: _Rule(
        "The service log has the details; [bold]pb service status[/] names the file.",
        exit_code=EXIT_ERROR,
        message="The background service hit an unexpected problem.",
    ),
}

_LOST_CONNECTION = (
    anyio.BrokenResourceError,
    anyio.ClosedResourceError,
    anyio.EndOfStream,
    PeerClosedError,
)
"""What a service that died mid-call looks like from here.

``PeerClosedError`` is the most literal of them: the connection ended with a
request still waiting for its answer. It belongs here rather than among the
unrecognised, where it printed a traceback under "this looks like a bug in pb"
-- which is the wrong thing to say about a service that simply went away.


Recognised, but usually only the wake of a more informative failure, so it is
dropped whenever anything else survived the flattening.
"""

_CANCELLED = asyncio.CancelledError
"""Cancellation, as the asyncio backend spells it.

Deliberately not ``anyio.get_cancelled_exc_class()``, which needs a running
event loop to answer: by the time a failure is being explained, ``anyio.run``
has already returned and asking would itself raise. The repository pins the
asyncio backend, so the answer is known statically anyway.
"""

_BUG = (
    "This looks like a bug in pb. Please report it, including everything below "
    "and what you were doing at the time."
)


def flatten(exc: BaseException) -> list[BaseException]:
    """Return the leaves of a possibly nested exception group.

    Args:
        exc: An exception, which may be an :exc:`ExceptionGroup`.

    Returns:
        Every non-group exception it contains, in order. A plain exception
        yields itself.
    """

    if not isinstance(exc, BaseExceptionGroup):
        return [exc]

    # The cast is only about generics: isinstance cannot tell pyright what a
    # group contains, and the answer is always "exceptions".
    group = cast("BaseExceptionGroup[BaseException]", exc)
    return [leaf for child in group.exceptions for leaf in flatten(child)]


def find(exc: BaseException, kind: type[ExcT]) -> ExcT | None:
    """Return the first leaf of ``exc`` that is an instance of ``kind``.

    Commands use this when they want to treat one particular failure as a
    normal outcome. Catching the exception type alone is not enough, because a
    task group will have wrapped it in a group.

    Args:
        exc: The exception to look inside.
        kind: The type being looked for.

    Returns:
        The matching exception, or ``None``.
    """

    for leaf in flatten(exc):
        if isinstance(leaf, kind):
            return leaf

    return None


def _sentence(text: str) -> str:
    """Render a wire message as a sentence."""
    text = text.strip()
    if not text:
        return "Something went wrong."

    text = text[0].upper() + text[1:]
    return text if text[-1] in ".!?" else text + "."


def _too_large(exc: JsonRpcError) -> str:
    """Say how much too big a program is, following Pybricks Code's wording."""
    payload: object = exc.data
    if not isinstance(payload, Mapping):
        return _sentence(exc.message)

    # Cast the original rather than the narrowed local: isinstance can say
    # that it is a mapping, but not of what.
    data = cast("Mapping[str, object]", exc.data)
    size, limit = data.get("size"), data.get("limit")
    if not isinstance(size, int) or not isinstance(limit, int):
        return _sentence(exc.message)

    return (
        f"This program is too big. Compiled size is {size} bytes "
        f"but the hub can only fit {limit} bytes."
    )


_ONE_SLOT = "Leave [bold]--slot[/] out, or ask for slot 0."


def _slot_hint(exc: JsonRpcError, default: str | None) -> str | None:
    """Suggest what to do about a slot, which depends on how many there are.

    Args:
        exc: The error the service sent.
        default: The suggestion for a hub with slots to choose between.

    Returns:
        The suggestion to print.
    """
    payload: object = exc.data
    if not isinstance(payload, Mapping):
        return default

    data = cast("Mapping[str, object]", exc.data)
    return _ONE_SLOT if data.get("slots") == 1 else default


def _describe_rpc(exc: JsonRpcError, hints: Mapping[int, str] | None) -> Problem | None:
    """Present an error the service reported, or ``None`` if the code is unknown."""
    rule = _RULES.get(exc.code)
    if rule is None:
        # An unknown code means the service and this CLI disagree about the
        # protocol, which is a bug rather than something to paper over.
        return None

    if exc.code == ErrorCode.PROGRAM_TOO_LARGE:
        message = _too_large(exc)
    else:
        message = rule.message or _sentence(exc.message)

    hint = rule.hint if hints is None else hints.get(exc.code, rule.hint)

    if exc.code == ErrorCode.PROGRAM_SLOT_NOT_SELECTED:
        hint = _slot_hint(exc, hint)

    if exc.code == ErrorCode.UNSUPPORTED_PROTOCOL_VERSION:
        message, hint = _version_mismatch(exc, message, hint)

    return Problem(message=message, hint=hint, exit_code=rule.exit_code)


def _version_mismatch(
    exc: JsonRpcError, message: str, hint: str | None
) -> tuple[str, str | None]:
    """Say which of the two halves is the old one.

    Args:
        exc: The error the service sent.
        message: The wording to fall back on.
        hint: The suggestion to fall back on.

    Returns:
        What to print. A service too old to send the numbers is itself
        the answer to the question, so the fallback -- which blames the
        service -- is right in exactly the case where nothing can be
        worked out.
    """
    payload: object = exc.data
    if not isinstance(payload, Mapping):
        return message, hint

    data = cast("Mapping[str, object]", payload)
    client = data.get("client")
    lowest = data.get("min")
    highest = data.get("max")
    if not (
        isinstance(client, int) and isinstance(lowest, int) and isinstance(highest, int)
    ):
        return message, hint

    served = f"{lowest}" if lowest == highest else f"{lowest} to {highest}"
    # No markup: a headline is printed literally, because it can carry a name
    # or a path that somebody wrote square brackets into.
    said = (
        f"This pb speaks protocol {client} and the background service speaks {served}."
    )
    return said, (_OLD_CLIENT if client < lowest else _STALE)


def _describe(exc: BaseException, hints: Mapping[int, str] | None) -> Problem | None:
    """Present one leaf exception, or ``None`` if it is not one we know."""

    if isinstance(exc, JsonRpcError):
        return _describe_rpc(exc, hints)

    if isinstance(exc, pb_client.CompileError):
        # The compiler's own diagnostics name the offending line, which is a
        # better suggestion than anything that could be written here.
        return Problem(_sentence(str(exc)), detail=exc.output.rstrip())

    if isinstance(exc, pb_client.ServiceUnavailable):
        return Problem(
            "No hub is connected.",
            "Run [bold]pb connect[/] first.",
            exit_code=EXIT_NOTHING_CONNECTED,
        )

    if isinstance(exc, pb_client.ServiceStartError):
        return Problem(
            "The background service would not start.",
            f"Look in {exc.log_file} for what went wrong.",
        )

    if isinstance(exc, _LOST_CONNECTION):
        return Problem(
            "Lost the connection to the background service.",
            "Run [bold]pb service status[/] to check on it, then try again.",
        )

    if isinstance(exc, EndpointError):
        return Problem(
            _sentence(str(exc)),
            f"{ENDPOINT_ENV_VAR} should look like tcp://127.0.0.1:47823, "
            "unix:///path/to/socket or pipe://name. Unset it to use the "
            "default.",
        )

    if isinstance(exc, KeyboardInterrupt):
        return Problem("Interrupted.", exit_code=EXIT_INTERRUPTED)

    return None


def explain(
    exc: BaseException, hints: Mapping[int, str] | None = None
) -> list[Problem] | None:
    """Describe a failure for the user.

    Args:
        exc: What was raised, possibly an exception group.
        hints: Suggestions replacing the default one, for particular JSON-RPC
            error codes. A command knows something the table cannot: which fix
            to suggest for a hub that was not found depends on whether the user
            was trying to connect to one or to use one already connected.

    Returns:
        One :class:`Problem` per distinct failure, or ``None`` if any of them
        is unrecognised -- in which case the caller must re-raise, so that the
        traceback is not lost.
    """
    # Cancellation is control flow rather than failure: a task group cancels
    # its remaining children as part of unwinding, so these are always noise.
    leaves = [leaf for leaf in flatten(exc) if not isinstance(leaf, _CANCELLED)]
    if not leaves:
        # Noise all the way down, which means the whole command was cancelled
        # rather than any part of it failing -- Ctrl-C, in a command that was
        # inside a task group at the time. The interrupt reaches the top as
        # cancellation rather than as KeyboardInterrupt, so without this it
        # was reported as a bug in pb, under a message asking the user to
        # report "everything below" with nothing below it to report.
        return [Problem("Interrupted.", exit_code=EXIT_INTERRUPTED)]

    interesting = [leaf for leaf in leaves if not isinstance(leaf, _LOST_CONNECTION)]
    leaves = interesting or leaves

    problems: list[Problem] = []
    seen: set[tuple[type[BaseException], str]] = set()

    for leaf in leaves:
        # Task groups routinely produce several copies of one failure, one for
        # each task that noticed it.
        key = (type(leaf), str(leaf))
        if key in seen:
            continue

        seen.add(key)
        problem = _describe(leaf, hints)
        if problem is None:
            logger.debug("no friendly wording for %r", leaf)
            return None

        problems.append(problem)

    return problems


def render(err: Console, problems: list[Problem]) -> int:
    """Print problems to stderr and return the exit code to use.

    Args:
        err: A console writing to stderr. Nothing about a failure belongs on
            stdout, which is reserved for data.
        problems: What to print, most important first.

    Returns:
        The exit code of the first problem.
    """

    for problem in problems:
        # Messages can carry text from the service or a file path, so markup is
        # off for them; suggestions are our own literals and may use it.
        err.print(problem.message, style="red", markup=False, highlight=False)

        if problem.detail:
            err.print(problem.detail, markup=False, highlight=False)

        if problem.hint:
            err.print(problem.hint, style="dim")

    return problems[0].exit_code


async def run(
    func: Callable[[*PosArgsT], Awaitable[int]],
    *args: *PosArgsT,
    hints: Mapping[int, str] | None = None,
) -> int:
    """Run a command body, reporting expected failures kindly.

    This is where every command meets the outside world, and the only place a
    broad ``except`` belongs: anything :func:`explain` does not recognise is
    re-raised untouched, so that Python prints its traceback.

    Async, and it starts no event loop of its own. Cyclopts runs an ``async
    def`` command itself, so the whole program enters its loop exactly once, at
    the outermost edge. A second entry point here would be a second boundary,
    and every sync-to-async boundary turns up later as a helper thread, a
    blocking portal, or a test that cannot be written the obvious way.

    Args:
        func: The async body of the command.
        *args: Passed to ``func``.
        hints: Per-code suggestions, as for :func:`explain`.

    Returns:
        The process exit code.
    """

    try:
        return await func(*args)
    except BaseException as exc:
        problems = explain(exc, hints)
        if problems is None:
            logger.debug("unhandled error", exc_info=exc)
            Console(stderr=True).print(_BUG, style="yellow")
            raise

        return render(Console(stderr=True), problems)
