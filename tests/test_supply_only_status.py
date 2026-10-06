"""Offline checks for the supply-only operation status."""

from __future__ import annotations

import asyncio
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from custom_components.zehnder_connectbox import binary_sensor, diagnostics  # noqa: E402
from custom_components.zehnder_connectbox import client as client_mod  # noqa: E402
from custom_components.zehnder_connectbox.models import (  # noqa: E402
    GatewaySnapshot,
    RunState,
    VersionInfo,
)
from custom_components.zehnder_connectbox.profiles import (  # noqa: E402
    FAN_STATE_PROPERTY_SPECS,
    fan_state_property_specs_for_device,
    supply_only_operation,
)
from custom_components.zehnder_connectbox.protobuf import (  # noqa: E402
    bytes_value,
    decode_fields,
    encode_bytes,
    encode_string,
    encode_uint,
    uint_value,
)
from custom_components.zehnder_connectbox.protocol import decode_rooms  # noqa: E402
from custom_components.zehnder_connectbox.session import (  # noqa: E402
    GatewayResponseError,
)

FAN_STATE = ((38, 0, 5), (38, 0, 6), (38, 0, 8))


def prop(key, value: bytes) -> bytes:
    key_message = b"".join(
        encode_uint(number, part)
        for number, part in zip(
            (1, 2, 3, 4, 5, 6), (30, 255, 0, *key), strict=True
        )
    )
    return encode_bytes(101, encode_bytes(1, key_message) + encode_bytes(2, value))


def room_message(exhaust_enabled: bytes | None = b"\x01", *, variant=1) -> bytes:
    properties = [prop((38, 0, 3), b"\x60\x04")]
    if exhaust_enabled is not None:
        properties.append(prop((38, 0, 5), exhaust_enabled))
    device = b"".join(
        [
            encode_uint(1, 1),
            encode_string(2, "SN1"),
            encode_uint(5, 30),
            encode_uint(20, 1),
            *properties,
            encode_uint(110, variant),
        ]
    )
    room = b"".join(
        [
            encode_uint(1, 1),
            encode_string(2, "Wohnzimmer"),
            encode_bytes(8, device),
            encode_bytes(19, encode_uint(1, 0) + encode_uint(2, 2)),
            encode_uint(21, 2),
        ]
    )
    return encode_bytes(1, room)


def device_of(message: bytes):
    (room,) = decode_rooms(message)
    return room.devices[0]


def snapshot(message: bytes) -> GatewaySnapshot:
    run_state = RunState(1, 0, False, 0, False, None, ())
    return GatewaySnapshot(
        VersionInfo(None, None, None, None, None), run_state, decode_rooms(message)
    )


# --- interpretation ----------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(b"\x00", True), (b"\x01", False), (b"\x02", None), (None, None)],
)
def test_supply_only_follows_the_exhaust_fan_flag(raw, expected):
    assert supply_only_operation(device_of(room_message(raw))) is expected


def test_supply_only_only_for_the_checked_profile():
    device = device_of(room_message(b"\x00", variant=2))
    assert supply_only_operation(device) is None
    assert fan_state_property_specs_for_device(device) == ()


def test_fan_state_sequence_is_the_verified_one():
    device = device_of(room_message())
    specs = fan_state_property_specs_for_device(device)
    assert specs == FAN_STATE_PROPERTY_SPECS
    assert [spec.key for spec in specs] == list(FAN_STATE)


# --- reading -----------------------------------------------------------------


def property_client(reject=()):
    client = client_mod.ConnectBoxClient(
        "192.0.2.1", uuid.uuid4(), uuid.uuid4(), "00" * 32
    )
    sent = []
    closed = []

    def request(request, confirm, body=b"", **_kw):
        fields = decode_fields(body)
        key = decode_fields(bytes_value(fields, 3))
        identity = tuple(uint_value(key, n) for n in (4, 5, 6))
        sent.append((uint_value(fields, 1), identity))
        if identity in reject:
            raise GatewayResponseError(2, None)
        return SimpleNamespace(body=b"")

    def read_rooms():
        sent.append("rooms")
        return decode_rooms(room_message())

    session = SimpleNamespace(request=request)
    client._connected_session = lambda: session
    client._read_rooms = read_rooms
    client.close = lambda: closed.append(True)
    return client, sent, closed


def fan_state_requests(sent):
    return [item for item in sent if item != "rooms" and item[1] in FAN_STATE]


@pytest.mark.parametrize("slow_refresh", [True, False])
def test_fan_state_sequence_is_read_with_every_refresh(slow_refresh):
    client, sent, closed = property_client()
    rooms = decode_rooms(room_message())
    client._read_device_properties(rooms, include_filter_properties=True)
    if not slow_refresh:
        sent.clear()
        client._read_device_properties(rooms, include_filter_properties=False)
    fan_state = fan_state_requests(sent)
    assert fan_state == [(0, FAN_STATE[0]), (1, FAN_STATE[1]), (2, FAN_STATE[2])]
    # The core telemetry is read back before the fan state is requested, and
    # the room model is read again afterwards.
    first = sent.index(fan_state[0])
    assert "rooms" in sent[:first]
    assert sent[-1] == "rooms"
    assert closed == []


def test_rejected_fan_state_sequence_keeps_telemetry():
    client, sent, closed = property_client(reject={(38, 0, 5)})
    rooms = decode_rooms(room_message())
    result = client._read_device_properties(rooms, include_filter_properties=True)
    assert closed == [True]
    assert result and result[0].room_id == 1
    assert (0, (25, 0, 1)) in sent
    # The rooms read before the rejected sequence are returned.
    assert sent.count("rooms") == 1

    # The unit's fan state is not requested again; its settings are.
    client._remember_device_properties(rooms)
    sent.clear()
    client._read_device_properties(rooms, include_filter_properties=True)
    assert fan_state_requests(sent) == []
    assert (2, (38, 0, 11)) in sent
    assert closed == [True]


def test_filter_reset_read_back_requests_no_fan_state():
    client, sent, closed = property_client()
    rooms = decode_rooms(room_message())
    client._read_device_properties(
        rooms,
        include_filter_properties=True,
        expected_property_values={(1, (38, 0, 3)): b"\x60\x04"},
    )
    assert fan_state_requests(sent) == []
    assert closed == []


def test_no_fan_state_sequence_for_other_profiles():
    client, sent, _closed = property_client()
    client._read_rooms = lambda: decode_rooms(room_message(variant=2))
    rooms = decode_rooms(room_message(variant=2))
    client._read_device_properties(rooms, include_filter_properties=True)
    assert not any(item[1] in FAN_STATE for item in sent)


# --- entity and diagnostics ----------------------------------------------------


class FakeCoordinator:
    def __init__(self, data):
        self.data = data
        self.entry = SimpleNamespace(data={"gateway_uuid": "gw"})
        self.last_update_success = True

    def async_add_listener(self, callback):
        return lambda: None


def supply_only_entities(message: bytes):
    added = []
    entry = SimpleNamespace(
        runtime_data=FakeCoordinator(snapshot(message)),
        async_on_unload=lambda unsub: None,
    )
    asyncio.run(
        binary_sensor.async_setup_entry(
            None, entry, lambda entities: added.extend(entities)
        )
    )
    return [e for e in added if e.entity_description.key == "supply_only"]


@pytest.mark.parametrize(
    ("raw", "state", "available"),
    [(b"\x00", True, True), (b"\x01", False, True), (None, None, False)],
)
def test_supply_only_binary_sensor(raw, state, available):
    (entity,) = supply_only_entities(room_message(raw))
    assert entity.is_on is state
    assert entity.available is available
    assert entity.unique_id == "gw_1_supply_only"
    assert entity.translation_key == "supply_only"
    assert entity.entity_category is None


def test_no_supply_only_sensor_for_other_profiles():
    assert supply_only_entities(room_message(b"\x00", variant=2)) == []


def test_diagnostics_report_supply_only_operation():
    entry = SimpleNamespace(
        runtime_data=FakeCoordinator(snapshot(room_message(b"\x00"))),
        version=1,
        data={},
    )
    result = asyncio.run(diagnostics.async_get_config_entry_diagnostics(None, entry))
    assert result["attached_devices"][0]["supply_only_operation"] is True
