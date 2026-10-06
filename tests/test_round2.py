"""Offline checks for the situation select and the boost controls."""

from __future__ import annotations

import asyncio
import sys
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from custom_components.zehnder_connectbox import client as client_mod  # noqa: E402
from custom_components.zehnder_connectbox import select, sensor  # noqa: E402
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
    uint_values,
)
from custom_components.zehnder_connectbox.protocol import (  # noqa: E402
    OperationType,
    decode_rooms,
)
from custom_components.zehnder_connectbox.session import (  # noqa: E402
    GatewayResponseError,
)
from homeassistant.exceptions import ServiceValidationError  # noqa: E402


def room_message(*, room_id=1, boost_until=None, boost_duration=15, until=None):
    prop_key = b"".join(
        encode_uint(n, v) for n, v in ((1, 30), (2, 255), (3, 0), (4, 38), (5, 0), (6, 3))
    )
    device = b"".join(
        [
            encode_uint(1, 7),
            encode_string(2, "SN1"),
            encode_uint(5, 30),
            encode_bytes(8, encode_uint(1, 2) + encode_uint(2, 51)),
            encode_uint(20, 1),
            encode_uint(30, 9),
            encode_uint(31, 1),
            encode_uint(41, 1234),
            encode_bytes(101, encode_bytes(1, prop_key) + encode_bytes(2, b"\x60\x04")),
            encode_uint(110, 1),
        ]
    )
    room = [
        encode_uint(1, room_id),
        encode_string(2, "Wohnzimmer"),
        encode_bytes(8, device),
        encode_bytes(19, encode_uint(1, 0) + encode_uint(2, 5)),
        encode_bytes(19, encode_uint(1, 2) + encode_uint(2, 0)),
        encode_uint(21, 5),
    ]
    if boost_until is not None:
        room.append(encode_uint(18, boost_until))
    if boost_duration is not None:
        room.append(encode_uint(22, boost_duration))
    if until is not None:
        room.append(encode_uint(61, until))
    return encode_bytes(1, b"".join(room))


def run_state(run_mode=1, temperature_mode=0):
    return RunState(run_mode, temperature_mode, False, 0, False, None, ())


def snapshot(message, *, run_mode=1, temperature_mode=0):
    return GatewaySnapshot(
        VersionInfo(None, None, None, None, None),
        run_state(run_mode, temperature_mode),
        decode_rooms(message),
    )


# --- protocol ---------------------------------------------------------------------


def test_decode_boost_fields():
    (room,) = decode_rooms(room_message(boost_until=2_000_000_000, boost_duration=30))
    assert room.boost_until == 2_000_000_000
    assert room.boost_duration == 30
    assert room.boost_active(1_999_999_999)
    assert not room.boost_active(2_000_000_000)
    (idle,) = decode_rooms(room_message(boost_duration=15))
    assert idle.boost_until is None
    assert not idle.boost_active(time.time())


def room_fields(body):
    return decode_fields(bytes_value(decode_fields(body), 1))


# --- client -----------------------------------------------------------------------


class FakeGateway:
    """Answer client reads from mutable state and record writes."""

    def __init__(self, message, state, *, apply_boost=True, situation_result=None):
        self.message = message
        self.state = state
        self.apply_boost = apply_boost
        self.situation_result = situation_result
        self.requests = []

    def request(self, request, confirm, body=b"", **_kw):
        self.requests.append((request, body))
        if request is OperationType.SET_ROOM_REQUEST and self.apply_boost:
            fields = room_fields(body)
            boost = uint_value(fields, 18)
            self.message = room_message(boost_until=boost)
        if request is OperationType.SET_RUN_STATE_REQUEST:
            fields = decode_fields(body)
            mode, location = uint_value(fields, 1), uint_value(fields, 2)
            situation = self.situation_result
            if situation is None:
                situation = {1: 0, 2: 2}[location]
            self.state = run_state(mode, situation)
        return SimpleNamespace(body=b"")


def make_client(gateway):
    client = client_mod.ConnectBoxClient("192.0.2.1", uuid.uuid4(), uuid.uuid4(), "00" * 32)
    client._connected_session = lambda: gateway
    client._read_rooms = lambda: decode_rooms(gateway.message)
    client._read_run_state = lambda: gateway.state
    client.read_snapshot = lambda **_kw: "snapshot"
    client_mod.COMMAND_SETTLE_TIMEOUT = 0.3
    client_mod.LEVEL_SETTLE_POLL_INTERVAL = 0.05
    return client


@pytest.mark.parametrize(("mode", "location"), [(0, 1), (2, 2)])
def test_client_selects_situation(mode, location):
    gateway = FakeGateway(room_message(), run_state())
    client = make_client(gateway)
    assert client.set_situation(mode) == "snapshot"
    request, body = gateway.requests[0]
    assert request is OperationType.SET_RUN_STATE_REQUEST
    fields = decode_fields(body)
    assert (uint_value(fields, 1), uint_value(fields, 2)) == (2, location)


def test_client_reports_unexpected_situation():
    gateway = FakeGateway(room_message(), run_state(), situation_result=1)
    client = make_client(gateway)
    with pytest.raises(client_mod.ProtocolError, match="situation 1"):
        client.set_situation(0)


def test_client_rejects_unselectable_situation():
    client = make_client(FakeGateway(room_message(), run_state()))
    with pytest.raises(ValueError):
        client.set_situation(1)


# --- entities ---------------------------------------------------------------------


class FakeCoordinator:
    def __init__(self, data):
        self.data = data
        self.entry = SimpleNamespace(data={"gateway_uuid": "gw"})
        self.last_update_success = True
        self.calls = []

    def async_add_listener(self, callback):
        return lambda: None

    async def async_set_situation(self, mode):
        self.calls.append(("situation", mode))



def setup(module, coordinator):
    added = []
    entry = SimpleNamespace(runtime_data=coordinator, async_on_unload=lambda unsub: None)
    asyncio.run(module.async_setup_entry(None, entry, lambda entities: added.extend(entities)))
    return added


def situation_select(coordinator):
    return next(e for e in setup(select, coordinator) if isinstance(e, select.ConnectBoxSituation))


def test_situation_select_shows_and_selects():
    coordinator = FakeCoordinator(snapshot(room_message(), run_mode=1, temperature_mode=1))
    entity = situation_select(coordinator)
    assert entity.current_option == "asleep"
    assert entity.options == ["awake", "away", "asleep"]
    asyncio.run(entity.async_select_option("away"))
    assert coordinator.calls == [("situation", 2)]
    with pytest.raises(ServiceValidationError):
        asyncio.run(entity.async_select_option("asleep"))


def test_situation_select_switches_schedule_to_manual():
    coordinator = FakeCoordinator(snapshot(room_message(), run_mode=1, temperature_mode=0))
    entity = situation_select(coordinator)
    assert entity.options == ["awake", "away"]
    asyncio.run(entity.async_select_option("awake"))
    assert coordinator.calls == [("situation", 0)]


def test_situation_select_ignores_active_manual_situation():
    coordinator = FakeCoordinator(snapshot(room_message(), run_mode=2, temperature_mode=2))
    entity = situation_select(coordinator)
    asyncio.run(entity.async_select_option("away"))
    assert coordinator.calls == []


def boost_sensor(coordinator):
    return next(
        e for e in setup(sensor, coordinator) if e.entity_description.key == "boost_until"
    )


def test_boost_until_sensor():
    future = int(time.time()) + 600
    entity = boost_sensor(FakeCoordinator(snapshot(room_message(boost_until=future))))
    assert entity.native_value.timestamp() == future
    expired = boost_sensor(FakeCoordinator(snapshot(room_message(boost_until=100))))
    assert expired.native_value is None
    assert expired.available is True


# --- summer-ventilation role ------------------------------------------------------


def role_message(role):
    keys = [((38, 0, 9), b"\x3c"), ((38, 0, 10), b"\x00")]
    if role is not None:
        keys.append(((38, 0, 11), bytes([role])))
    props = b""
    for key, value in keys:
        key_message = b"".join(
            encode_uint(n, v) for n, v in zip((1, 2, 3, 4, 5, 6), (30, 255, 0, *key), strict=True)
        )
        props += encode_bytes(101, encode_bytes(1, key_message) + encode_bytes(2, value))
    device = encode_uint(1, 7) + encode_uint(5, 30) + props + encode_uint(110, 1)
    return encode_bytes(1, encode_uint(1, 1) + encode_bytes(8, device))


@pytest.mark.parametrize(
    ("raw", "state"), [(0, "supply_and_exhaust"), (1, "supply"), (2, "exhaust"), (7, None), (None, None)]
)
def test_summer_ventilation_role_sensor(raw, state):
    coordinator = FakeCoordinator(snapshot(role_message(raw)))
    entities = [
        e for e in setup(sensor, coordinator) if e.entity_description.key == "summer_ventilation_role"
    ]
    if state is None:
        # Created only once the unit reports a usable role.
        assert entities == []
        return
    (entity,) = entities
    assert entity.native_value == state
    assert entity.options == ["supply_and_exhaust", "supply", "exhaust"]


def property_identities(body):
    fields = decode_fields(body)
    key = decode_fields(bytes_value(fields, 3))
    return uint_value(fields, 1), tuple(uint_value(key, n) for n in (4, 5, 6))


def property_client(reject_optional=False):
    client = client_mod.ConnectBoxClient("192.0.2.1", uuid.uuid4(), uuid.uuid4(), "00" * 32)
    sent = []
    closed = []

    def request(request, confirm, body=b"", **_kw):
        command, identity = property_identities(body)
        sent.append((command, identity))
        if reject_optional and identity[:2] == (38, 0) and identity[2] in (9, 10, 11):
            raise GatewayResponseError(2, None)
        return SimpleNamespace(body=b"")

    def read_rooms():
        sent.append("rooms")
        return decode_rooms(role_message(1))

    session = SimpleNamespace(request=request)
    client._connected_session = lambda: session
    client._read_rooms = read_rooms
    client.close = lambda: closed.append(True)
    return client, sent, closed


OPTIONAL = ((38, 0, 9), (38, 0, 10), (38, 0, 11))


def optional_requests(sent):
    return [item for item in sent if item != "rooms" and item[1] in OPTIONAL]


def test_optional_role_sequence_follows_main_sequence():
    client, sent, closed = property_client()
    rooms = decode_rooms(role_message(1))
    client._read_device_properties(rooms, include_filter_properties=True)
    optional = optional_requests(sent)
    assert optional == [(0, (38, 0, 9)), (1, (38, 0, 10)), (2, (38, 0, 11))]
    # The core sequence is complete and read back before the optional one, and
    # the room model is read once more afterwards.
    first_optional = sent.index(optional[0])
    assert "rooms" in sent[:first_optional]
    assert all(item == "rooms" or item[1] not in OPTIONAL for item in sent[:first_optional])
    assert sent[-4:] == [*optional, "rooms"]
    assert closed == []


def test_optional_role_sequence_only_with_slow_refresh():
    client, sent, _closed = property_client()
    rooms = decode_rooms(role_message(1))
    client._read_device_properties(rooms, include_filter_properties=True)
    sent.clear()
    client._read_device_properties(rooms, include_filter_properties=False)
    assert optional_requests(sent) == []
    assert sent[-1] == "rooms"


def test_rejected_optional_sequence_keeps_telemetry():
    client, sent, closed = property_client(reject_optional=True)
    rooms = decode_rooms(role_message(1))
    result = client._read_device_properties(rooms, include_filter_properties=True)
    assert closed == [True]
    assert result and result[0].room_id == 1
    assert (0, (25, 0, 1)) in sent
    # The rooms read before the rejected sequence are returned.
    assert sent.count("rooms") == 1


def test_rejected_optional_sequence_is_not_requested_again():
    client, sent, closed = property_client(reject_optional=True)
    rooms = decode_rooms(role_message(1))
    client._read_device_properties(rooms, include_filter_properties=True)
    client._remember_device_properties(rooms)
    sent.clear()
    client._read_device_properties(rooms, include_filter_properties=True)
    assert optional_requests(sent) == []
    assert (0, (25, 0, 1)) in sent
    assert closed == [True]


def test_filter_reset_read_back_requests_no_optional_sequence():
    client, sent, closed = property_client()
    rooms = decode_rooms(role_message(1))
    expected = {(7, (38, 0, 9)): b"\x3c"}
    client._read_device_properties(
        rooms, include_filter_properties=True, expected_property_values=expected
    )
    assert optional_requests(sent) == []
    assert closed == []
