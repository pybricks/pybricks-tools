# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""What can go wrong before a single byte reaches a hub.

Raised by the modules below the RPC layer and translated once, in
:mod:`pb_service.methods.firmware`, in the same way hub failures are. Keeping
them as their own types rather than raising ``JsonRpcError`` directly is what
lets the archive reader be tested without a wire anywhere near it.
"""

from __future__ import annotations

__all__ = [
    "FirmwareBadArchiveError",
    "FirmwareError",
    "FirmwareHubNameTooLongError",
    "FirmwareIncompatibleError",
    "FirmwareNotFoundError",
    "FirmwareUnavailableError",
]


class FirmwareError(Exception):
    """Something went wrong finding, reading or installing firmware."""


class FirmwareNotFoundError(FirmwareError):
    """No firmware matches what was asked for.

    A version that was never published, or a hub with no image of the kind
    wanted -- there is no official LEGO firmware for a Move, City or Technic
    hub to be restored to.
    """


class FirmwareUnavailableError(FirmwareError):
    """The image is not cached and could not be fetched.

    Kept apart from :class:`FirmwareNotFoundError` because the remedies are
    nothing alike: one is a typo, the other is a network, a rate limit, or a
    machine deliberately kept off one.
    """


class FirmwareBadArchiveError(FirmwareError):
    """The archive could not be read as firmware.

    A truncated download, something that was never a firmware zip, or metadata
    written in a format newer than this build understands.
    """


class FirmwareIncompatibleError(FirmwareError):
    """The image does not match the hub in front of us.

    The wrong hub type, or an archive with no variant for the silicon this
    particular hub turned out to have. Never resolved by falling back to
    whatever variant is present: that is how an F4 image gets written to an H5
    hub.
    """


class FirmwareHubNameTooLongError(FirmwareError):
    """The name asked for does not fit the space the firmware reserves for it.

    Sixteen bytes today, including the terminator, so fifteen characters of
    ASCII -- fewer once anything outside it is used, since the field is UTF-8
    and counted in bytes rather than characters.
    """
