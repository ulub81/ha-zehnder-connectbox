"""Offline checks for the temporary 38.0.5 test write (fork main only)."""

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
    PropertyKey,
    VersionInfo,
)
from custom_components.zehnder_connectbox.profiles import (  # noqa: E402
    CANDIDATE_WRITE_LOG_SIZE,
    supply_only_candidate_key,
)
from custom_components.zehnder_connectbox.protobuf import (  # noqa: E402
    bytes_value,
    decode_fields,
    encode_bytes,
    encode_string,
    encode_uint,
    uint_value,
)
from custom_components.zehnder_connectbox.protocol import (  # noqa: E402
    OperationType,
    PropertySequenceCommand,
    decode_rooms,
    decode_run_state,
)
from custom_components.zehnder_connectbox.session import (  # noqa: E402
    GatewayResponseError,
)
from homeassistant.exceptions import HomeAssistantError  # noqa: E402

FAN_SPEED = ((38, 0, 3), b"\x60\x04")
CANDIDATE = ((38, 0, 5), b"\x01")


def prop(key, value: bytes, hardware: int = 255) -> bytes:
    key_message = b"".join(
        encode_uint(number, part)
        for number, part in zip(
            (1, 2, 3, 4, 5, 6), (30, hardware, 0, *key), strict=True
        )
    )
    return encode_bytes(101, encode_bytes(1, key_message) + encode_bytes(2, value))


def device_message(device_id=7, variant=1, properties=(FAN_SPEED,)) -> bytes:
    return b"".join(
        [
            encode_uint(1, device_id),
            encode_string(2, f"SN{device_id}"),
            encode_uint(5, 30),
            encode_bytes(8, encode_uint(1, 2) + encode_uint(2, 51)),
            encode_uint(20, 1),
            *(prop(key, value) for key, value in properties),
            encode_uint(110, variant),
        ]
    )


def rooms_message(*devices: bytes) -> bytes:
    room = b"".join(
        [
            encode_uint(1, 1),
            encode_string(2, "Wohnzimmer"),
            *(encode_bytes(8, device) for device in devices),
            encode_bytes(19, encode_uint(1, 0) + encode_uint(2, 5)),
            encode_uint(21, 2),
        ]
    )
    return encode_bytes(1, room)


def first_device(message: bytes):
    return decode_rooms(message)[0].devices[0]


def snapshot(message: bytes) -> GatewaySnapshot:
    run_state = decode_run_state(encode_bytes(1, encode_uint(1, 1) + encode_uint(2, 0)))
    return GatewaySnapshot(
        VersionInfo(None, None, None, None, None), run_state, decode_rooms(message)
    )


# --- property key ------------------------------------------------------------


def test_candidate_key_uses_reported_key():
    device = first_device(
        rooms_message(device_message(properties=(FAN_SPEED, CANDIDATE)))
    )
    key = supply_only_candidate_key(device)
    assert key == PropertyKey(30, 255, 0, 38, 0, 5)


def test_candidate_key_derived_from_same_fan():
    message = rooms_message(
        device_message(properties=(((38, 1, 3), b"\x00"), ((38, 0, 15), b"\x00\x00")))
    )
    key = supply_only_candidate_key(first_device(message))
    assert key == PropertyKey(30, 255, 0, 38, 0, 5)


@pytest.mark.parametrize(
    ("variant", "properties"),
    [(2, (FAN_SPEED,)), (1, ()), (1, (((38, 1, 3), b"\x00"),))],
)
def test_candidate_key_needs_comfospot_fan_values(variant, properties):
    message = rooms_message(device_message(variant=variant, properties=properties))
    assert supply_only_candidate_key(first_device(message)) is None


# --- client ------------------------------------------------------------------


class FakeGateway:
    def __init__(self, message, reject=False):
        self.message = message
        self.reject = reject
        self.requests = []

    def request(self, request, confirm, body=b"", **_kw):
        self.requests.append((request, confirm, body))
        if self.reject:
            raise GatewayResponseError(2, None)
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
    return client


KEY = PropertyKey(30, 255, 0, 38, 0, 5)


@pytest.mark.parametrize("value", [1])
def test_client_writes_candidate(value):
    gateway = FakeGateway(rooms_message(device_message()))
    client = make_client(gateway)
    assert client.write_supply_only_candidate(7, KEY, value) == (
        "snapshot",
        {"refresh_properties": True},
    )
    [(request, confirm, body)] = gateway.requests
    assert request is OperationType.SET_DEVICE_PROPERTIES_REQUEST
    assert confirm is OperationType.SET_DEVICE_PROPERTIES_CONFIRM
    fields = decode_fields(body)
    assert uint_value(fields, 1) == int(PropertySequenceCommand.FINISH)
    assert uint_value(fields, 2) == 7
    entry = decode_fields(bytes_value(fields, 3))
    key_fields = decode_fields(bytes_value(entry, 1))
    assert [uint_value(key_fields, n) for n in range(1, 7)] == [30, 255, 0, 38, 0, 5]
    assert bytes_value(entry, 2) == bytes((value,))


@pytest.mark.parametrize(
    ("key", "value"),
    [(PropertyKey(30, 255, 0, 38, 0, 15), 1), (KEY, 0), (KEY, 2), (KEY, 255)],
)
def test_client_refuses_other_writes(key, value):
    gateway = FakeGateway(rooms_message(device_message()))
    with pytest.raises(ValueError):
        make_client(gateway).write_supply_only_candidate(7, key, value)
    assert gateway.requests == []


@pytest.mark.parametrize(
    ("message", "device_id"),
    [
        (rooms_message(device_message(variant=2)), 7),
        (rooms_message(device_message()), 8),
    ],
)
def test_client_refuses_other_devices(message, device_id):
    gateway = FakeGateway(message)
    client = make_client(gateway)
    with pytest.raises(client_mod.ProtocolError):
        client.write_supply_only_candidate(device_id, KEY, 1)
    assert gateway.requests == []
    assert client.closed == 1


def test_client_reports_rejection():
    gateway = FakeGateway(rooms_message(device_message()), reject=True)
    client = make_client(gateway)
    with pytest.raises(GatewayResponseError):
        client.write_supply_only_candidate(7, KEY, 1)
    assert client.closed == 1


# --- coordinator -------------------------------------------------------------


class FakeHass:
    async def async_add_executor_job(self, func, *args):
        return func(*args)


def make_coordinator(message, write):
    coordinator = object.__new__(ZehnderConnectBoxCoordinator)
    coordinator.hass = FakeHass()
    coordinator.data = snapshot(message)
    coordinator.client = SimpleNamespace(write_supply_only_candidate=write)
    coordinator._io_lock = asyncio.Lock()
    coordinator._last_property_refresh = 0.0
    coordinator.candidate_writes = []
    coordinator.accepted = []
    coordinator._accept_command_snapshot = coordinator.accepted.append
    return coordinator


def test_coordinator_logs_confirmed_write():
    calls = []

    def write(device_id, key, value):
        calls.append((device_id, key, value))
        return "new snapshot"

    coordinator = make_coordinator(rooms_message(device_message()), write)
    asyncio.run(coordinator.async_write_supply_only_candidate(7, 1))
    assert calls == [(7, KEY, 1)]
    assert coordinator.accepted == ["new snapshot"]
    [record] = coordinator.candidate_writes
    assert record["device"] == 7
    assert record["target"] == "property 38.0.5"
    assert record["value"] == 1
    assert record["result"] == "confirmed"
    assert record["at"]


@pytest.mark.parametrize(
    ("error", "result"),
    [
        (GatewayResponseError(2, None), "rejected with result 2"),
        (TimeoutError("no answer"), "failed: TimeoutError: no answer"),
    ],
)
def test_coordinator_logs_failed_write(error, result):
    def write(*_args):
        raise error

    coordinator = make_coordinator(rooms_message(device_message()), write)
    with pytest.raises(type(error)):
        asyncio.run(coordinator.async_write_supply_only_candidate(7, 1))
    assert coordinator.candidate_writes[-1]["result"] == result
    assert coordinator.accepted == []


def test_coordinator_keeps_a_short_log():
    coordinator = make_coordinator(
        rooms_message(device_message()), lambda *_args: "snapshot"
    )
    for _ in range(CANDIDATE_WRITE_LOG_SIZE + 5):
        asyncio.run(coordinator.async_write_supply_only_candidate(7, 1))
    assert len(coordinator.candidate_writes) == CANDIDATE_WRITE_LOG_SIZE


def test_coordinator_refuses_device_without_fan_values():
    coordinator = make_coordinator(
        rooms_message(device_message(properties=())), lambda *_args: "snapshot"
    )
    with pytest.raises(client_mod.ProtocolError):
        asyncio.run(coordinator.async_write_supply_only_candidate(7, 1))
    assert coordinator.candidate_writes == []


# --- buttons -----------------------------------------------------------------


class FakeCoordinator:
    def __init__(self, data, error=None):
        self.data = data
        self.entry = SimpleNamespace(data={"gateway_uuid": "gw"})
        self.last_update_success = True
        self.error = error
        self.calls = []

    def async_add_listener(self, callback):
        return lambda: None

    async def async_write_supply_only_candidate(self, device_id, value):
        self.calls.append((device_id, value))
        if self.error is not None:
            raise self.error


def candidate_buttons(coordinator):
    added = []
    entry = SimpleNamespace(
        runtime_data=coordinator, async_on_unload=lambda unsub: None
    )
    asyncio.run(
        button.async_setup_entry(None, entry, lambda entities: added.extend(entities))
    )
    return [
        entity
        for entity in added
        if isinstance(entity, button.ConnectBoxSupplyOnlyCandidateButton)
    ]


def test_buttons_for_comfospot_units_only():
    message = rooms_message(
        device_message(device_id=7),
        device_message(device_id=8, variant=2),
        device_message(device_id=9, properties=()),
    )
    entities = candidate_buttons(FakeCoordinator(snapshot(message)))
    assert [(e.device_id, e._value) for e in entities] == [(7, 1)]
    assert [e.translation_key for e in entities] == ["supply_only_candidate_1"]
    assert entities[0].unique_id == "gw_7_supply_only_candidate_1"
    assert all(e.available for e in entities)


def test_button_press_writes_value():
    coordinator = FakeCoordinator(snapshot(rooms_message(device_message())))
    [entity] = candidate_buttons(coordinator)
    asyncio.run(entity.async_press())
    assert coordinator.calls == [(7, 1)]


def test_button_reports_rejection():
    coordinator = FakeCoordinator(
        snapshot(rooms_message(device_message())), error=GatewayResponseError(2, None)
    )
    [entity] = candidate_buttons(coordinator)
    with pytest.raises(HomeAssistantError, match="38.0.5 = 1"):
        asyncio.run(entity.async_press())
