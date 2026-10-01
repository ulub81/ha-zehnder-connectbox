"""Offline checks for sensor readings and the current (actual) fan level."""

from __future__ import annotations

import asyncio
import sys
import uuid
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from custom_components.zehnder_connectbox import client as client_mod  # noqa: E402
from custom_components.zehnder_connectbox import diagnostics, fan, sensor  # noqa: E402
from custom_components.zehnder_connectbox.models import (  # noqa: E402
    GatewaySnapshot,
    RunMode,
    RunState,
    VersionInfo,
)
from custom_components.zehnder_connectbox.profiles import (  # noqa: E402
    SENSOR_TYPE_CO2,
    SENSOR_TYPE_HUMIDITY,
    board_reading,
    has_sensor_board,
    supports_sensor_mode,
)
from custom_components.zehnder_connectbox.protobuf import (  # noqa: E402
    encode_bytes,
    encode_string,
    encode_uint,
)
from custom_components.zehnder_connectbox.protocol import decode_rooms  # noqa: E402
from homeassistant.helpers.json import json_bytes  # noqa: E402

ROOM_ID = 1
DEVICE_ID = 1


def room_message(
    *,
    configured: int,
    current: int | None,
    readings=((1, 239), (2, 51), (3, 597)),
    variant: int = 1,
    status_props: bool = False,
    asleep: int = 5,
) -> bytes:
    """Build a room message shaped like the Wohnzimmer unit's diagnostics."""
    parts = [
        encode_uint(1, DEVICE_ID),
        encode_string(2, "SN12345"),
        encode_uint(5, 30),
        encode_uint(6, 255),
        encode_uint(7, 67),
    ]
    for sensor_type, value in readings:
        parts.append(encode_bytes(8, encode_uint(1, sensor_type) + encode_uint(2, value)))
    parts.append(encode_bytes(9, encode_uint(1, 2) + encode_uint(2, 0)))
    parts.append(encode_uint(20, 1))
    if status_props:
        key = b"".join(
            encode_uint(number, value)
            for number, value in ((1, 30), (2, 255), (3, 0), (4, 26), (5, 0), (6, 1))
        )
        parts.append(encode_bytes(101, encode_bytes(1, key) + encode_bytes(2, b"\x00")))
    parts.append(encode_uint(110, variant))
    room = [
        encode_uint(1, ROOM_ID),
        encode_string(2, "Wohnzimmer"),
        encode_uint(5, 239),
        encode_uint(7, 51),
        encode_bytes(8, b"".join(parts)),
        encode_bytes(9, encode_uint(1, 0) + encode_uint(2, 220)),
        encode_bytes(19, encode_uint(1, 0) + encode_uint(2, configured)),
        encode_bytes(19, encode_uint(1, 1) + encode_uint(2, asleep)),
        encode_bytes(19, encode_uint(1, 2) + encode_uint(2, 0)),
        encode_bytes(19, encode_uint(1, 3) + encode_uint(2, 1)),
        encode_uint(20, 597),
    ]
    if current is not None:
        room.append(encode_uint(21, current))
    return encode_bytes(1, b"".join(room))


def snapshot(message: bytes, run_mode=RunMode.AUTOMATIC) -> GatewaySnapshot:
    run_state = RunState(int(run_mode), 0, False, 0, False, None, ())
    return GatewaySnapshot(
        VersionInfo(None, None, None, None, None), run_state, decode_rooms(message)
    )


class FakeCoordinator:
    def __init__(self, data: GatewaySnapshot) -> None:
        self.data = data
        self.entry = SimpleNamespace(data={"gateway_uuid": "gw"})
        self.last_update_success = True
        self.listeners = []
        self.calls = []

    def async_add_listener(self, callback):
        self.listeners.append(callback)
        return lambda: None

    async def async_set_level(self, room_id, level):
        self.calls.append(("level", room_id, level))

    async def async_set_power(self, enabled):
        self.calls.append(("power", enabled))


# --- decoding -----------------------------------------------------------------


def test_decode_sensor_list_and_target_level():
    (room,) = decode_rooms(room_message(configured=5, current=4))
    (unit,) = room.devices
    assert unit.sensor_readings == ((1, 239), (2, 51), (3, 597))
    assert unit.reading(SENSOR_TYPE_HUMIDITY) == 51
    assert unit.reading(SENSOR_TYPE_CO2) == 597
    assert room.level_for_mode(0) == 5
    assert room.current_level(0) == 4


def test_current_level_falls_back_to_configured_value():
    (room,) = decode_rooms(room_message(configured=3, current=None))
    assert room.current_level(0) == 3


def test_board_readings_only_for_comfospot_50():
    (room,) = decode_rooms(room_message(configured=5, current=5, variant=2))
    assert board_reading(room.devices[0], SENSOR_TYPE_HUMIDITY) is None


def test_readings_count_as_sensor_board():
    (room,) = decode_rooms(room_message(configured=2, current=2))
    unit = room.devices[0]
    assert has_sensor_board(unit) is True
    assert supports_sensor_mode(room, unit) is True
    (bare,) = decode_rooms(
        room_message(configured=2, current=2, readings=((1, 239),), asleep=1)
    )
    assert has_sensor_board(bare.devices[0]) is False
    assert supports_sensor_mode(bare, bare.devices[0]) is False


# --- fan shows the level changed on the unit ------------------------------------


def test_fan_follows_local_change_to_level_4():
    """Wohnzimmer after pressing (-) on the unit: configured 5, running 4."""
    entity = fan.ConnectBoxFan(
        FakeCoordinator(snapshot(room_message(configured=5, current=4))), DEVICE_ID
    )
    assert entity.preset_mode == "Level 4"
    assert entity.percentage == 100
    assert entity.is_on is True


def test_fan_follows_local_change_back_to_auto():
    """Wohnzimmer after pressing (+) again: configured 4 (lagging), running 5."""
    entity = fan.ConnectBoxFan(
        FakeCoordinator(snapshot(room_message(configured=4, current=5))), DEVICE_ID
    )
    assert entity.preset_mode == "auto"
    assert entity.percentage is None
    assert entity.is_on is True
    assert "auto" in entity.preset_modes


def test_fan_off_in_standby_regardless_of_current_level():
    entity = fan.ConnectBoxFan(
        FakeCoordinator(snapshot(room_message(configured=5, current=4), RunMode.OFF)),
        DEVICE_ID,
    )
    assert entity.is_on is False


# --- client waits for the unit to apply a written level --------------------------


def make_client(room_reads):
    client = client_mod.ConnectBoxClient(
        "192.0.2.1", uuid.uuid4(), uuid.uuid4(), "00" * 32
    )
    reads = iter(room_reads)
    seen = []

    def read_rooms():
        try:
            seen.append(next(reads))
        except StopIteration:
            pass
        return seen[-1]

    session = SimpleNamespace(requests=[])
    session.request = lambda *args, **kwargs: (
        session.requests.append(args[0]) or SimpleNamespace(body=b"")
    )
    client._read_run_state = lambda: RunState(1, 0, False, 0, False, None, ())
    client._read_rooms = read_rooms
    client._connected_session = lambda: session
    client.read_snapshot = lambda **_kwargs: "snapshot"
    return client, session, seen


@pytest.fixture
def fast_settle(monkeypatch):
    monkeypatch.setattr(client_mod, "LEVEL_SETTLE_POLL_INTERVAL", 0.01)
    monkeypatch.setattr(client_mod, "LEVEL_SETTLE_TIMEOUT", 0.2)


def test_set_level_waits_until_the_unit_reports_it(fast_settle):
    before = decode_rooms(room_message(configured=5, current=5))
    lagging = decode_rooms(room_message(configured=2, current=5))
    applied = decode_rooms(room_message(configured=2, current=2))
    client, session, seen = make_client([before, lagging, lagging, applied, applied])
    assert client.set_level(ROOM_ID, 2) == "snapshot"
    assert session.requests == [client_mod.OperationType.SET_ROOM_VALUE_REQUEST]
    assert len(seen) == 4  # initial read + three polls until the unit reported 2


def test_set_level_stops_waiting_after_the_timeout(fast_settle):
    before = decode_rooms(room_message(configured=5, current=5))
    client, session, _seen = make_client([before])
    assert client.set_level(ROOM_ID, 3) == "snapshot"
    assert len(session.requests) == 1


# --- sensor entities appear only when reported ------------------------------------


def setup_sensor_entities(coordinator):
    added = []
    entry = SimpleNamespace(
        runtime_data=coordinator, async_on_unload=lambda unsub: None
    )
    asyncio.run(
        sensor.async_setup_entry(None, entry, lambda entities: added.extend(entities))
    )
    return added


def test_humidity_and_co2_sensors_for_units_with_readings():
    coordinator = FakeCoordinator(snapshot(room_message(configured=5, current=5)))
    added = setup_sensor_entities(coordinator)
    by_key = {entity.entity_description.key: entity for entity in added}
    assert by_key["humidity"].native_value == 51
    assert by_key["co2"].native_value == 597
    assert by_key["co2"].native_unit_of_measurement == "ppm"


def test_board_sensors_are_added_when_readings_appear_later():
    bare = snapshot(room_message(configured=5, current=5, readings=((1, 239),)))
    coordinator = FakeCoordinator(bare)
    added = setup_sensor_entities(coordinator)
    keys = [entity.entity_description.key for entity in added]
    assert "humidity" not in keys and "co2" not in keys
    count = len(added)

    coordinator.data = snapshot(room_message(configured=5, current=5))
    for listener in coordinator.listeners:
        listener()
    new_keys = [entity.entity_description.key for entity in added[count:]]
    assert sorted(new_keys) == ["co2", "humidity"]

    for listener in coordinator.listeners:  # no duplicates on later updates
        listener()
    assert len(added) == count + 2


def test_no_board_sensors_for_comfoair_70():
    coordinator = FakeCoordinator(snapshot(room_message(configured=5, current=5, variant=2)))
    keys = [entity.entity_description.key for entity in setup_sensor_entities(coordinator)]
    assert "humidity" not in keys and "co2" not in keys


# --- diagnostics ------------------------------------------------------------------


def test_diagnostics_show_current_level_and_readings():
    coordinator = FakeCoordinator(snapshot(room_message(configured=5, current=4)))
    entry = SimpleNamespace(runtime_data=coordinator, version=1)
    result = asyncio.run(diagnostics.async_get_config_entry_diagnostics(None, entry))
    unit = result["attached_devices"][0]
    assert unit["ventilation_level"] == 5
    assert unit["current_level"] == 4
    assert unit["humidity"] == 51
    assert unit["co2"] == 597
    assert unit["other_sensor_readings"] == {}
    assert "property_scan" not in result and "raw_rooms" not in result
    text = json_bytes(result).decode()
    assert "Wohnzimmer" not in text and "SN12345" not in text


def test_diagnostics_list_unmapped_sensor_types():
    message = room_message(
        configured=5, current=5, readings=((1, 239), (2, 51), (3, 597), (4, 120))
    )
    entry = SimpleNamespace(runtime_data=FakeCoordinator(snapshot(message)), version=1)
    result = asyncio.run(diagnostics.async_get_config_entry_diagnostics(None, entry))
    assert result["attached_devices"][0]["other_sensor_readings"] == {"4": 120}
