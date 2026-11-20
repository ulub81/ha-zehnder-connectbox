"""Check the test build's role write, boost duration and capture offline."""

import asyncio
import tempfile
import time
import unittest
from dataclasses import replace
from datetime import datetime
from types import MappingProxyType, SimpleNamespace
from unittest.mock import Mock, patch
from uuid import UUID

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError

from custom_components.zehnder_connectbox import client as client_mod
from custom_components.zehnder_connectbox import (
    diagnostics,
    number,
    profiles,
    select,
    sensor,
    status,
    switch,
)
from custom_components.zehnder_connectbox.client import CaptureError, ConnectBoxClient
from custom_components.zehnder_connectbox.coordinator import (
    ZehnderConnectBoxCoordinator,
)
from custom_components.zehnder_connectbox.errors import CommandValidationError
from custom_components.zehnder_connectbox.models import (
    GatewaySnapshot,
    PropertyKey,
    RunState,
    VersionInfo,
)
from custom_components.zehnder_connectbox.protobuf import (
    bytes_value,
    bytes_values,
    decode_fields,
    encode_bytes,
    encode_string,
    encode_uint,
    uint_value,
)
from custom_components.zehnder_connectbox.protocol import (
    Frame,
    Operation,
    OperationType,
    ProtocolError,
    decode_rooms,
    decode_run_state,
    encode_room_boost,
    encode_run_state,
    encode_room_number_update,
)
from custom_components.zehnder_connectbox.session import (
    ConnectBoxSession,
    GatewayResponseError,
)

GATEWAY = UUID(int=1)
APP = UUID(int=2)
ROLE = (38, 0, 11)
OPTIONAL = ((38, 0, 9), (38, 0, 10), ROLE)


def property_record(identity, value):
    key = b"".join(
        encode_uint(number, part)
        for number, part in zip((1, 2, 3, 4, 5, 6), (30, 255, 0, *identity), strict=True)
    )
    return encode_bytes(101, encode_bytes(1, key) + encode_bytes(2, value))


def rooms_message(
    *, role=1, boost_duration=15, boost_until=None, runtime=60, extra=(), errors=()
):
    """One room with one ComfoSpot 50 in the shape the gateway reports."""
    device = [
        encode_uint(1, 7),
        encode_string(2, "SERIAL-0001"),
        encode_uint(5, 30),
        *(encode_uint(10, code) for code in errors),
        encode_uint(30, 9),
        encode_uint(31, 1),
        encode_uint(41, 1234),
        property_record((38, 0, 9), bytes((runtime,))),
        property_record((38, 0, 10), b"\x00"),
    ]
    if role is not None:
        device.append(property_record(ROLE, bytes((role,))))
    device.extend(property_record(identity, value) for identity, value in extra)
    device.append(encode_uint(110, 1))
    room = [
        encode_uint(1, 3),
        encode_string(2, "Arbeitszimmer"),
        encode_bytes(8, b"".join(device)),
    ]
    if boost_until is not None:
        room.append(encode_uint(18, boost_until))
    room.append(encode_bytes(19, encode_uint(1, 0) + encode_uint(2, 2)))
    room.append(encode_uint(21, 2))
    if boost_duration is not None:
        room.append(encode_uint(22, boost_duration))
    return encode_bytes(1, b"".join(room))


def run_state(*, summer=False, run_mode=1):
    return RunState(run_mode, 0, False, 0, summer, None, ())


def snapshot(message, **state):
    return GatewaySnapshot(
        VersionInfo(None, None, None, None, None),
        run_state(**state),
        decode_rooms(message),
    )


def written_room(body):
    return decode_fields(bytes_value(decode_fields(body), 1))


class FakeGateway:
    """Answer the client's reads from mutable state and record its requests."""

    def __init__(self, *, apply=True, summer=False, run_mode=1, boost_shift=0, **room):
        self.room = room
        self.apply = apply
        self.summer = summer
        self.run_mode = run_mode
        # Seconds the gateway adds to a requested boost end.
        self.boost_shift = boost_shift
        self.requests = []

    @property
    def message(self):
        return rooms_message(**self.room)

    def request(self, request, confirm, body=b"", **_kwargs):
        self.requests.append((request, body))
        if not self.apply:
            return SimpleNamespace(body=b"")
        if request is OperationType.SET_ROOM_REQUEST:
            self.room["boost_duration"] = uint_value(written_room(body), 22)
        if request is OperationType.SET_DEVICE_PROPERTIES_REQUEST:
            record = decode_fields(bytes_value(decode_fields(body), 3))
            key = decode_fields(bytes_value(record, 1))
            identity = tuple(uint_value(key, number) for number in (4, 5, 6))
            name = {ROLE: "role", (38, 0, 9): "runtime"}[identity]
            self.room[name] = bytes_value(record, 2)[0]
        if request is OperationType.SET_ROOM_VALUE_REQUEST:
            until = uint_value(written_room(body), 15)
            self.room["boost_until"] = until + self.boost_shift if until else None
        return SimpleNamespace(body=b"")


def make_client(gateway):
    client = ConnectBoxClient("test.invalid", GATEWAY, APP, "a" * 64)
    client._connected_session = lambda: gateway
    client._read_rooms = lambda: decode_rooms(gateway.message)
    client._read_run_state = lambda: run_state(
        summer=gateway.summer, run_mode=gateway.run_mode
    )
    client.read_snapshot = lambda **_kwargs: "snapshot"
    return client


def role_key(client):
    (room,) = client._read_rooms()
    return next(
        value.key
        for value in room.devices[0].properties
        if value.key.value_identity == ROLE
    )


class FastSettle(unittest.TestCase):
    """Shorten the read-back waits of the client."""

    def setUp(self):
        for name, value in (
            ("COMMAND_SETTLE_TIMEOUT", 0.2),
            ("LEVEL_SETTLE_POLL_INTERVAL", 0.02),
        ):
            patcher = patch.object(client_mod, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)


class RoomNumberUpdateTests(unittest.TestCase):
    def test_changes_only_the_given_field_and_drops_read_only_values(self):
        (room,) = decode_rooms(rooms_message(boost_duration=15))
        fields = written_room(encode_room_number_update(room, {22: 45}))
        self.assertEqual(uint_value(fields, 22), 45)
        self.assertEqual(bytes_value(fields, 2), b"Arbeitszimmer")
        self.assertEqual(uint_value(fields, 21), 2)
        self.assertEqual([field.number for field in fields], [1, 2, 8, 19, 21, 22])
        device = decode_fields(bytes_value(fields, 8))
        self.assertEqual(uint_value(device, 1), 7)
        self.assertEqual(len(bytes_values(device, 101)), 3)
        for read_only in (30, 31, 41):
            self.assertIsNone(uint_value(device, read_only))

    def test_adds_a_missing_field_in_field_order(self):
        (room,) = decode_rooms(rooms_message(boost_duration=None))
        fields = written_room(encode_room_number_update(room, {20: 1, 22: 30}))
        self.assertEqual([field.number for field in fields], [1, 2, 8, 19, 20, 21, 22])
        self.assertEqual((uint_value(fields, 20), uint_value(fields, 22)), (1, 30))

    def test_refuses_structural_fields_and_invalid_values(self):
        (room,) = decode_rooms(rooms_message())
        for values in ({}, {1: 4}, {8: 1}, {19: 1}, {22: -1}, {22: True}, {22: "30"}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                encode_room_number_update(room, values)


class BoostDurationClientTests(FastSettle):
    def test_writes_the_room_and_reads_the_duration_back(self):
        gateway = FakeGateway(boost_duration=15)
        self.assertEqual(make_client(gateway).set_boost_duration(3, 45), "snapshot")
        ((request, body),) = gateway.requests
        self.assertIs(request, OperationType.SET_ROOM_REQUEST)
        fields = written_room(body)
        self.assertEqual((uint_value(fields, 1), uint_value(fields, 22)), (3, 45))

    def test_refuses_values_the_app_does_not_offer(self):
        gateway = FakeGateway(boost_duration=15)
        client = make_client(gateway)
        for minutes in (0, 10, 20, 125, 135, True, 30.0):
            with self.subTest(minutes=minutes):
                with self.assertRaises(CommandValidationError) as raised:
                    client.set_boost_duration(3, minutes)
                self.assertEqual(
                    raised.exception.translation_key, "invalid_boost_duration"
                )
        self.assertEqual(gateway.requests, [])

    def test_refuses_unknown_rooms_running_boosts_and_rooms_without_duration(self):
        cases = (
            (FakeGateway(boost_duration=15), 9, "room_unavailable"),
            (
                FakeGateway(boost_duration=15, boost_until=int(time.time()) + 600),
                3,
                "boost_active",
            ),
            (FakeGateway(boost_duration=None), 3, "boost_unsupported"),
        )
        for gateway, room_id, key in cases:
            with self.subTest(key=key):
                with self.assertRaises(CommandValidationError) as raised:
                    make_client(gateway).set_boost_duration(room_id, 30)
                self.assertEqual(raised.exception.translation_key, key)
                self.assertEqual(gateway.requests, [])

    def test_reports_a_duration_the_gateway_did_not_take_over(self):
        client = make_client(FakeGateway(boost_duration=15, apply=False))
        client.close = Mock()
        with self.assertRaisesRegex(ProtocolError, "boost duration"):
            client.set_boost_duration(3, 45)
        client.close.assert_called_once_with()


class BoostClientTests(FastSettle):
    def boost_writes(self, gateway):
        return [
            (uint_value(written_room(body), 1), uint_value(written_room(body), 15))
            for request, body in gateway.requests
            if request is OperationType.SET_ROOM_VALUE_REQUEST
        ]

    def test_room_value_update_carries_only_room_and_boost_end(self):
        fields = written_room(encode_room_boost(3, 1_800_000_900))
        self.assertEqual([field.number for field in fields], [1, 15])
        self.assertEqual(
            (uint_value(fields, 1), uint_value(fields, 15)), (3, 1_800_000_900)
        )
        for room_id, until in ((-1, 0), (3, -1), (True, 0), (3, 1.5)):
            with self.subTest(room_id=room_id, until=until):
                with self.assertRaises(ValueError):
                    encode_room_boost(room_id, until)

    def test_starts_a_boost_for_the_room_duration(self):
        gateway = FakeGateway(boost_duration=30)
        before = int(time.time())
        self.assertEqual(make_client(gateway).set_boost(3, True), "snapshot")
        ((room_id, until),) = self.boost_writes(gateway)
        self.assertEqual(room_id, 3)
        self.assertGreaterEqual(until, before + 30 * 60)
        self.assertLessEqual(until, int(time.time()) + 30 * 60)
        self.assertEqual(
            [request for request, _body in gateway.requests],
            [OperationType.SET_ROOM_VALUE_REQUEST],
        )

    def test_stops_a_running_boost(self):
        gateway = FakeGateway(boost_until=int(time.time()) + 600)
        self.assertEqual(make_client(gateway).set_boost(3, False), "snapshot")
        self.assertEqual(self.boost_writes(gateway), [(3, 0)])
        self.assertIsNone(gateway.room["boost_until"])

    def test_does_not_write_when_the_room_is_already_in_that_state(self):
        for gateway, active in (
            (FakeGateway(boost_until=int(time.time()) + 600), True),
            (FakeGateway(), False),
        ):
            with self.subTest(active=active):
                self.assertEqual(make_client(gateway).set_boost(3, active), "snapshot")
                self.assertEqual(gateway.requests, [])

    def test_refuses_other_modes_and_rooms_without_boost(self):
        cases = (
            (FakeGateway(run_mode=4), 3, "boost_mode_unavailable"),
            (FakeGateway(run_mode=3), 3, "boost_mode_unavailable"),
            (FakeGateway(boost_duration=None), 3, "boost_unsupported"),
            (FakeGateway(boost_duration=10), 3, "boost_duration_unavailable"),
            (FakeGateway(), 9, "room_unavailable"),
        )
        for gateway, room_id, key in cases:
            with self.subTest(key=key, run_mode=gateway.run_mode):
                with self.assertRaises(CommandValidationError) as raised:
                    make_client(gateway).set_boost(room_id, True)
                self.assertEqual(raised.exception.translation_key, key)
                self.assertEqual(gateway.requests, [])

    def test_reports_a_boost_the_gateway_did_not_start(self):
        gateway = FakeGateway(apply=False)
        client = make_client(gateway)
        client.close = Mock()
        with self.assertRaisesRegex(ProtocolError, "did not start"):
            client.set_boost(3, True)
        client.close.assert_called_once_with()

    def test_stops_a_boost_that_would_run_longer_than_its_duration(self):
        gateway = FakeGateway(boost_shift=3600)
        client = make_client(gateway)
        client.close = Mock()
        with self.assertRaisesRegex(ProtocolError, "unexpected boost end"):
            client.set_boost(3, True)
        writes = self.boost_writes(gateway)
        self.assertEqual(len(writes), 2)
        self.assertEqual(writes[1], (3, 0))
        self.assertIsNone(gateway.room["boost_until"])


class SummerRoleClientTests(FastSettle):
    def test_writes_the_property_and_requests_it_again(self):
        gateway = FakeGateway(role=1)
        client = make_client(gateway)
        key = role_key(client)
        self.assertEqual(client.set_summer_ventilation_role(7, key, 2), "snapshot")

        write, *reads = gateway.requests
        self.assertIs(write[0], OperationType.SET_DEVICE_PROPERTIES_REQUEST)
        fields = decode_fields(write[1])
        self.assertEqual((uint_value(fields, 1), uint_value(fields, 2)), (2, 7))
        record = decode_fields(bytes_value(fields, 3))
        written_key = decode_fields(bytes_value(record, 1))
        self.assertEqual(
            tuple(uint_value(written_key, number) for number in (1, 4, 5, 6)),
            (30, *ROLE),
        )
        self.assertEqual(bytes_value(record, 2), b"\x02")

        # The role is read back in its verified three-item sequence.
        self.assertEqual(
            [request for request, _body in reads],
            [OperationType.DEVICE_PROPERTIES_REQUEST] * 3,
        )
        sequence = []
        for _request, body in reads:
            fields = decode_fields(body)
            key_fields = decode_fields(bytes_value(fields, 3))
            sequence.append(
                (
                    uint_value(fields, 1),
                    tuple(uint_value(key_fields, number) for number in (4, 5, 6)),
                )
            )
        self.assertEqual(sequence, list(zip((0, 1, 2), OPTIONAL, strict=True)))
        # The read-back value is kept for the following snapshots.
        cached = {
            value.key.value_identity: value.value
            for values in client._property_cache.values()
            for value in values
        }
        self.assertEqual(cached[ROLE], b"\x02")

    def test_refuses_unknown_roles_and_running_intervals_without_writing(self):
        gateway = FakeGateway(role=1)
        client = make_client(gateway)
        key = role_key(client)
        with self.assertRaises(CommandValidationError) as raised:
            client.set_summer_ventilation_role(7, key, 3)
        self.assertEqual(raised.exception.translation_key, "invalid_summer_role")
        gateway.summer = True
        with self.assertRaises(CommandValidationError) as raised:
            client.set_summer_ventilation_role(7, key, 2)
        self.assertEqual(
            raised.exception.translation_key, "summer_role_interval_active"
        )
        gateway.summer = False
        with self.assertRaises(CommandValidationError) as raised:
            client.set_summer_ventilation_role(99, key, 2)
        self.assertEqual(raised.exception.translation_key, "device_unavailable")
        self.assertEqual(gateway.requests, [])

    def test_refuses_another_property(self):
        client = make_client(FakeGateway(role=1))
        with self.assertRaises(ValueError):
            client.set_summer_ventilation_role(
                7, PropertyKey(30, 255, 0, 38, 0, 5), 1
            )

    def test_reports_a_role_the_unit_did_not_take_over(self):
        gateway = FakeGateway(role=1, apply=False)
        client = make_client(gateway)
        client.close = Mock()
        with self.assertRaisesRegex(ProtocolError, "summer ventilation role"):
            client.set_summer_ventilation_role(7, role_key(client), 2)
        client.close.assert_called_once_with()


class FakeCoordinator:
    def __init__(self, data):
        self.data = data
        self.entry = SimpleNamespace(data={"gateway_uuid": "gateway"})
        self.last_update_success = True
        self.calls = []

    def async_add_listener(self, _callback):
        return lambda: None

    async def async_set_summer_ventilation_role(self, device_id, role):
        self.calls.append(("role", device_id, role))

    async def async_set_boost_duration(self, room_id, minutes):
        self.calls.append(("boost_duration", room_id, minutes))

    async def async_set_boost(self, room_id, active):
        self.calls.append(("boost", room_id, active))


def set_up(module, coordinator):
    added = []
    entry = SimpleNamespace(
        runtime_data=coordinator, async_on_unload=lambda _unsubscribe: None
    )
    asyncio.run(
        module.async_setup_entry(None, entry, lambda entities: added.extend(entities))
    )
    return added


class EntityTests(unittest.TestCase):
    def role_selects(self, coordinator):
        return [
            entity
            for entity in set_up(select, coordinator)
            if isinstance(entity, select.ConnectBoxSummerVentilationRole)
        ]

    def test_role_select_shows_and_writes_the_role(self):
        coordinator = FakeCoordinator(snapshot(rooms_message(role=1)))
        (entity,) = self.role_selects(coordinator)
        self.assertEqual(entity.current_option, "supply")
        self.assertEqual(entity.options, ["supply_and_exhaust", "supply", "exhaust"])
        self.assertTrue(entity.available)
        asyncio.run(entity.async_select_option("supply"))
        self.assertEqual(coordinator.calls, [])
        asyncio.run(entity.async_select_option("exhaust"))
        self.assertEqual(coordinator.calls, [("role", 7, 2)])

    def test_role_select_needs_a_reported_known_role(self):
        for role in (None, 7):
            with self.subTest(role=role):
                coordinator = FakeCoordinator(snapshot(rooms_message(role=role)))
                self.assertEqual(self.role_selects(coordinator), [])

    def boost_durations(self, coordinator):
        return [
            entity
            for entity in set_up(number, coordinator)
            if isinstance(entity, number.ConnectBoxBoostDuration)
        ]

    def test_boost_duration_shows_and_writes_the_room_value(self):
        coordinator = FakeCoordinator(snapshot(rooms_message(boost_duration=15)))
        (entity,) = self.boost_durations(coordinator)
        self.assertEqual(entity.native_value, 15)
        self.assertEqual(
            (entity.native_min_value, entity.native_max_value, entity.native_step),
            (15, 120, 15),
        )
        asyncio.run(entity.async_set_native_value(45.0))
        self.assertEqual(coordinator.calls, [("boost_duration", 3, 45)])
        for value in (10, 50, 37.5, 135):
            with self.subTest(value=value), self.assertRaises(ServiceValidationError):
                asyncio.run(entity.async_set_native_value(value))
        self.assertEqual(len(coordinator.calls), 1)

    def boost_switches(self, coordinator):
        return [
            entity
            for entity in set_up(switch, coordinator)
            if isinstance(entity, switch.ConnectBoxBoostSwitch)
        ]

    def test_boost_switch_follows_the_room_and_writes(self):
        end = int(time.time()) + 600
        coordinator = FakeCoordinator(snapshot(rooms_message(boost_until=end)))
        (entity,) = self.boost_switches(coordinator)
        self.assertTrue(entity.is_on)
        self.assertTrue(entity.available)
        self.assertEqual(entity.extra_state_attributes["duration_minutes"], 15)
        self.assertEqual(
            datetime.fromisoformat(
                entity.extra_state_attributes["ends_at"]
            ).timestamp(),
            end,
        )
        asyncio.run(entity.async_turn_off())
        asyncio.run(entity.async_turn_on())
        self.assertEqual(coordinator.calls, [("boost", 3, False), ("boost", 3, True)])
        expired = FakeCoordinator(snapshot(rooms_message(boost_until=100)))
        (idle,) = self.boost_switches(expired)
        self.assertFalse(idle.is_on)
        self.assertIsNone(idle.extra_state_attributes["ends_at"])

    def test_capture_switch_selects_the_unit(self):
        coordinator = FakeCoordinator(snapshot(rooms_message()))
        coordinator.capture_device_ids = set()
        (entity,) = [
            item
            for item in set_up(switch, coordinator)
            if isinstance(item, switch.ConnectBoxPropertyCaptureSwitch)
        ]
        entity.async_write_ha_state = Mock()
        self.assertFalse(entity.is_on)
        asyncio.run(entity.async_turn_on())
        self.assertEqual(coordinator.capture_device_ids, {7})
        self.assertTrue(entity.is_on)
        asyncio.run(entity.async_turn_off())
        self.assertEqual(coordinator.capture_device_ids, set())

    def test_boost_switch_needs_a_reported_duration(self):
        coordinator = FakeCoordinator(snapshot(rooms_message(boost_duration=None)))
        self.assertEqual(self.boost_switches(coordinator), [])

    def test_boost_duration_needs_a_reported_duration(self):
        coordinator = FakeCoordinator(snapshot(rooms_message(boost_duration=None)))
        self.assertEqual(self.boost_durations(coordinator), [])


class CoordinatorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.hass = HomeAssistant(self.directory.name)
        entry = ConfigEntry(
            version=1,
            minor_version=1,
            domain="zehnder_connectbox",
            title="Synthetic",
            unique_id=str(GATEWAY),
            data={"gateway_uuid": str(GATEWAY)},
            options={},
            source="user",
            discovery_keys=MappingProxyType({}),
            subentries_data=None,
        )
        self.client = ConnectBoxClient("test.invalid", GATEWAY, APP, "a" * 64)
        self.coordinator = ZehnderConnectBoxCoordinator(self.hass, entry, self.client)
        # The bare test instance has no device registry for accepted snapshots.
        self.coordinator._register_devices = Mock()
        self.coordinator.data = snapshot(rooms_message(role=1, boost_duration=15))

    async def asyncTearDown(self):
        await self.hass.async_stop()

    async def test_role_write_uses_the_reported_key(self):
        result = snapshot(rooms_message(role=2))
        self.client.set_summer_ventilation_role = Mock(return_value=result)
        await self.coordinator.async_set_summer_ventilation_role(7, 2)
        device_id, key, role = self.client.set_summer_ventilation_role.call_args.args
        self.assertEqual((device_id, key.value_identity, role), (7, ROLE, 2))
        self.assertIs(self.coordinator.data, result)

    async def test_role_write_needs_a_device_that_reports_its_role(self):
        self.client.set_summer_ventilation_role = Mock()
        for device_id, message, key in (
            (99, rooms_message(role=1), "device_unavailable"),
            (7, rooms_message(role=None), "summer_role_unavailable"),
        ):
            with self.subTest(key=key):
                self.coordinator.data = snapshot(message)
                with self.assertRaises(ServiceValidationError) as raised:
                    await self.coordinator.async_set_summer_ventilation_role(
                        device_id, 2
                    )
                self.assertEqual(raised.exception.translation_key, key)
        self.client.set_summer_ventilation_role.assert_not_called()

    async def test_boost_duration_outcomes_are_logged_for_the_capture(self):
        result = snapshot(rooms_message(boost_duration=45))
        self.client.set_boost_duration = Mock(return_value=result)
        await self.coordinator.async_set_boost_duration(3, 45)

        self.client.set_boost_duration = Mock(
            side_effect=GatewayResponseError(3, "synthetic refusal")
        )
        with self.assertRaises(ServiceValidationError):
            await self.coordinator.async_set_boost_duration(3, 60)

        self.client.set_boost_duration = Mock(
            side_effect=ProtocolError("gateway did not report the written value")
        )
        with self.assertRaises(HomeAssistantError) as raised:
            await self.coordinator.async_set_boost_duration(3, 75)
        self.assertEqual(raised.exception.translation_key, "command_failed")

        log = self.coordinator.candidate_writes
        self.assertEqual(
            [(item["write"], item["room"], item["value"]) for item in log],
            [("boost_duration", 3, 45), ("boost_duration", 3, 60), ("boost_duration", 3, 75)],
        )
        self.assertEqual(log[0]["result"], "confirmed")
        self.assertEqual(
            log[0]["read_back"],
            {"boost_duration": 45, "boost_ends_in_seconds": None},
        )
        self.assertEqual(log[1]["result"], "rejected: result 3: synthetic refusal")
        self.assertEqual(
            log[2]["result"],
            "ProtocolError: gateway did not report the written value",
        )

    async def test_boost_start_and_stop_are_logged_with_the_duration(self):
        result = snapshot(rooms_message(boost_until=int(time.time()) + 900))
        self.client.set_boost = Mock(return_value=result)
        await self.coordinator.async_set_boost(3, True)
        self.client.set_boost.assert_called_once_with(3, True)
        await self.coordinator.async_set_boost(3, False)
        log = self.coordinator.candidate_writes
        self.assertEqual(
            [(item["write"], item["value"], item["result"]) for item in log],
            [("boost_start", 15, "confirmed"), ("boost_stop", 0, "confirmed")],
        )
        self.assertGreater(log[0]["read_back"]["boost_ends_in_seconds"], 800)

    async def test_capture_reads_only_the_selected_units(self):
        self.client.capture = Mock(return_value=("state", (), {}))
        await self.coordinator.async_capture()
        targets = self.client.capture.call_args.args[0]
        self.assertEqual(targets, ())
        self.coordinator.capture_device_ids.add(7)
        await self.coordinator.async_capture()
        targets = self.client.capture.call_args.args[0]
        self.assertEqual([(device_id, product) for device_id, product, _groups in targets], [(7, 30)])

    async def test_unsolicited_frames_are_copied_for_the_diagnostics(self):
        self.client._record_unsolicited(Operation(type=207), b"\x08\x01")
        frames = await self.coordinator.async_unsolicited_frames()
        count, last = frames[207]
        self.assertEqual(count, 1)
        self.assertEqual(last[0][1:], (None, None, b"\x08\x01"))


class FakeTransport:
    def __init__(self, frames):
        self.pending = list(frames)
        self.written = []

    def set_timeout(self, _timeout):
        pass

    def write(self, data):
        self.written.append(data)

    def read(self):
        return self.pending.pop(0)


def gateway_frame(operation, body=b""):
    return Frame(GATEWAY, APP, operation.encode(), body).encode()


class UnsolicitedFrameTests(unittest.TestCase):
    def test_session_reports_frames_that_answer_no_request(self):
        seen = []
        transport = FakeTransport(
            [
                gateway_frame(Operation(type=207), b"\x08\x03"),
                gateway_frame(Operation(type=195, reference=1), b"answer"),
            ]
        )
        session = ConnectBoxSession(
            transport,
            GATEWAY,
            APP,
            on_unsolicited=lambda operation, body: seen.append((operation.type, body)),
        )
        response = session.request(194, 195)
        self.assertEqual(response.body, b"answer")
        self.assertEqual(seen, [(207, b"\x08\x03")])

    def test_session_still_skips_them_without_a_listener(self):
        transport = FakeTransport(
            [
                gateway_frame(Operation(type=207), b"\x08\x03"),
                gateway_frame(Operation(type=195, reference=1), b"answer"),
            ]
        )
        session = ConnectBoxSession(transport, GATEWAY, APP)
        self.assertEqual(session.request(194, 195).body, b"answer")

    def test_client_counts_frames_and_keeps_the_latest_bodies(self):
        client = ConnectBoxClient("test.invalid", GATEWAY, APP, "a" * 64)
        samples = client_mod.UNSOLICITED_SAMPLES
        for index in range(samples + 2):
            client._record_unsolicited(Operation(type=207), bytes((8, index)))
        entry = client.unsolicited_frames[207]
        self.assertEqual(entry.count, samples + 2)
        self.assertEqual(
            [body for _at, _reference, _result, body in entry.last],
            [bytes((8, index)) for index in range(2, samples + 2)],
        )
        for operation_type in range(1000, 1000 + client_mod.UNSOLICITED_TYPE_LIMIT + 5):
            client._record_unsolicited(Operation(type=operation_type), b"")
        self.assertEqual(
            len(client.unsolicited_frames), client_mod.UNSOLICITED_TYPE_LIMIT
        )

    def test_diagnostics_show_structure_without_names_or_identities(self):
        other_app = UUID(int=77).bytes
        body = b"".join(
            (
                encode_uint(1, 3),
                encode_string(2, "Arbeitszimmer"),
                encode_bytes(5, other_app),
                encode_bytes(6, GATEWAY.bytes),
                encode_bytes(12, encode_uint(1, 0) + encode_uint(2, 4)),
                encode_bytes(13, b"\x3c"),
                encode_bytes(14, encode_bytes(1, encode_string(2, "SERIAL-0001"))),
            )
        )
        shown = diagnostics._unsolicited_frames(
            {207: (4, ((1_800_000_000.0, None, None, body),))},
            {GATEWAY.bytes: "gateway_uuid"},
        )
        entry = shown["207"]
        self.assertEqual(entry["count"], 4)
        (frame,) = entry["last"]
        self.assertEqual(frame["at"], "2027-01-15T08:00:00+00:00")
        self.assertEqual(
            frame["body"],
            {
                "f1": 3,
                "f2": "text(len=13)",
                "f5": "bytes(len=16)",
                "f6": "same as gateway_uuid",
                "f12": {
                    "hex": "08001004",
                    "uint_le": 68157448,
                    "as_fields": {"f1": 0, "f2": 4},
                },
                "f13": {"hex": "3c", "uint_le": 60},
                "f14": {"f1": {"f2": "text(len=11)"}},
            },
        )
        text = repr(shown)
        for private in ("Arbeitszimmer", "SERIAL", other_app.hex()):
            self.assertNotIn(private, text)

    def test_diagnostics_open_a_sixteen_byte_room_value_update(self):
        update = b"".join(
            (
                encode_uint(1, 3),
                encode_uint(2, 100),
                encode_uint(7, 0),
                encode_uint(8, 1),
                encode_uint(11, 1795204800),
                encode_uint(14, 5),
            )
        )
        self.assertEqual(len(update), 16)
        shown = diagnostics._unsolicited_frames(
            {207: (1, ((1_800_000_000.0, None, None, encode_bytes(1, update)),))}, {}
        )
        self.assertEqual(
            shown["207"]["last"][0]["body"],
            {"f1": {"f1": 3, "f2": 100, "f7": 0, "f8": 1, "f11": 1795204800, "f14": 5}},
        )

    def test_diagnostics_show_an_empty_body_as_none(self):
        shown = diagnostics._unsolicited_frames(
            {300: (1, ((1_800_000_000.0, 9, 2, b""),))}, {}
        )
        self.assertEqual(
            shown["300"]["last"][0],
            {
                "at": "2027-01-15T08:00:00+00:00",
                "reference": 9,
                "result": 2,
                "body": None,
            },
        )


class CaptureRobustnessTests(unittest.TestCase):
    def setUp(self):
        for name, value in (
            ("CAPTURE_RETRY_PAUSE", 0),
            ("CAPTURE_POLL_INTERVAL", 0),
            ("CAPTURE_SETTLE_TIMEOUT", 0),
        ):
            patcher = patch.object(client_mod, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = ConnectBoxClient("test.invalid", GATEWAY, APP, "a" * 64)
        self.client.close = Mock()
        self.groups = (((38, 0, 9),), ((38, 0, 13),), ((36, 0, 3),), ((36, 1, 3),))

    def test_skips_the_remaining_groups_after_repeated_failures(self):
        attempts = []

        def refuse():
            attempts.append(True)
            raise client_mod.TransportError("unable to establish a connection")

        self.client._connected_session = refuse
        self.client._read_rooms = lambda: decode_rooms(rooms_message())
        self.client._read_run_state = lambda: run_state()
        _state, _rooms, result = self.client.capture(
            ((7, 30, self.groups),), deadline=time.monotonic() + 30
        )
        self.assertEqual(len(attempts), client_mod.CAPTURE_FAILURE_LIMIT)
        self.assertTrue(result[7]["errors"][-1].endswith("skipped after 3 failures"))
        self.assertIn("TransportError", result[7]["errors"][0])

    def test_final_read_is_retried_and_failure_keeps_the_group_errors(self):
        session = SimpleNamespace(
            request=Mock(side_effect=GatewayResponseError(2, "refused"))
        )
        self.client._connected_session = lambda: session
        reads = []

        def failing_read():
            reads.append(True)
            raise client_mod.TransportError("gateway read failed")

        self.client._read_rooms = failing_read
        with self.assertRaises(CaptureError) as raised:
            self.client.capture(((7, 30, self.groups[:1]),), deadline=time.monotonic() + 30)
        self.assertEqual(len(reads), 2)
        self.assertIn("TransportError: gateway read failed", str(raised.exception))
        self.assertIn("refused", raised.exception.errors[7][0])

    def test_diagnostics_show_the_capture_error_and_group_errors(self):
        async def failing_capture():
            raise CaptureError("TransportError: gateway read failed", {7: ["38.0.9: x"]})

        async def frames():
            return {}

        coordinator = FakeCoordinator(snapshot(rooms_message()))
        coordinator.async_capture = failing_capture
        coordinator.async_unsolicited_frames = frames
        entry = SimpleNamespace(
            runtime_data=coordinator, version=1, data={"gateway_uuid": str(GATEWAY)}
        )
        result = asyncio.run(diagnostics.async_get_config_entry_diagnostics(None, entry))
        capture = result["capture"]
        self.assertEqual(capture["source"], "last poll")
        self.assertEqual(
            capture["error"], "CaptureError: TransportError: gateway read failed"
        )
        self.assertEqual(capture["group_errors"], {"7": ["38.0.9: x"]})


# --- unit settings, error log, schedules ------------------------------------------


ERROR_LOG = (
    ((37, 0, 1), b"\x85"),
    ((37, 1, 1), b"\x85"),
    ((37, 2, 1), b"\x00"),
    ((37, 0, 2), (27733).to_bytes(2, "little")),
    ((37, 1, 2), (10505).to_bytes(2, "little")),
    ((37, 2, 2), b"\x00\x00"),
)


def device_of(message):
    (room,) = decode_rooms(message)
    return room.devices[0]


class ProfileTests(unittest.TestCase):
    def test_error_log_is_history_with_signed_codes(self):
        device = device_of(rooms_message(extra=ERROR_LOG))
        self.assertEqual(
            profiles.error_log(device),
            [
                {"code": -123, "operating_hours": 27733},
                {"code": -123, "operating_hours": 10505},
            ],
        )
        self.assertFalse(profiles.has_fault(device))
        self.assertEqual(status.error_codes(device), [])
        active = device_of(rooms_message(extra=ERROR_LOG, errors=(4, 4)))
        self.assertTrue(profiles.has_fault(active))
        self.assertEqual(status.error_codes(active), [4])

    def test_unit_settings(self):
        device = device_of(
            rooms_message(runtime=45, extra=(((38, 0, 13), b"\x00"), ((38, 0, 14), b"\x03")))
        )
        self.assertEqual(profiles.level_one_runtime(device), 45)
        self.assertEqual(profiles.level_one_runtime_key(device).value_identity, (38, 0, 9))
        self.assertEqual(profiles.bath_function(device), "locked")
        self.assertEqual(profiles.automatic_sensors(device), "both")
        odd = device_of(rooms_message(runtime=50, extra=(((38, 0, 14), b"\x07"),)))
        self.assertIsNone(profiles.level_one_runtime(odd))
        self.assertIsNone(profiles.level_one_runtime_key(odd))
        self.assertIsNone(profiles.automatic_sensors(odd))


def run_state_message(*, week_plan=1, slots=((0, 1), (480, 0), (1260, 1)), name=b"Wintertagesplan"):
    plan = encode_uint(1, week_plan) + encode_bytes(2, name)
    for minute, mode in slots:
        slot = (encode_uint(1, minute) if minute else b"") + (
            encode_uint(2, mode) if mode else b""
        )
        plan += encode_bytes(3, slot)
    inner = b"".join(
        (
            encode_uint(1, 1),
            encode_uint(2, 0),
            encode_uint(3, 1),
            encode_bytes(4, plan),
            encode_uint(5, week_plan),
            encode_uint(6, 3),
            encode_uint(12, 0),
        )
    )
    return encode_bytes(1, inner)


class ScheduleProtocolTests(unittest.TestCase):
    def test_run_state_reports_week_and_day_plan(self):
        state = decode_run_state(run_state_message())
        self.assertEqual(state.week_plan, 1)
        self.assertEqual(state.day_plan.plan_id, 1)
        self.assertEqual(state.day_plan.name, "Wintertagesplan")
        self.assertEqual(state.day_plan.slots, ((0, 1), (480, 0), (1260, 1)))
        summer = decode_run_state(
            run_state_message(week_plan=2, slots=((0, 1), (480, 2), (1350, 0)))
        )
        self.assertEqual((summer.week_plan, summer.day_plan.slots[1]), (2, (480, 2)))

    def test_run_state_without_plan(self):
        inner = encode_uint(1, 2) + encode_uint(2, 0)
        state = decode_run_state(encode_bytes(1, inner))
        self.assertIsNone(state.week_plan)
        self.assertIsNone(state.day_plan)

    def test_run_state_update_carries_the_week_plan_candidate(self):
        fields = decode_fields(encode_run_state(1, week_plan=2))
        self.assertEqual(
            [(field.number, field.value) for field in fields], [(1, 1), (2, 1), (5, 2)]
        )
        self.assertEqual(len(decode_fields(encode_run_state(1))), 2)


class SettingClientTests(FastSettle):
    def runtime_key(self, client):
        (room,) = client._read_rooms()
        return next(
            value.key
            for value in room.devices[0].properties
            if value.key.value_identity == (38, 0, 9)
        )

    def test_writes_the_runtime_and_reads_it_back(self):
        gateway = FakeGateway(runtime=45)
        client = make_client(gateway)
        self.assertEqual(
            client.set_level_one_runtime(7, self.runtime_key(client), 60), "snapshot"
        )
        write = gateway.requests[0]
        self.assertIs(write[0], OperationType.SET_DEVICE_PROPERTIES_REQUEST)
        record = decode_fields(bytes_value(decode_fields(write[1]), 3))
        self.assertEqual(bytes_value(record, 2), b"\x3c")
        self.assertEqual(gateway.room["runtime"], 60)

    def test_refuses_values_the_app_does_not_offer(self):
        gateway = FakeGateway(runtime=45)
        client = make_client(gateway)
        key = self.runtime_key(client)
        for minutes in (0, 10, 50, 90, True):
            with self.subTest(minutes=minutes):
                with self.assertRaises(CommandValidationError):
                    client.set_level_one_runtime(7, key, minutes)
        with self.assertRaises(ValueError):
            client.set_level_one_runtime(7, PropertyKey(30, 255, 0, *ROLE), 60)
        self.assertEqual(gateway.requests, [])

    def test_reports_a_runtime_the_unit_did_not_take_over(self):
        client = make_client(FakeGateway(runtime=45, apply=False))
        client.close = Mock()
        with self.assertRaisesRegex(ProtocolError, "level-one runtime"):
            client.set_level_one_runtime(7, self.runtime_key(client), 60)


class SlowSequenceTests(unittest.TestCase):
    def make(self, reject=()):
        client = ConnectBoxClient("test.invalid", GATEWAY, APP, "a" * 64)
        sent = []
        client.close = Mock()

        def request(request, confirm, body=b"", **_kwargs):
            fields = decode_fields(body)
            key = decode_fields(bytes_value(fields, 3))
            identity = tuple(uint_value(key, number) for number in (4, 5, 6))
            sent.append(identity)
            if identity in reject:
                raise GatewayResponseError(2, None)
            return SimpleNamespace(body=b"")

        client._connected_session = lambda: SimpleNamespace(request=request)
        client._read_rooms = lambda: decode_rooms(rooms_message())
        return client, sent

    def test_settings_and_error_hours_follow_with_the_slow_refresh(self):
        client, sent = self.make()
        rooms = decode_rooms(rooms_message())
        client._read_device_properties(rooms, include_filter_properties=True)
        self.assertEqual(
            sent[-5:],
            [(38, 0, 13), (38, 0, 14), (37, 0, 2), (37, 1, 2), (37, 2, 2)],
        )
        sent.clear()
        client._read_device_properties(rooms, include_filter_properties=False)
        self.assertNotIn((38, 0, 14), sent)
        self.assertNotIn((37, 0, 2), sent)

    def test_a_rejected_sequence_is_skipped_until_reload(self):
        client, sent = self.make(reject={(38, 0, 13)})
        rooms = decode_rooms(rooms_message())
        client._read_device_properties(rooms, include_filter_properties=True)
        client.close.assert_called_once_with()
        self.assertNotIn((37, 0, 2), sent)
        sent.clear()
        client._read_device_properties(rooms, include_filter_properties=True)
        self.assertNotIn((38, 0, 13), sent)
        self.assertIn((37, 0, 2), sent)


class WeekPlanClientTests(FastSettle):
    def make(self, *, run_mode=1, apply=True):
        state = {"week_plan": 1, "run_mode": run_mode}
        requests = []

        def request(request, confirm, body=b"", **_kwargs):
            requests.append((request, body))
            if apply:
                state["week_plan"] = uint_value(decode_fields(body), 5)
            return SimpleNamespace(body=b"")

        client = ConnectBoxClient("test.invalid", GATEWAY, APP, "a" * 64)
        client._connected_session = lambda: SimpleNamespace(request=request)
        client._read_run_state = lambda: replace(
            run_state(run_mode=state["run_mode"]), week_plan=state["week_plan"]
        )
        client.read_snapshot = lambda **_kwargs: "snapshot"
        client.close = Mock()
        return client, state, requests

    def test_switches_the_week_plan_in_schedule_mode(self):
        client, state, requests = self.make()
        self.assertEqual(client.set_week_plan(2), "snapshot")
        ((request, body),) = requests
        self.assertIs(request, OperationType.SET_RUN_STATE_REQUEST)
        fields = decode_fields(body)
        self.assertEqual((uint_value(fields, 1), uint_value(fields, 5)), (1, 2))
        self.assertEqual(state["week_plan"], 2)

    def test_does_not_write_the_active_plan_or_outside_schedule_mode(self):
        client, _state, requests = self.make()
        self.assertEqual(client.set_week_plan(1), "snapshot")
        client, _state, requests2 = self.make(run_mode=2)
        with self.assertRaises(CommandValidationError) as raised:
            client.set_week_plan(2)
        self.assertEqual(raised.exception.translation_key, "week_plan_mode_unavailable")
        for plan in (0, 9, True):
            with self.subTest(plan=plan), self.assertRaises(CommandValidationError):
                client.set_week_plan(plan)
        self.assertEqual(requests + requests2, [])

    def test_reports_a_plan_the_gateway_did_not_take_over(self):
        client, _state, _requests = self.make(apply=False)
        with self.assertRaisesRegex(ProtocolError, "weekly schedule"):
            client.set_week_plan(2)
        client.close.assert_called_once_with()


class ScheduleAndSettingEntityTests(unittest.TestCase):
    def snapshot_with_plan(self, message, **kwargs):
        data = snapshot(message, **kwargs)
        return replace(
            data,
            run_state=replace(
                decode_run_state(run_state_message()), run_mode=kwargs.get("run_mode", 1)
            ),
        )

    def test_level_one_runtime_number(self):
        coordinator = FakeCoordinator(snapshot(rooms_message(runtime=45)))
        coordinator.calls = []

        async def write(device_id, minutes):
            coordinator.calls.append((device_id, minutes))

        coordinator.async_set_level_one_runtime = write
        (entity,) = [
            item
            for item in set_up(number, coordinator)
            if isinstance(item, number.ConnectBoxLevelOneRuntime)
        ]
        self.assertEqual(entity.native_value, 45)
        self.assertEqual(
            (entity.native_min_value, entity.native_max_value, entity.native_step),
            (15, 60, 15),
        )
        asyncio.run(entity.async_set_native_value(60.0))
        asyncio.run(entity.async_set_native_value(45.0))
        self.assertEqual(coordinator.calls, [(7, 60)])
        for value in (20, 37.5, 90):
            with self.subTest(value=value), self.assertRaises(ServiceValidationError):
                asyncio.run(entity.async_set_native_value(value))

    def test_week_plan_number_follows_the_run_state(self):
        coordinator = FakeCoordinator(self.snapshot_with_plan(rooms_message()))
        calls = []

        async def write(plan):
            calls.append(plan)

        coordinator.async_set_week_plan = write
        (entity,) = [
            item
            for item in set_up(number, coordinator)
            if isinstance(item, number.ConnectBoxWeekPlan)
        ]
        self.assertEqual(entity.native_value, 1)
        self.assertTrue(entity.available)
        asyncio.run(entity.async_set_native_value(2.0))
        self.assertEqual(calls, [2])
        coordinator.data = self.snapshot_with_plan(rooms_message(), run_mode=2)
        self.assertFalse(entity.available)

    def test_setting_error_log_and_day_plan_sensors(self):
        message = rooms_message(
            extra=(*ERROR_LOG, ((38, 0, 13), b"\x00"), ((38, 0, 14), b"\x03"))
        )
        coordinator = FakeCoordinator(self.snapshot_with_plan(message))
        entities = set_up(sensor, coordinator)
        by_key = {
            getattr(getattr(entity, "entity_description", None), "key", None): entity
            for entity in entities
        }
        self.assertEqual(by_key["bath_function"].native_value, "locked")
        self.assertEqual(by_key["automatic_sensors"].native_value, "both")
        self.assertEqual(by_key["error_log"].native_value, 2)
        self.assertEqual(
            by_key["error_log"].extra_state_attributes["entries"][0],
            {"code": -123, "operating_hours": 27733},
        )
        plan = next(e for e in entities if isinstance(e, sensor.ConnectBoxDayPlanSensor))
        self.assertEqual(plan.native_value, "Wintertagesplan")
        self.assertEqual(
            plan.extra_state_attributes,
            {
                "day_plan": 1,
                "week_plan": 1,
                "slots": [
                    {"start": "00:00", "situation": "asleep"},
                    {"start": "08:00", "situation": "awake"},
                    {"start": "21:00", "situation": "asleep"},
                ],
            },
        )


if __name__ == "__main__":
    unittest.main()
