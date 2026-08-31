# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""What the service says, as types rather than as dictionaries.

Every one of these is built from a wire payload with ``from_wire`` and is
missing-field tolerant, because the service on the other end can be older or
newer than this client: it outlives the install that started it. A field this
build has never heard of is ignored, and one it expects but does not get takes
a sensible default, so a version difference narrows what a command can say
rather than stopping it from running.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

__all__ = ["Choice", "DeviceInfo", "Downloaded", "HubInfo", "ServiceInfo"]


@dataclass(frozen=True, slots=True)
class ServiceInfo:
    """What the service reports about itself."""

    version: str
    protocol_version: int
    pid: int
    uptime_s: float
    endpoint: str
    log_file: str
    idle_timeout_s: float
    clients: int

    @classmethod
    def from_wire(cls, payload: dict[str, Any]) -> ServiceInfo:
        """Build from a ``service.info`` result.

        Args:
            payload: The result object.

        Returns:
            The parsed information.
        """
        return cls(
            version=str(payload.get("version", "unknown")),
            protocol_version=int(payload.get("protocol_version", 0)),
            pid=int(payload.get("pid", 0)),
            uptime_s=float(payload.get("uptime_s", 0.0)),
            endpoint=str(payload.get("endpoint", "")),
            log_file=str(payload.get("log_file", "")),
            idle_timeout_s=float(payload.get("idle_timeout_s", 0.0)),
            clients=int(payload.get("clients", 0)),
        )


@dataclass(frozen=True, slots=True)
class DeviceInfo:
    """A hub seen while scanning."""

    id: str
    name: str
    address: str
    rssi: int | None
    transport: str

    hub_type: str | None = None
    """What kind of hub it is, such as "City hub", if its advertisement says."""

    @classmethod
    def from_wire(cls, payload: dict[str, Any]) -> DeviceInfo:
        """Build from a scan result entry.

        Args:
            payload: One entry from ``hub.scan``.

        Returns:
            The parsed device.
        """
        rssi = payload.get("rssi")
        return cls(
            id=str(payload.get("id", "")),
            name=str(payload.get("name", "")),
            address=str(payload.get("address", "")),
            rssi=int(rssi) if isinstance(rssi, int) else None,
            transport=str(payload.get("transport", "ble")),
            hub_type=(
                str(payload["type"]) if payload.get("type") is not None else None
            ),
        )


@dataclass(frozen=True, slots=True)
class Choice:
    """The hub that commands act on when none is named."""

    name: str | None
    """The chosen hub's name, or ``None`` if none has been chosen.

    A name rather than an id, because an id is built from a Bluetooth
    address and stops naming the hub it names today.
    """

    connected: bool
    """Whether that hub is connected at the moment.

    A hub can be chosen while it is switched off, so this is worth saying
    out loud rather than assuming.
    """

    @classmethod
    def from_wire(cls, payload: dict[str, Any]) -> Choice:
        """Read one from a ``hub.use`` result.

        Args:
            payload: The result object.

        Returns:
            The parsed result.
        """
        name = payload.get("default")
        return cls(
            name=str(name) if name else None,
            connected=bool(payload.get("connected", False)),
        )


@dataclass(frozen=True, slots=True)
class Downloaded:
    """Where a program ended up on the hub."""

    size: int
    """How many bytes were written."""

    slot: int
    """Which slot it went into, which the hub chooses rather than the host."""

    slots: int
    """How many slots this hub has. One means it holds a single program."""

    @classmethod
    def from_wire(cls, payload: dict[str, Any]) -> Downloaded:
        """Read one from a ``program.download`` result.

        Args:
            payload: The result object.

        Returns:
            The parsed result.
        """
        return cls(
            size=int(payload.get("size", 0)),
            slot=int(payload.get("slot", 0)),
            slots=max(1, int(payload.get("slots", 1))),
        )


@dataclass(frozen=True, slots=True)
class HubInfo:
    """A connected hub."""

    id: str
    name: str
    state: str
    hub_type: str | None
    """What kind of hub it is, such as "Technic hub", if it is a known one."""

    firmware: str
    protocol: str
    address: str
    num_slots: int
    max_program_size: int
    subscribers: int
    program_running: bool
    status_names: tuple[str, ...]

    running_program_id: int | None = None
    """Which program is running, where the hub says.

    128 is the hub's own interpreter, which is how a session tells a REPL it
    can join from a program somebody else started.
    """

    @classmethod
    def from_wire(cls, payload: dict[str, Any]) -> HubInfo:
        """Build from a hub description.

        Args:
            payload: A hub object from ``hub.list``, ``hub.info`` or
                ``hub.connect``.

        Returns:
            The parsed hub.
        """
        names = payload.get("status_names") or []
        return cls(
            id=str(payload.get("id", "")),
            name=str(payload.get("name", "")),
            state=str(payload.get("state", "unknown")),
            hub_type=(
                str(payload["type"]) if payload.get("type") is not None else None
            ),
            firmware=str(payload.get("firmware", "")),
            protocol=str(payload.get("protocol", "")),
            address=str(payload.get("address", "")),
            num_slots=int(payload.get("num_slots", 0)),
            max_program_size=int(payload.get("max_program_size", 0)),
            subscribers=int(payload.get("subscribers", 0)),
            program_running=bool(payload.get("program_running", False)),
            running_program_id=(
                int(payload["running_program_id"])
                if isinstance(payload.get("running_program_id"), int)
                else None
            ),
            status_names=tuple(str(name) for name in names),
        )
