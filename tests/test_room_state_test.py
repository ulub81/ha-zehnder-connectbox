"""Offline checks for the temporary room-record test write (fork main only)."""

from __future__ import annotations

import asyncio
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from custom_components.zehnder_connectbox import button  # noqa: E402
from custom_components.zehnder_connectbox import client as client_mod  # noqa: E402
from custom_components.zehnder_connectbox.coordinator import (  # noqa: E402
    ZehnderConnectBoxCoordinator,
)
from custom_components.zehnder_connectbox.models import (  # noqa: E402
    GatewaySnapshot,
    Room,
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
    ProtocolError,
    decode_rooms,
    decode_run_state,
    encode_room_state_test,
    room_state_record,
)
from custom_components.zehnder_connectbox.session import (  # noqa: E402
    GatewayResponseError,
)
from homeassistant.exceptions import HomeAssistantError  # noqa: E402

# Records as reported for the living room: fans (class 38) after supply-only
# operation was switched off, and the later class-41 record.
FAN_OFF = bytes.fromhex("002034140020783f")
FAN_ON = bytes.fromhex("012034140020783f")
OTHER = bytes.fromhex("0102000000000000")


def record(class_id: int, value: bytes) -> bytes:
    return encode_bytes(
        101, encode_bytes(1, encode_uint(1, class_id)) + encode_bytes(2, value)
    )


def device_message(device_id=1, variant=1, alarm_fields=True) -> bytes:
    prop_key = b"".join(
        encode_uint(n, v)
        for n, v in ((1, 30), (2, 255), (3, 0), (4, 38), (5, 0), (6, 5))
    )
    alarms = (
        [encode_uint(30, 9), encode_uint(31, 1), encode_uint(41, 1234)]
        if alarm_fields
        else []
    )
    return b"".join(
        [
            encode_uint(1, device_id),
            encode_string(2, f"SN{device_id}"),
            encode_uint(5, 30),
            encode_bytes(8, encode_uint(1, 2) + encode_uint(2, 51)),
            encode_uint(20, 1),
            *alarms,
            encode_bytes(101, encode_bytes(1, prop_key) + encode_bytes(2, b"\x01")),
            encode_uint(110, variant),
        ]
    )


LIVING_ROOM_RECORDS = (record(38, FAN_OFF), record(41, OTHER))


def room_fields(
    room_id=1,
    records=LIVING_ROOM_RECORDS,
    variant=1,
    alarm_fields=True,
):
    return [
        encode_uint(1, room_id),
        encode_string(2, "Wohnzimmer"),
        encode_bytes(
            8,
            device_message(
                device_id=room_id, variant=variant, alarm_fields=alarm_fields
            ),
        ),
        encode_bytes(19, encode_uint(1, 0) + encode_uint(2, 5)),
        encode_uint(21, 2),
        encode_uint(61, 1790881200),
        *records,
        encode_uint(110, 4),
    ]


def room_message(**kwargs) -> bytes:
    return encode_bytes(1, b"".join(room_fields(**kwargs)))


def the_room(message: bytes) -> Room:
    (room,) = decode_rooms(message)
    return room


def snapshot(*messages: bytes) -> GatewaySnapshot:
    rooms = tuple(room for message in messages for room in decode_rooms(message))
    run_state = decode_run_state(encode_bytes(1, encode_uint(1, 1) + encode_uint(2, 0)))
    return GatewaySnapshot(VersionInfo(None, None, None, None, None), run_state, rooms)


# --- record lookup and encoding ------------------------------------------------


def test_room_state_record_finds_the_class():
    room = the_room(room_message())
    assert room_state_record(room, 38) == FAN_OFF
    assert room_state_record(room, 41) == OTHER
    assert room_state_record(room, 39) is None
    assert room_state_record(the_room(room_message(records=())), 38) is None
    assert room_state_record(None, 38) is None


@pytest.mark.parametrize(("value", "expected"), [(1, FAN_ON), (0, FAN_OFF)])
def test_encode_changes_only_the_first_record_byte(value, expected):
    body = encode_room_state_test(the_room(room_message()), 38, value)
    outer = decode_fields(body)
    assert [field.number for field in outer] == [1]
    room = decode_fields(bytes_value(outer, 1))
    # Same room fields in the same order.
    assert [field.number for field in room] == [1, 2, 8, 19, 21, 61, 101, 101, 110]
    records = bytes_values(room, 101)
    assert bytes_value(decode_fields(records[0]), 2) == expected
    assert bytes_value(decode_fields(records[1]), 2) == OTHER
    assert bytes_value(room, 2) == b"Wohnzimmer"
    assert uint_value(room, 61) == 1790881200


def test_encode_leaves_out_device_alarm_fields_without_acknowledging():
    body = encode_room_state_test(the_room(room_message()), 38, 1)
    room = decode_fields(bytes_value(decode_fields(body), 1))
    device = decode_fields(bytes_value(room, 8))
    numbers = [field.number for field in device]
    assert 30 not in numbers and 31 not in numbers and 41 not in numbers
    assert uint_values(device, 31) == ()
    assert numbers == [1, 2, 5, 8, 20, 101, 110]


def test_encode_is_the_plain_room_write_when_the_byte_is_unchanged():
    body = encode_room_state_test(the_room(room_message()), 38, 0)
    assert body == encode_bytes(1, b"".join(room_fields(alarm_fields=False)))


def test_encode_changes_only_the_first_fan_record():
    records = (record(38, FAN_OFF), record(38, FAN_OFF))
    body = encode_room_state_test(the_room(room_message(records=records)), 38, 1)
    room = decode_fields(bytes_value(decode_fields(body), 1))
    first, second = bytes_values(room, 101)
    assert bytes_value(decode_fields(first), 2) == FAN_ON
    assert bytes_value(decode_fields(second), 2) == FAN_OFF


def test_encode_requires_a_record_and_a_byte():
    with pytest.raises(ProtocolError):
        encode_room_state_test(the_room(room_message(records=())), 38, 1)
    with pytest.raises(ProtocolError):
        empty = the_room(room_message(records=(record(38, b""),)))
        encode_room_state_test(empty, 38, 1)
    with pytest.raises(ValueError):
        encode_room_state_test(the_room(room_message()), 38, 256)


# --- client ------------------------------------------------------------------


class FakeGateway:
    """Answers room reads; a confirmed room write is stored when store=True."""

    def __init__(self, message, *, store=True, reject=False):
        self.message = message
        self.store = store
        self.reject = reject
        self.requests = []

    def request(self, request, confirm, body=b"", **_kw):
        self.requests.append((request, confirm, body))
        if self.reject:
            raise GatewayResponseError(2, None)
        if request is OperationType.SET_ROOM_REQUEST and self.store:
            self.message = body
        return SimpleNamespace(body=b"")


def make_client(gateway):
    client = client_mod.ConnectBoxClient(
        "192.0.2.1", uuid.uuid4(), uuid.uuid4(), "00" * 32
    )
    client._connected_session = lambda: gateway
    client._read_rooms = lambda: decode_rooms(gateway.message)
    client.read_snapshot = lambda **kw: ("snapshot", kw)
    client.closed = 0

    def close():
        client.closed += 1

    client.close = close
    client_mod.ROOM_STATE_SETTLE_TIMEOUT = 0.3
    client_mod.ROOM_STATE_POLL_INTERVAL = 0.05
    return client


@pytest.mark.parametrize("value", [1, 0])
def test_client_sends_the_room_write(value):
    message = room_message()
    gateway = FakeGateway(message)
    client = make_client(gateway)
    assert client.write_room_state_test(1, 38, value) == (
        "snapshot",
        {"refresh_properties": True},
    )
    [(request, confirm, body)] = gateway.requests
    assert request is OperationType.SET_ROOM_REQUEST
    assert confirm is OperationType.SET_ROOM_CONFIRM
    assert body == encode_room_state_test(the_room(message), 38, value)


def test_client_waits_briefly_when_the_gateway_keeps_the_old_byte():
    gateway = FakeGateway(room_message(), store=False)
    client = make_client(gateway)
    reads = []
    original = client._read_rooms

    def counting_read():
        reads.append(1)
        return original()

    client._read_rooms = counting_read
    client.write_room_state_test(1, 38, 1)
    # One read before the write, then polling until the short timeout.
    assert len(reads) > 3


@pytest.mark.parametrize(("class_id", "value"), [(41, 1), (38, 2), (38, 255)])
def test_client_refuses_other_writes(class_id, value):
    gateway = FakeGateway(room_message())
    with pytest.raises(ValueError):
        make_client(gateway).write_room_state_test(1, class_id, value)
    assert gateway.requests == []


@pytest.mark.parametrize(
    ("message", "room_id"),
    [
        (room_message(variant=2), 1),
        (room_message(), 2),
        (room_message(records=()), 1),
    ],
)
def test_client_refuses_other_rooms(message, room_id):
    gateway = FakeGateway(message)
    client = make_client(gateway)
    with pytest.raises(ProtocolError):
        client.write_room_state_test(room_id, 38, 1)
    assert gateway.requests == []
    assert client.closed == 1


def test_client_reports_rejection():
    gateway = FakeGateway(room_message(), reject=True)
    client = make_client(gateway)
    with pytest.raises(GatewayResponseError):
        client.write_room_state_test(1, 38, 1)
    assert client.closed == 1


# --- coordinator -------------------------------------------------------------


class FakeHass:
    async def async_add_executor_job(self, func, *args):
        return func(*args)


def make_coordinator(message, write):
    coordinator = object.__new__(ZehnderConnectBoxCoordinator)
    coordinator.hass = FakeHass()
    coordinator.data = snapshot(message)
    coordinator.client = SimpleNamespace(write_room_state_test=write)
    coordinator._io_lock = asyncio.Lock()
    coordinator._last_property_refresh = 0.0
    coordinator.candidate_writes = []
    coordinator.accepted = []
    coordinator._accept_command_snapshot = coordinator.accepted.append
    return coordinator


@pytest.mark.parametrize(
    ("after", "read_back"),
    [
        (room_message(records=(record(38, FAN_ON),)), 1),
        (room_message(), 0),
        (room_message(records=()), None),
    ],
)
def test_coordinator_logs_write_and_stored_byte(after, read_back):
    calls = []

    def write(room_id, class_id, value):
        calls.append((room_id, class_id, value))
        return snapshot(after)

    coordinator = make_coordinator(room_message(), write)
    asyncio.run(coordinator.async_write_room_state_test(1, 1))
    assert calls == [(1, 38, 1)]
    assert len(coordinator.accepted) == 1
    [entry] = coordinator.candidate_writes
    assert entry["device"] == 1
    assert entry["room"] == 1
    assert entry["target"] == "room record class 38 byte 0"
    assert entry["value"] == 1
    assert entry["result"] == "confirmed"
    assert entry["read_back"] == read_back
    assert entry["at"]


def test_coordinator_logs_rejection():
    def write(*_args):
        raise GatewayResponseError(2, None)

    coordinator = make_coordinator(room_message(), write)
    with pytest.raises(GatewayResponseError):
        asyncio.run(coordinator.async_write_room_state_test(1, 0))
    assert coordinator.candidate_writes[-1]["result"] == "rejected with result 2"
    assert "read_back" not in coordinator.candidate_writes[-1]
    assert coordinator.accepted == []


def test_coordinator_refuses_room_without_record():
    coordinator = make_coordinator(
        room_message(records=()), lambda *_args: snapshot(room_message())
    )
    with pytest.raises(ProtocolError):
        asyncio.run(coordinator.async_write_room_state_test(1, 1))
    assert coordinator.candidate_writes == []


# --- buttons -----------------------------------------------------------------


class FakeCoordinator:
    def __init__(self, data, error=None):
        self.data = data
        self.entry = SimpleNamespace(data={"gateway_uuid": "gw"})
        self.last_update_success = True
        self.error = error
        self.calls = []
        self.listeners = []

    def async_add_listener(self, callback):
        self.listeners.append(callback)
        return lambda: None

    async def async_write_room_state_test(self, device_id, value):
        self.calls.append((device_id, value))
        if self.error is not None:
            raise self.error


def room_buttons(coordinator):
    added = []
    entry = SimpleNamespace(
        runtime_data=coordinator, async_on_unload=lambda unsub: None
    )
    asyncio.run(
        button.async_setup_entry(None, entry, lambda entities: added.extend(entities))
    )
    return added, [
        entity
        for entity in added
        if isinstance(entity, button.ConnectBoxRoomStateTestButton)
    ]


def test_buttons_only_for_rooms_with_a_fan_record():
    data = snapshot(
        room_message(room_id=1),
        room_message(room_id=2, records=()),
        room_message(room_id=3, variant=2),
    )
    _added, entities = room_buttons(FakeCoordinator(data))
    assert [(e.device_id, e._value) for e in entities] == [(1, 1), (1, 0)]
    assert [e.translation_key for e in entities] == [
        "room_supply_only_test_1",
        "room_supply_only_test_0",
    ]
    assert entities[0].unique_id == "gw_1_room_supply_only_test_1"
    assert all(e.available for e in entities)


def test_buttons_follow_when_the_record_appears():
    coordinator = FakeCoordinator(snapshot(room_message(records=())))
    added, entities = room_buttons(coordinator)
    assert entities == []
    coordinator.data = snapshot(room_message())
    for listener in coordinator.listeners:
        listener()
    assert [
        (e.device_id, e._value)
        for e in added
        if isinstance(e, button.ConnectBoxRoomStateTestButton)
    ] == [(1, 1), (1, 0)]


def test_button_press_writes_value_and_reports_rejection():
    coordinator = FakeCoordinator(snapshot(room_message()))
    _added, (on, off) = room_buttons(coordinator)
    asyncio.run(on.async_press())
    asyncio.run(off.async_press())
    assert coordinator.calls == [(1, 1), (1, 0)]
    coordinator.error = GatewayResponseError(2, None)
    with pytest.raises(HomeAssistantError, match="= 1"):
        asyncio.run(on.async_press())


def test_buttons_unavailable_without_record():
    coordinator = FakeCoordinator(snapshot(room_message()))
    _added, (on, _off) = room_buttons(coordinator)
    coordinator.data = snapshot(room_message(records=()))
    assert on.available is False
