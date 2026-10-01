"""Offline checks for the sensor-mode (Auto) change on top of v0.2.1-beta.2."""

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
from custom_components.zehnder_connectbox import diagnostics, fan  # noqa: E402
from custom_components.zehnder_connectbox.const import (  # noqa: E402
    SENSOR_MODE_LEVEL,
)
from custom_components.zehnder_connectbox.models import (  # noqa: E402
    AttachedDevice,
    GatewaySnapshot,
    PropertyKey,
    PropertyValue,
    Room,
    RunMode,
    RunState,
    TemperatureMode,
    VentilationValue,
    VersionInfo,
)
from custom_components.zehnder_connectbox.profiles import (  # noqa: E402
    has_sensor_board,
    room_uses_sensor_mode,
    supports_sensor_mode,
)
from custom_components.zehnder_connectbox.protobuf import (  # noqa: E402
    bytes_value,
    bytes_values,
    decode_fields,
    uint_value,
)
from custom_components.zehnder_connectbox.protocol import (  # noqa: E402
    encode_room_level,
)
from homeassistant.components.fan import NotValidPresetModeError  # noqa: E402
from homeassistant.helpers.json import json_bytes  # noqa: E402

ROOM_ID = 3
DEVICE_ID = 17
LEVEL_PRESETS = ["Level 1", "Level 2", "Level 3", "Level 4"]


def _status(identity: tuple[int, int, int], value: int) -> PropertyValue:
    return PropertyValue(PropertyKey(30, 255, 0, *identity), bytes([value]))


def make_device(*, humidity: int | None, co2: int | None = 1, variant: int = 1):
    """Status 0 = sensor available, 1 = not available, None = not yet read."""
    properties = []
    if humidity is not None:
        properties.append(_status((26, 0, 1), humidity))
    if co2 is not None:
        properties.append(_status((39, 0, 1), co2))
    return AttachedDevice(
        device_id=DEVICE_ID,
        product_type=30,
        product_variant=variant,
        hardware_version=None,
        software_version=None,
        battery_state=None,
        signal_strength=40,
        errors=(),
        level_zero_supported=False,
        filter_warning=False,
        filter_runtime=None,
        filter_maximum=None,
        properties=tuple(properties),
    )


BOARD = make_device(humidity=0)
NO_BOARD = make_device(humidity=1, co2=1)
UNKNOWN = make_device(humidity=None, co2=None)


def make_room(level: int, *, asleep: int = 1, device=BOARD) -> Room:
    return Room(
        room_id=ROOM_ID,
        name="Living",
        room_type=None,
        target_level=None,
        ventilation=(
            VentilationValue(TemperatureMode.AWAKE, level),
            VentilationValue(TemperatureMode.ASLEEP, asleep),
            VentilationValue(TemperatureMode.AWAY, 2),
        ),
        devices=(device,),
    )


def make_snapshot(level: int, *, run_mode=RunMode.MANUAL, asleep=1, device=BOARD):
    run_state = RunState(
        run_mode=int(run_mode),
        temperature_mode=int(TemperatureMode.AWAKE),
        standby=None,
        standby_mode=None,
        summer_ventilation=None,
        errors=(),
    )
    version = VersionInfo(None, None, None, None, None)
    room = make_room(level, asleep=asleep, device=device)
    return GatewaySnapshot(version, run_state, (room,))


class FakeCoordinator:
    """Records commands and applies them to the snapshot like a read-back."""

    def __init__(self, snapshot: GatewaySnapshot) -> None:
        self.data = snapshot
        self.entry = SimpleNamespace(data={"gateway_uuid": "gw"})
        self.last_update_success = True
        self.calls: list[tuple] = []

    async def async_set_level(self, room_id: int, level: int) -> None:
        self.calls.append(("level", room_id, level))
        room = self.data.rooms[0]
        values = tuple(
            replace(value, level=level)
            if value.temperature_mode == self.data.run_state.temperature_mode
            else value
            for value in room.ventilation
        )
        self.data = replace(self.data, rooms=(replace(room, ventilation=values),))

    async def async_set_power(self, enabled: bool) -> None:
        self.calls.append(("power", enabled))
        mode = RunMode.MANUAL if enabled else RunMode.OFF
        self.data = replace(
            self.data, run_state=replace(self.data.run_state, run_mode=int(mode))
        )


def make_fan(snapshot: GatewaySnapshot):
    coordinator = FakeCoordinator(snapshot)
    return fan.ConnectBoxFan(coordinator, DEVICE_ID), coordinator


def room_entries(payload: bytes) -> dict[int, int]:
    room_value = decode_fields(bytes_value(decode_fields(payload), 1))
    assert uint_value(room_value, 1) == ROOM_ID
    return {
        uint_value(decode_fields(item), 1): uint_value(decode_fields(item), 2)
        for item in bytes_values(room_value, 12)
    }


# --- protocol -------------------------------------------------------------


def test_encode_sensor_mode_for_active_situation_only():
    payload = encode_room_level(make_room(2), TemperatureMode.AWAKE, SENSOR_MODE_LEVEL)
    assert room_entries(payload) == {0: SENSOR_MODE_LEVEL, 1: 1, 2: 2}


def test_encode_sensor_mode_entry_bytes():
    payload = encode_room_level(make_room(2), TemperatureMode.AWAKE, SENSOR_MODE_LEVEL)
    assert bytes.fromhex("62 04 08 00 10 05") in payload


def test_encode_level_keeps_sensor_mode_of_other_situations():
    payload = encode_room_level(make_room(SENSOR_MODE_LEVEL), TemperatureMode.ASLEEP, 3)
    assert room_entries(payload) == {0: SENSOR_MODE_LEVEL, 1: 3, 2: 2}


@pytest.mark.parametrize("level", [-1, 6, 255])
def test_encode_rejects_unknown_values(level):
    with pytest.raises(ValueError):
        encode_room_level(make_room(2), 0, level)


# --- sensor-board gate ------------------------------------------------------


@pytest.mark.parametrize(
    ("device", "level", "asleep", "expected"),
    [
        (BOARD, 2, 1, True),  # humidity sensor available
        (make_device(humidity=1, co2=0), 2, 1, True),  # CO2 sensor available
        (NO_BOARD, 2, 1, False),  # both sensors not available
        (UNKNOWN, 2, 1, False),  # status not read yet, no evidence
        (UNKNOWN, SENSOR_MODE_LEVEL, 1, True),  # active situation already auto
        (UNKNOWN, 2, SENSOR_MODE_LEVEL, True),  # another situation is auto
        (make_device(humidity=0, co2=0, variant=2), 2, 1, False),  # ComfoAir 70
        (make_device(humidity=None, co2=None, variant=2), SENSOR_MODE_LEVEL, 1, False),
    ],
)
def test_supports_sensor_mode(device, level, asleep, expected):
    room = make_room(level, asleep=asleep, device=device)
    assert supports_sensor_mode(room, device) is expected


def test_board_and_room_helpers():
    assert has_sensor_board(BOARD) is True
    assert has_sensor_board(NO_BOARD) is False
    assert has_sensor_board(UNKNOWN) is False
    assert room_uses_sensor_mode(make_room(2)) is False
    assert room_uses_sensor_mode(make_room(2, asleep=SENSOR_MODE_LEVEL)) is True


# --- client -----------------------------------------------------------------


class FakeSession:
    def __init__(self) -> None:
        self.requests: list[tuple[int, bytes]] = []

    def request(self, request_type, confirmation_type, body=b"", **_kwargs):
        self.requests.append((request_type, body))
        return SimpleNamespace(body=b"")


def make_client(snapshot: GatewaySnapshot, *, cached_properties: bool):
    client = client_mod.ConnectBoxClient(
        "192.0.2.1", uuid.uuid4(), uuid.uuid4(), "00" * 32
    )
    session = FakeSession()
    room = snapshot.rooms[0]
    device = room.devices[0]
    # The fast room read does not carry the slow sensor-status properties.
    bare_room = replace(room, devices=(replace(device, properties=()),))
    client._read_run_state = lambda: snapshot.run_state
    client._read_rooms = lambda: (bare_room,)
    client._connected_session = lambda: session
    client.read_snapshot = lambda **_kwargs: snapshot
    if cached_properties and device.properties:
        client._property_cache[client._property_cache_key(device)] = device.properties
    return client, session


def test_client_writes_sensor_mode_when_board_is_reported():
    client, session = make_client(make_snapshot(2), cached_properties=True)
    client.set_level(ROOM_ID, SENSOR_MODE_LEVEL)
    assert len(session.requests) == 1
    request_type, body = session.requests[0]
    assert request_type == client_mod.OperationType.SET_ROOM_VALUE_REQUEST
    assert room_entries(body)[0] == SENSOR_MODE_LEVEL


def test_client_refuses_sensor_mode_without_board():
    client, session = make_client(
        make_snapshot(2, device=NO_BOARD), cached_properties=True
    )
    with pytest.raises(ValueError):
        client.set_level(ROOM_ID, SENSOR_MODE_LEVEL)
    assert session.requests == []


def test_client_refuses_sensor_mode_while_status_is_unknown():
    client, session = make_client(
        make_snapshot(2, device=UNKNOWN), cached_properties=False
    )
    with pytest.raises(ValueError):
        client.set_level(ROOM_ID, SENSOR_MODE_LEVEL)
    assert session.requests == []


def test_client_allows_sensor_mode_when_room_already_uses_it():
    snapshot = make_snapshot(2, asleep=SENSOR_MODE_LEVEL, device=UNKNOWN)
    client, session = make_client(snapshot, cached_properties=False)
    client.set_level(ROOM_ID, SENSOR_MODE_LEVEL)
    assert len(session.requests) == 1


def test_client_level_writes_are_unchanged():
    client, session = make_client(
        make_snapshot(SENSOR_MODE_LEVEL, device=NO_BOARD), cached_properties=False
    )
    client.set_level(ROOM_ID, 4)
    assert room_entries(session.requests[0][1])[0] == 4


# --- fan entity -------------------------------------------------------------


def test_fan_offers_auto_with_board():
    entity, _ = make_fan(make_snapshot(2))
    assert entity.is_on is True
    assert entity.percentage == 50
    assert entity.preset_mode == "Level 2"
    assert entity.preset_modes == [*LEVEL_PRESETS, "auto"]


@pytest.mark.parametrize("device", [NO_BOARD, UNKNOWN])
def test_fan_hides_auto_without_detected_board(device):
    entity, coordinator = make_fan(make_snapshot(2, device=device))
    assert entity.preset_modes == LEVEL_PRESETS
    with pytest.raises(NotValidPresetModeError):
        asyncio.run(entity.async_handle_set_preset_mode_service("auto"))
    with pytest.raises(NotValidPresetModeError):
        asyncio.run(entity.async_handle_turn_on_service(preset_mode="auto"))
    assert coordinator.calls == []


def test_fan_offers_auto_when_room_already_uses_it():
    entity, _ = make_fan(make_snapshot(SENSOR_MODE_LEVEL, device=UNKNOWN))
    assert entity.preset_modes == [*LEVEL_PRESETS, "auto"]
    assert entity.preset_mode == "auto"


def test_fan_comfoair_70_shows_but_does_not_offer_auto():
    device = make_device(humidity=0, co2=0, variant=2)
    entity, _ = make_fan(make_snapshot(SENSOR_MODE_LEVEL, device=device))
    assert entity.preset_mode == "auto"
    assert entity.is_on is True
    assert "auto" not in entity.preset_modes


def test_fan_reports_sensor_mode():
    entity, _ = make_fan(make_snapshot(SENSOR_MODE_LEVEL))
    assert entity.is_on is True
    assert entity.percentage is None
    assert entity.preset_mode == "auto"


def test_fan_in_standby_is_off_even_in_sensor_mode():
    entity, _ = make_fan(make_snapshot(SENSOR_MODE_LEVEL, run_mode=RunMode.OFF))
    assert entity.is_on is False


def test_set_preset_auto_wakes_system_and_writes_sensor_mode():
    entity, coordinator = make_fan(make_snapshot(2, run_mode=RunMode.OFF))
    asyncio.run(entity.async_handle_set_preset_mode_service("auto"))
    assert coordinator.calls == [("power", True), ("level", ROOM_ID, SENSOR_MODE_LEVEL)]
    assert entity.preset_mode == "auto"


def test_turn_on_keeps_sensor_mode():
    entity, coordinator = make_fan(
        make_snapshot(SENSOR_MODE_LEVEL, run_mode=RunMode.OFF)
    )
    asyncio.run(entity.async_handle_turn_on_service())
    assert coordinator.calls == [("power", True)]
    assert entity.preset_mode == "auto"


def test_turn_on_without_speed_still_uses_level_1():
    entity, coordinator = make_fan(make_snapshot(3, run_mode=RunMode.OFF))
    asyncio.run(entity.async_handle_turn_on_service())
    assert coordinator.calls == [("power", True), ("level", ROOM_ID, 1)]


def test_turn_on_with_auto_preset():
    entity, coordinator = make_fan(make_snapshot(1))
    asyncio.run(entity.async_handle_turn_on_service(preset_mode="auto"))
    assert coordinator.calls == [("level", ROOM_ID, SENSOR_MODE_LEVEL)]


def test_percentage_and_level_presets_leave_sensor_mode():
    entity, coordinator = make_fan(make_snapshot(SENSOR_MODE_LEVEL))
    asyncio.run(entity.async_set_percentage(75))
    assert entity.preset_mode == "Level 3"
    asyncio.run(entity.async_handle_set_preset_mode_service("Level 4"))
    assert coordinator.calls == [("level", ROOM_ID, 3), ("level", ROOM_ID, 4)]


# --- diagnostics ------------------------------------------------------------


def test_diagnostics_are_serializable_and_show_sensor_mode():
    coordinator = FakeCoordinator(make_snapshot(SENSOR_MODE_LEVEL, device=UNKNOWN))
    entry = SimpleNamespace(runtime_data=coordinator, version=1)
    result = asyncio.run(diagnostics.async_get_config_entry_diagnostics(None, entry))
    device = result["attached_devices"][0]
    assert device["ventilation_level"] == SENSOR_MODE_LEVEL
    assert device["ventilation_values"] == {
        "awake": SENSOR_MODE_LEVEL,
        "asleep": 1,
        "away": 2,
    }
    assert device["sensor_board_reported"] is False
    assert device["sensor_mode_supported"] is True
    json_bytes(result)
