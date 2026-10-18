# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""How failures reach the user.

The rule under test is the one users notice: something they can fix is one
sentence and a suggestion, and something they cannot is a traceback.
"""

from __future__ import annotations

import asyncio
import io
import sys
from pathlib import Path

import anyio
import pytest
from rich.console import Console

import pb_client
from pb_cli import cli, errors
from pb_core.ipc import EndpointError
from pb_core.jsonrpc import ErrorCode, JsonRpcError, PeerClosedError

pytestmark = pytest.mark.anyio


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    return "asyncio"


def _grouped(exc: BaseException) -> BaseExceptionGroup[BaseException]:
    """Wrap an exception the way a task group would."""
    return BaseExceptionGroup("unhandled errors in a TaskGroup", [exc])


def _render(problems: list[errors.Problem]) -> tuple[str, int]:
    """Render to a plain console and return the text and exit code."""
    buffer = io.StringIO()
    console = Console(file=buffer, width=100, no_color=True, highlight=False)
    code = errors.render(console, problems)
    return buffer.getvalue(), code


# --------------------------------------------------------------- flattening


def test_flatten_returns_a_plain_exception_unchanged() -> None:
    exc = ValueError("bang")
    assert errors.flatten(exc) == [exc]


def test_flatten_reaches_into_nested_groups() -> None:
    inner = ValueError("inner")
    outer = RuntimeError("outer")
    group = BaseExceptionGroup("a", [BaseExceptionGroup("b", [inner]), outer])
    assert errors.flatten(group) == [inner, outer]


def test_find_looks_inside_a_group() -> None:
    wanted = pb_client.ServiceUnavailable("nothing there")
    assert errors.find(_grouped(wanted), pb_client.ServiceUnavailable) is wanted


def test_find_returns_none_when_absent() -> None:
    assert errors.find(_grouped(ValueError("bang")), KeyError) is None


# ---------------------------------------------------------------- explaining


def test_an_error_wrapped_by_a_task_group_is_still_recognised() -> None:
    # The regression that made this module necessary: every command wrapped
    # its work in `async with pb_client.connect()`, whose task group turned a
    # perfectly ordinary hub error into a group of one, so the command's
    # `except JsonRpcError` never matched and the user got a traceback.
    exc = JsonRpcError(ErrorCode.HUB_NOT_FOUND, "no hub found")
    problems = errors.explain(_grouped(exc))
    assert problems is not None
    assert problems[0].message == "No hub found."
    assert problems[0].hint is not None


_BUG_CODES = {
    ErrorCode.PARSE_ERROR,
    ErrorCode.INVALID_REQUEST,
    ErrorCode.INVALID_PARAMS,
}
"""Codes that mean the two halves disagree about the protocol.

There is nothing to suggest to a user about a message the service could not
parse or could not use, so these keep their traceback like any other bug.
"""

_NOTHING_TO_SUGGEST = {ErrorCode.REQUEST_CANCELLED}
"""Codes whose message is already the whole story."""


@pytest.mark.parametrize("code", [code for code in ErrorCode if code not in _BUG_CODES])
def test_every_expected_failure_is_worded(code: ErrorCode) -> None:
    # A new error code with no wording would reach users as a traceback, so
    # this fails as soon as one is added to the protocol.
    problems = errors.explain(JsonRpcError(code, "something happened"))
    assert problems is not None, f"{code.name} has no friendly wording"
    assert problems[0].message

    if code not in _NOTHING_TO_SUGGEST:
        # A message with no way forward is only half an error message.
        assert problems[0].hint, f"{code.name} suggests nothing"


@pytest.mark.parametrize("code", sorted(_BUG_CODES))
def test_protocol_bugs_keep_their_traceback(code: ErrorCode) -> None:
    assert errors.explain(JsonRpcError(code, "something happened")) is None


def test_an_unknown_error_code_is_not_explained_away() -> None:
    # A code this CLI has never heard of means the two halves disagree about
    # the protocol, which is a bug and deserves a traceback.
    assert errors.explain(JsonRpcError(-31999, "from the future")) is None


def test_an_unknown_exception_is_not_explained_away() -> None:
    assert errors.explain(_grouped(ValueError("bang"))) is None


def test_one_unknown_leaf_spoils_the_whole_group() -> None:
    # Rendering the half we understand and dropping the rest would hide the
    # bug, so the group goes back untouched.
    group = BaseExceptionGroup(
        "several",
        [JsonRpcError(ErrorCode.HUB_BUSY, "busy"), ValueError("bang")],
    )
    assert errors.explain(group) is None


def test_repeated_copies_of_one_failure_are_reported_once() -> None:
    # Every task in a group notices the same disconnect.
    exc = JsonRpcError(ErrorCode.HUB_DISCONNECTED, "gone")
    group = BaseExceptionGroup("a", [exc, JsonRpcError(exc.code, exc.message)])
    problems = errors.explain(group)
    assert problems is not None
    assert len(problems) == 1


def test_cancellation_is_not_a_failure() -> None:
    exc = JsonRpcError(ErrorCode.HUB_BUSY, "busy")
    group = BaseExceptionGroup("a", [exc, asyncio.CancelledError()])
    problems = errors.explain(group)
    assert problems is not None
    assert len(problems) == 1


def test_an_interrupt_is_not_a_bug() -> None:
    # Ctrl-C in a command that was inside a task group arrives at the top as
    # cancellation rather than as KeyboardInterrupt, and every leaf is then
    # something this module treats as noise. That used to leave nothing to
    # explain, which was read as "no friendly wording for this" and reported
    # as a bug in pb -- for the most ordinary thing a user can do.
    problems = errors.explain(_grouped(asyncio.CancelledError()))
    assert problems is not None
    assert problems[0].exit_code == errors.EXIT_INTERRUPTED
    assert problems[0].message == "Interrupted."


def test_a_bare_cancellation_is_an_interrupt_too() -> None:
    problems = errors.explain(asyncio.CancelledError())
    assert problems is not None
    assert problems[0].exit_code == errors.EXIT_INTERRUPTED


def test_a_broken_stream_yields_to_a_better_explanation() -> None:
    # Losing the connection is what a hub error looks like from the transport,
    # a moment later. The hub error is the one worth printing.
    exc = JsonRpcError(ErrorCode.HUB_DISCONNECTED, "gone")
    group = BaseExceptionGroup("a", [exc, anyio.BrokenResourceError()])
    problems = errors.explain(group)
    assert problems is not None
    assert len(problems) == 1
    assert problems[0].message == "The hub is no longer connected."


def test_a_broken_stream_alone_is_explained() -> None:
    problems = errors.explain(_grouped(anyio.BrokenResourceError()))
    assert problems is not None
    assert "background service" in problems[0].message


def test_a_command_can_replace_a_suggestion() -> None:
    exc = JsonRpcError(ErrorCode.HUB_NOT_FOUND, "no hub found")
    hints = {int(ErrorCode.HUB_NOT_FOUND): "Try switching it on."}
    problems = errors.explain(exc, hints)
    assert problems is not None
    assert problems[0].hint == "Try switching it on."


def test_the_size_limit_is_quoted_back() -> None:
    # error.data exists so the message can say what would have fit.
    exc = JsonRpcError(
        ErrorCode.PROGRAM_TOO_LARGE,
        "program is 5000 bytes; this hub accepts at most 4096",
        {"size": 5000, "limit": 4096},
    )
    problems = errors.explain(exc)
    assert problems is not None
    assert "5000 bytes" in problems[0].message
    assert "4096 bytes" in problems[0].message


def test_a_missing_size_limit_falls_back_to_the_service_wording() -> None:
    exc = JsonRpcError(ErrorCode.PROGRAM_TOO_LARGE, "program is too big", None)
    problems = errors.explain(exc)
    assert problems is not None
    assert problems[0].message == "Program is too big."


def test_the_compiler_output_is_kept_verbatim() -> None:
    exc = pb_client.CompileError("could not compile demo.py", "line 2\nSyntaxError\n")
    problems = errors.explain(exc)
    assert problems is not None
    assert problems[0].message == "Could not compile demo.py."
    assert problems[0].detail == "line 2\nSyntaxError"


def test_a_service_that_will_not_start_names_its_log() -> None:
    exc = pb_client.ServiceStartError("pipe://pb", Path("C:/logs/pb-service.log"))
    problems = errors.explain(exc)
    assert problems is not None
    assert problems[0].hint is not None
    assert "pb-service.log" in problems[0].hint


def test_a_mistyped_endpoint_says_what_one_looks_like() -> None:
    # PB_ENDPOINT is the one address a user can get wrong by hand.
    problems = errors.explain(EndpointError("unknown endpoint scheme: 'wat'"))
    assert problems is not None
    assert problems[0].hint is not None
    assert "PB_ENDPOINT" in problems[0].hint


# ----------------------------------------------------------------- rendering


def test_rendering_shows_the_message_then_the_suggestion() -> None:
    text, code = _render(
        [
            errors.Problem(
                "It went wrong.", "Try again.", exit_code=errors.EXIT_HUB_ERROR
            )
        ]
    )
    assert text.splitlines() == ["It went wrong.", "Try again."]
    assert code == errors.EXIT_HUB_ERROR


def test_rendering_does_not_interpret_markup_in_a_message() -> None:
    # Messages carry text from the service and paths from the filesystem, and
    # a stray bracket must not eat the rest of the line.
    text, _ = _render([errors.Problem("could not read [draft].py")])
    assert "[draft].py" in text


def test_nothing_about_a_failure_reaches_stdout(
    capsys: pytest.CaptureFixture[str],
) -> None:
    errors.render(
        Console(stderr=True), [errors.Problem("It went wrong.", "Try again.")]
    )
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "It went wrong." in captured.err


# --------------------------------------------------------------------- run
#
# run() is async and starts no event loop of its own: cyclopts runs the
# commands, so the program enters its loop exactly once. These therefore run
# inside a loop like the commands do, rather than reaching in from outside.


async def _ok() -> int:
    return errors.EXIT_OK


async def _raise(exc: BaseException) -> int:
    raise exc


async def test_run_returns_the_command_result() -> None:
    assert await errors.run(_ok) == errors.EXIT_OK


async def test_run_reports_an_expected_failure(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = await errors.run(_raise, JsonRpcError(ErrorCode.HUB_BUSY, "the hub is busy"))
    assert code == errors.EXIT_HUB_ERROR
    captured = capsys.readouterr()
    assert "The hub is busy." in captured.err
    assert "Traceback" not in captured.err


async def test_run_lets_an_unexpected_failure_through(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Deliberately not caught: an error we cannot explain is a bug, and the
    # traceback is the useful part.
    with pytest.raises(ValueError, match="bang"):
        await errors.run(_raise, ValueError("bang"))

    assert "bug in pb" in capsys.readouterr().err


async def test_interrupting_is_not_a_crash(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert await errors.run(_raise, KeyboardInterrupt()) == errors.EXIT_INTERRUPTED
    assert "Interrupted." in capsys.readouterr().err


# ------------------------------------------------------------------ wiring


def test_nothing_in_the_cli_starts_an_event_loop() -> None:
    # The commands are async and cyclopts runs them, so the program enters its
    # loop exactly once, at the outermost edge. A second entry point would be a
    # second sync-to-async boundary, and every one of those turns up later as a
    # helper thread, a blocking portal, or a test that cannot be written the
    # obvious way. It would also put the command outside the one place that
    # turns failures into messages, bringing the traceback back.
    package = Path(cli.__file__).parent

    for module in sorted(package.glob("*.py")):
        source = module.read_text(encoding="utf-8")
        assert "anyio.run(" not in source, module.name
        assert "asyncio.run(" not in source, module.name
        assert "start_blocking_portal" not in source, module.name


async def test_a_service_that_goes_away_mid_call_is_not_called_a_bug(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # The most literal form of a lost connection: the request was sent and the
    # connection ended before the answer came. It used to print a traceback
    # under "this looks like a bug in pb", which is the wrong thing to say
    # about a service that simply went away -- and it does go away, because a
    # service that loses the startup race exits while a client is attached.
    code = await errors.run(_raise, PeerClosedError("connection closed"))
    assert code == errors.EXIT_ERROR
    captured = capsys.readouterr()
    assert "Lost the connection" in captured.err
    assert "bug in pb" not in captured.err
    assert "Traceback" not in captured.err


def _version_error(client: int, lowest: int, highest: int) -> JsonRpcError:
    """Build the error a service raises when the handshake does not agree."""
    return JsonRpcError(
        ErrorCode.UNSUPPORTED_PROTOCOL_VERSION,
        f"client speaks protocol {client}; this service serves {lowest}..{highest}",
        {"client": client, "min": lowest, "max": highest},
    )


def test_an_old_service_is_named_as_the_old_one() -> None:
    # The usual way round: the service outlives the install that started it.
    problems = errors.explain(_version_error(3, 1, 2))
    assert problems is not None
    (problem,) = problems
    assert "speaks protocol 3" in problem.message
    assert "background service speaks 1 to 2" in problem.message
    assert problem.hint is not None
    assert "pb service stop" in problem.hint


def test_an_old_pb_is_not_told_to_stop_the_service() -> None:
    # The other way round, and the reason this exists: stopping the service
    # does nothing for somebody whose own pb is the older half.
    problems = errors.explain(_version_error(1, 2, 2))
    assert problems is not None
    (problem,) = problems
    assert problem.hint is not None
    assert "Upgrade" in problem.hint
    assert "background service speaks 2." in problem.message


def test_a_service_too_old_to_say_is_blamed_anyway() -> None:
    # No numbers means a service from before they were sent, which is itself
    # the answer: it is the old one.
    stale = JsonRpcError(
        ErrorCode.UNSUPPORTED_PROTOCOL_VERSION,
        "client speaks protocol 3; this service serves 1..1",
    )
    problems = errors.explain(stale)
    assert problems is not None
    (problem,) = problems
    assert problem.hint is not None
    assert "left over from an older install" in problem.hint


def test_the_version_headline_carries_no_markup() -> None:
    # Headlines are printed literally, so markup in one arrives as itself.
    problems = errors.explain(_version_error(1, 2, 2))
    assert problems is not None
    (problem,) = problems
    assert "[" not in problem.message


# --------------------------------------------------- a port that would not open


def test_a_refused_serial_port_keeps_the_port_in_the_message() -> None:
    # Which port it was is the half of this the user cannot work out, and the
    # service is the only one that knows it, so its wording is kept.
    exc = JsonRpcError(
        ErrorCode.SERIAL_PORT_UNAVAILABLE,
        "found a hub but could not open its port: /dev/ttyACM0 (Permission denied)",
    )
    problems = errors.explain(exc)

    assert problems is not None
    assert "/dev/ttyACM0" in problems[0].message
    assert problems[0].hint


def test_unusable_bluetooth_keeps_the_systems_words() -> None:
    # They are what tells a radio switched off from one pb may not use, and
    # the advice covers Bluetooth Classic as well as Low Energy.
    exc = JsonRpcError(
        ErrorCode.BLUETOOTH_UNAVAILABLE,
        "Bluetooth cannot be used: Bluetooth is switched off",
    )
    problems = errors.explain(exc)

    assert problems is not None
    assert "switched off" in problems[0].message
    assert problems[0].hint
    assert "Low Energy" not in problems[0].hint


@pytest.mark.parametrize(
    ("availability", "expected"),
    [
        ("switched_off", "Switch Bluetooth on"),
        ("no_adapter", "Plug in a Bluetooth adapter"),
        # The two only macOS reports, worded for it there.
        (
            "not_allowed",
            "Privacy & Security" if sys.platform == "darwin" else "lets pb use",
        ),
        ("awaiting_permission", "Answer the"),
        ("service_unavailable", "Wait a moment"),
        ("unsupported", "USB"),
        (None, "Switch Bluetooth on, or plug in an adapter"),
        ("something_newer", "Switch Bluetooth on, or plug in an adapter"),
    ],
)
def test_unusable_bluetooth_gets_the_advice_for_its_reason(
    availability: str | None, expected: str
) -> None:
    # One suggestion covering every reason sent somebody whose permission
    # dialog was open off to switch on a radio that was already on.
    exc = JsonRpcError(
        ErrorCode.BLUETOOTH_UNAVAILABLE,
        "Bluetooth cannot be used: whatever the system said",
        {"availability": availability} if availability else None,
    )
    problems = errors.explain(exc)

    assert problems is not None
    assert problems[0].hint is not None
    assert expected in problems[0].hint
    assert "whatever the system said" in problems[0].message


def test_the_name_is_the_one_the_service_asks_under() -> None:
    from pb_service.backend import PERMISSION_NAME

    assert errors.PERMISSION_NAME == PERMISSION_NAME


def test_the_permission_advice_names_what_macos_shows() -> None:
    # The dialog and System Settings say "Pybricks", not pb or Python.
    for availability in ("awaiting_permission", "not_allowed"):
        assert "Pybricks" in errors.bluetooth_advice(availability, "darwin")

    # Elsewhere there is no System Settings to send anybody to.
    assert "System Settings" not in errors.bluetooth_advice("not_allowed", "linux")


@pytest.mark.parametrize(
    ("platform", "expected"),
    [
        ("linux", "dialout"),
        ("freebsd13", "dialout"),
        ("win32", "Close anything else"),
        ("darwin", "Unplug"),
    ],
)
def test_the_advice_matches_what_the_refusal_means_there(
    platform: str, expected: str
) -> None:
    # Windows has no group to join: a refused COM port there is one another
    # program is holding, so advice about permissions would send a user
    # looking for something their system does not have.
    assert expected in errors.serial_port_advice(platform)
