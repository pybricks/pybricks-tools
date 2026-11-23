# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""What a Bluetooth scan reports, from what the radio hears.

bleak is replaced by a scanner that replays advertisements, so these run
without a radio and say exactly which packet arrived when.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Sequence
from types import TracebackType
from typing import Any

import anyio
import pytest
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData

from pb_service.hub import ble
from pb_service.hub import protocol as p
from pb_service.hub.transport import DeviceInfo, ScanFilter

pytestmark = pytest.mark.anyio

CITY_HUB_PNP_ID = bytes([1, 0x97, 0x03, 0x41, 0x00, 0x00, 0x00])
ADDRESS = "ED:2F:CF:0F:14:39"
NAME = "Pybricks Hub"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _heard(*, scan_response: bool) -> tuple[BLEDevice, AdvertisementData]:
    """One sighting of a City hub, with or without its scan response merged in."""
    service_data = {p.PNP_ID_UUID: CITY_HUB_PNP_ID} if scan_response else {}
    advertisement = AdvertisementData(
        local_name=NAME,
        manufacturer_data={},
        service_data=service_data,
        service_uuids=[p.PYBRICKS_SERVICE_UUID],
        tx_power=None,
        rssi=-50,
        platform_data=(),
    )
    return BLEDevice(ADDRESS, NAME, None), advertisement


class _ReplayingScanner:
    """Stands in for bleak's scanner, hearing what it is given, in order.

    Each entry is how long to wait before hearing it, then what is heard.
    """

    script: Sequence[tuple[float, tuple[BLEDevice, AdvertisementData]]] = ()

    def __init__(self, **_kwargs: Any) -> None:
        pass

    async def __aenter__(self) -> _ReplayingScanner:
        return self

    async def __aexit__(
        self,
        _type: type[BaseException] | None,
        _value: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        pass

    async def advertisement_data(
        self,
    ) -> AsyncGenerator[tuple[BLEDevice, AdvertisementData], None]:
        for delay, heard in self.script:
            await anyio.sleep(delay)
            yield heard
        await anyio.sleep_forever()


async def _first_report(
    monkeypatch: pytest.MonkeyPatch,
    script: Sequence[tuple[float, tuple[BLEDevice, AdvertisementData]]],
) -> DeviceInfo:
    monkeypatch.setattr(_ReplayingScanner, "script", script)
    monkeypatch.setattr(ble, "BleakScanner", _ReplayingScanner)
    with anyio.fail_after(5):
        async for info in ble.BleHubTransport().discover(ScanFilter()):
            return info
    raise AssertionError("the scan ended without reporting anything")


async def test_a_hub_is_first_reported_with_its_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The advertisement comes first and the scan response, which says what
    # kind of hub this is, a moment later. Only the whole of it is reported.
    info = await _first_report(
        monkeypatch,
        [
            (0, _heard(scan_response=False)),
            (0.05, _heard(scan_response=True)),
        ],
    )

    assert info.hub_type == "City hub"


async def test_a_hub_that_never_says_its_type_is_logged_not_reported(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # Every Pybricks firmware sends its PnP ID, so one that does not has a bug
    # worth hearing about, once, rather than a hub to list without its type.
    monkeypatch.setattr(ble, "SCAN_RESPONSE_SECONDS", 0.2)
    monkeypatch.setattr(
        _ReplayingScanner,
        "script",
        [(0, _heard(scan_response=False))] + [(0.05, _heard(scan_response=False))] * 10,
    )
    monkeypatch.setattr(ble, "BleakScanner", _ReplayingScanner)

    reported: list[DeviceInfo] = []
    with anyio.move_on_after(1):
        async for info in ble.BleHubTransport().discover(ScanFilter()):
            reported.append(info)

    assert reported == []
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1
    assert ADDRESS in warnings[0].getMessage()
    assert "firmware bug" in warnings[0].getMessage()
