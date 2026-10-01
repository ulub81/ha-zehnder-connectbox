"""Offline checks for summer ventilation, situation levels and temporary changes."""

from __future__ import annotations

import asyncio
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from custom_components.zehnder_connectbox import (  # noqa: E402
    binary_sensor,
    diagnostics,
    select,
    sensor,
)
from custom_components.zehnder_connectbox import client as client_mod  # noqa: E402
from custom_components.zehnder_connectbox.models import (  # noqa: E402
    GatewaySnapshot,
    RunState,
    VersionInfo,
)
from custom_components.zehnder_connectbox.protobuf import (  # noqa: E402
    bytes_value,
    bytes_values,
    decode_fields,
    encode_bytes,
    encode_string,
    encode_uint,
    uint_value,
)
from custom_components.zehnder_connectbox.protocol import decode_rooms  # noqa: E402
from homeassistant.exceptions import ServiceValidationError  # noqa: E402
from homeassistant.helpers.json import json_bytes  # noqa: E402

UNTIL = 1790881200  # 2026-10-01 21:00 Europe/Berlin


def room_message(
    *,
    room_id=1,
    device_id=1,
    levels=(5, 5, 0, 1),
    current=5,
    until=None,
    level_zero=1,
    readings=((1, 239), (2, 51), (3, 597)),
    variant=1,
):
    unit = [encode_uint(1, device_id), encode_uint(5, 30), encode_uint(7, 67)]
    for sensor_type, value in readings:
        unit.append(encode_bytes(8, encode_uint(1, sensor_type) + encode_uint(2, value)))
    unit += [encode_uint(20, level_zero), encode_uint(110, variant)]
    room = [encode_uint(1, room_id), encode_string(2, "Wohnzimmer"), encode_bytes(8, b"".join(unit))]
    for mode, level in enumerate(levels):
        room.append(encode_bytes(19, encode_uint(1, mode) + encode_uint(2, level)))
    room.append(encode_uint(21, current))
    if until is not None:
        room.append(encode_uint(61, until))
    return encode_bytes(1, b"".join(room))


def snapshot(*messages, summer=False, temperature_mode=0):
    rooms = tuple(room for message in messages for room in decode_rooms(message))
    run_state = RunState(1, temperature_mode, False, 0, summer, ())
    return GatewaySnapshot(VersionInfo(None, None, None, None, None), run_state, rooms)


class FakeCoordinator:
    def __init__(self, data):
        self.data = data
        self.entry = SimpleNamespace(data={"gateway_uuid": "gw"})
        self.last_update_success = True
        self.calls = []
        self.listeners = []

    def async_add_listener(self, callback):
        self.listeners.append(callback)
        return lambda: None

    async def async_set_level(self, room_id, level, temperature_mode=None):
        self.calls.append((room_id, level, temperature_mode))


def setup_platform(module, coordinator):
    added = []
    entry = SimpleNamespace(runtime_data=coordinator, async_on_unload=lambda unsub: None)
    asyncio.run(module.async_setup_entry(None, entry, lambda entities: added.extend(entities)))
    return added


# --- protocol ---------------------------------------------------------------------


def test_decode_temporary_change_end():
    (room,) = decode_rooms(room_message(until=UNTIL))
    assert room.temporary_until == UNTIL
    (plain,) = decode_rooms(room_message())
    assert plain.temporary_until is None


# --- summer ventilation -----------------------------------------------------------


@pytest.mark.parametrize(("summer", "state", "available"), [(True, True, True), (False, False, True), (None, None, False)])
def test_summer_ventilation_binary_sensor(summer, state, available):
    coordinator = FakeCoordinator(snapshot(room_message(), summer=summer))
    entities = setup_platform(binary_sensor, coordinator)
    summer_entity = next(
        entity for entity in entities if isinstance(entity, binary_sensor.ConnectBoxSummerVentilation)
    )
    assert summer_entity.is_on is state
    assert summer_entity.available is available


# --- situation levels -------------------------------------------------------------


def situation_entities(coordinator):
    return {
        entity.translation_key: entity
        for entity in setup_platform(select, coordinator)
        if isinstance(entity, select.ConnectBoxSituationLevel)
    }


def test_situation_selects_show_configured_values():
    entities = situation_entities(FakeCoordinator(snapshot(room_message(levels=(5, 1, 0, 2)))))
    assert set(entities) == {
        "situation_level_awake",
        "situation_level_asleep",
        "situation_level_away",
        "situation_level_antifreeze",
    }
    assert entities["situation_level_awake"].current_option == "auto"
    assert entities["situation_level_asleep"].current_option == "level_1"
    assert entities["situation_level_away"].current_option == "standby"
    assert entities["situation_level_antifreeze"].current_option == "level_2"
    assert entities["situation_level_away"].options == [
        "standby", "level_1", "level_2", "level_3", "level_4", "auto"
    ]


def test_situation_options_follow_unit_capabilities():
    message = room_message(
        levels=(2, 1, 1, 1), current=2, level_zero=0, readings=((1, 239),)
    )
    entities = situation_entities(FakeCoordinator(snapshot(message)))
    assert entities["situation_level_away"].options == [
        "level_1", "level_2", "level_3", "level_4"
    ]


def test_situation_select_writes_that_situation():
    coordinator = FakeCoordinator(snapshot(room_message(room_id=4, device_id=5)))
    entities = situation_entities(coordinator)
    asyncio.run(entities["situation_level_away"].async_select_option("level_2"))
    asyncio.run(entities["situation_level_asleep"].async_select_option("auto"))
    assert coordinator.calls == [(4, 2, 2), (4, 5, 1)]


def test_situation_select_validates_options():
    message = room_message(levels=(2, 1, 1, 1), current=2, readings=((1, 239),))
    entities = situation_entities(FakeCoordinator(snapshot(message)))
    with pytest.raises(ServiceValidationError):
        asyncio.run(entities["situation_level_away"].async_handle_select_option("auto"))


def test_situation_selects_only_for_present_situations():
    message = room_message(levels=(3, 1))
    entities = situation_entities(FakeCoordinator(snapshot(message)))
    assert set(entities) == {"situation_level_awake", "situation_level_asleep"}


# --- client writes a given situation -------------------------------------------------


def make_client(message):
    client = client_mod.ConnectBoxClient("192.0.2.1", uuid.uuid4(), uuid.uuid4(), "00" * 32)
    rooms = decode_rooms(message)
    session = SimpleNamespace(bodies=[])
    session.request = lambda request, confirm, body=b"", **_kw: (
        session.bodies.append(body) or SimpleNamespace(body=b"")
    )
    reads = []
    client._read_run_state = lambda: RunState(1, 0, False, 0, False, ())
    client._read_rooms = lambda: (reads.append(1) or rooms)
    client._connected_session = lambda: session
    client.read_snapshot = lambda **_kw: "snapshot"
    return client, session, reads


def entries(body):
    room_value = decode_fields(bytes_value(decode_fields(body), 1))
    return {
        uint_value(decode_fields(item), 1): uint_value(decode_fields(item), 2)
        for item in bytes_values(room_value, 12)
    }


def test_client_writes_inactive_situation_without_waiting():
    client, session, reads = make_client(room_message(levels=(5, 5, 0, 1)))
    assert client.set_level(1, 3, 2) == "snapshot"
    assert entries(session.bodies[0]) == {0: 5, 1: 5, 2: 3, 3: 1}
    assert len(reads) == 1  # no wait loop for a situation that is not active


def test_client_rejects_unknown_situation():
    client, session, _reads = make_client(room_message(levels=(5, 5)))
    with pytest.raises(ValueError):
        client.set_level(1, 3, 3)
    assert session.bodies == []


# --- temporary change sensor -------------------------------------------------------


def temporary_sensor(message):
    coordinator = FakeCoordinator(snapshot(message))
    return next(
        entity
        for entity in setup_platform(sensor, coordinator)
        if entity.entity_description.key == "temporary_until"
    )


def test_temporary_change_sensor_reports_end_time():
    entity = temporary_sensor(room_message(until=UNTIL))
    assert entity.native_value == datetime.fromtimestamp(UNTIL, UTC)
    assert entity.native_value.isoformat() == "2026-10-01T19:00:00+00:00"
    assert entity.available is True


def test_temporary_change_sensor_is_unknown_without_change():
    entity = temporary_sensor(room_message())
    assert entity.native_value is None
    assert entity.available is True


# --- diagnostics -------------------------------------------------------------------


def test_diagnostics_report_summer_and_temporary_change():
    coordinator = FakeCoordinator(snapshot(room_message(until=UNTIL), summer=True))
    entry = SimpleNamespace(runtime_data=coordinator, version=1)
    result = asyncio.run(diagnostics.async_get_config_entry_diagnostics(None, entry))
    assert result["gateway"]["summer_ventilation"] is True
    assert result["attached_devices"][0]["temporary_change_active"] is True
    assert "Wohnzimmer" not in json_bytes(result).decode()
