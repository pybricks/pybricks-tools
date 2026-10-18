# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""Telling a brick from everything else the computer is paired with.

Bluetooth Classic offers no PnP ID to a scan, and plain Serial Port is what
every headset publishes, so the name the firmware builds is what identifies an
NXT. These pin the shape that depends on, because it is a presentation string
upstream and nothing there knows this reads it.
"""

from __future__ import annotations

import pytest

from pb_service.hub.protocol import HubType
from pb_service.hub.rfcomm.port import split_hub_name


@pytest.mark.parametrize(
    ("full", "bare", "expected"),
    [
        ("ev3 (MINDSTORMS EV3)", "ev3", HubType.EV3),
        ("Pybricks (MINDSTORMS NXT)", "Pybricks", HubType.NXT),
        # The whole name can be the suffix if the hub was never named.
        ("(MINDSTORMS EV3)", None, HubType.EV3),
        # Only the firmware's one separating space goes. The rest belongs to
        # the name, which the hub reports with them, and filters match it.
        (" Bumblebee  (MINDSTORMS EV3)", " Bumblebee ", HubType.EV3),
    ],
)
def test_the_firmware_suffix_names_the_brick(
    full: str, bare: str | None, expected: HubType
) -> None:
    assert split_hub_name(full) == (bare, expected)


@pytest.mark.parametrize(
    "name",
    [
        None,
        "",
        # The reason any of this exists: a headset offers Serial Port too.
        "Bose NC 700 Head",
        # A hub type this build has never heard of says nothing rather than
        # guessing, the same way an unknown PnP ID does.
        "something (Another Hub)",
        # Brackets alone are not a suffix.
        "a hub (with a parenthetical)",
    ],
)
def test_anything_else_identifies_no_brick(name: str | None) -> None:
    # The name comes back untouched: brackets this does not recognise belong
    # to whoever named the device.
    assert split_hub_name(name) == (name, None)


def test_the_last_suffix_wins() -> None:
    # The user sets the prefix and the firmware appends to it, so somebody who
    # names their NXT after an EV3 still gets an NXT.
    assert split_hub_name("x (MINDSTORMS EV3) (MINDSTORMS NXT)") == (
        "x (MINDSTORMS EV3)",
        HubType.NXT,
    )
