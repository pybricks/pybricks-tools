# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""Which route is taken when a hub can be reached more than one way."""

from __future__ import annotations

import pytest

from pb_service.hub.transport import TRANSPORT_PREFERENCE, preference_rank


def test_usb_is_preferred_over_bluetooth() -> None:
    # Quicker to find, quicker to use, and it reaches a hub running a program,
    # which a Bluetooth scan cannot see at all.
    assert preference_rank("usb") < preference_rank("ble")


def test_a_transport_this_build_does_not_know_loses_every_tie() -> None:
    # It can still be used; it just is not chosen over something known.
    assert preference_rank("unknown") > preference_rank("ble")
    assert preference_rank("") > preference_rank("fake")


@pytest.mark.parametrize("scheme", TRANSPORT_PREFERENCE)
def test_every_preferred_transport_ranks_within_the_list(scheme: str) -> None:
    assert preference_rank(scheme) < len(TRANSPORT_PREFERENCE)


def test_the_service_builds_its_transports_in_preference_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The order in backend.py and the order here are two statements of one
    # decision, and nothing else would notice them drifting apart: connecting
    # would simply start choosing the wrong route.
    from pb_service.backend import HUB_BACKEND_ENV_VAR, get_hub_transport
    from pb_service.hub.rfcomm.fake import radio

    # The real transports, not whatever the suite is otherwise pointed at.
    # Building them touches no hardware, and RFCOMM's needs Bluetooth open, so
    # it gets a simulated one.
    monkeypatch.delenv(HUB_BACKEND_ENV_VAR, raising=False)

    schemes = get_hub_transport(radio()).scheme.split("+")

    assert schemes == sorted(schemes, key=preference_rank)
    assert schemes[0] == "usb", "USB is meant to be the preferred route"
