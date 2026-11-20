"""Check schedule editing, the absence planner and the history offline.

The expected bytes are the requests the ConnectBox accepted while the
messages were matched against the official app's message table.
"""

import asyncio
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock, patch

from homeassistant.exceptions import ServiceValidationError
from test_app_parity import (
    APP,
    GATEWAY,
    FakeCoordinator,
    day_plan_message,
    rooms_message,
    run_state_message,
    schedules_message,
    set_up,
    snapshot,
)

from custom_components.zehnder_connectbox import (
    binary_sensor,
    select,
    sensor,
    services,
    switch,
)
from custom_components.zehnder_connectbox import client as client_mod
from custom_components.zehnder_connectbox.client import ConnectBoxClient
from custom_components.zehnder_connectbox.errors import CommandValidationError
from custom_components.zehnder_connectbox.models import (
    ActionLogEntry,
    DayPlan,
    ScheduledFunction,
    VersionInfo,
    WeekPlan,
)
from custom_components.zehnder_connectbox.protobuf import (
    bytes_value,
    decode_fields,
    encode_bytes,
    encode_string,
    encode_uint,
    uint_value,
)
from custom_components.zehnder_connectbox.protocol import (
    OperationType,
    ProtocolError,
    decode_action_log,
    decode_day_plan_confirm,
    decode_remote_access,
    decode_run_state,
    decode_schedules,
    decode_week_plan_confirm,
    encode_active_week_plan,
    encode_day_plan,
    encode_plan_id,
    encode_standby_time_range,
    encode_week_plan,
)
from custom_components.zehnder_connectbox.session import GatewayResponseError

ABSENCE_START = 1796408100  # 04.12.2026 19:15 CET
ABSENCE_END = 1796494500  # 05.12.2026 19:15 CET


def absence_state(*, planned, start=ABSENCE_START, end=ABSENCE_END, week_plan=3):
    """Run state with the absence planner as the gateway reported it."""
    message = run_state_message(week_plan=week_plan)
    inner = bytes_value(decode_fields(message), 1)
    inner += encode_uint(7, 0) + encode_uint(8, int(planned))
    if planned:
        inner += encode_uint(9, start) + encode_uint(10, end)
    return encode_bytes(1, inner)


class EncodingTests(unittest.TestCase):
    def test_day_plan_with_boost_matches_the_accepted_request(self):
        plan = DayPlan(
            4,
            "HA Test",
            ((0, 1), (420, 0), (1380, 1)),
            (ScheduledFunction(420, 2, 15, 3),),
        )
        self.assertEqual(
            encode_day_plan(plan).hex(),
            "0a2d08041207484120546573741a04080010011a0508a40310001a0508e40a1001"
            "a2010b08a4031002180f20032800",
        )

    def test_new_week_plan_matches_the_accepted_request(self):
        self.assertEqual(
            encode_week_plan(WeekPlan(0, "HA Test", (4,) * 7)).hex(),
            "0a1908001207484120546573741804200428043004380440044804",
        )

    def test_week_plan_needs_seven_days_and_a_short_name(self):
        with self.assertRaises(ValueError):
            encode_week_plan(WeekPlan(0, "Woche", (1,) * 6))
        with self.assertRaises(ValueError):
            encode_week_plan(WeekPlan(0, "x" * 33, (1,) * 7))
        with self.assertRaises(ValueError):
            encode_day_plan(DayPlan(0, "Tag", ((1440, 0),)))

    def test_absence_and_active_week_plan_requests(self):
        self.assertEqual(
            encode_standby_time_range(True, ABSENCE_START, ABSENCE_END).hex(),
            "080110a486ccd80618a4a9d1d806",
        )
        self.assertEqual(
            encode_standby_time_range(False, ABSENCE_START, ABSENCE_END).hex(),
            "080010a486ccd80618a4a9d1d806",
        )
        self.assertEqual(encode_active_week_plan(1).hex(), "0801")
        self.assertEqual(encode_plan_id(3).hex(), "0803")

    def test_run_state_reports_the_absence_planner(self):
        state = decode_run_state(absence_state(planned=True))
        self.assertTrue(state.absence_planned)
        self.assertFalse(state.standby)
        self.assertEqual(
            (state.standby_start, state.standby_end), (ABSENCE_START, ABSENCE_END)
        )
        off = decode_run_state(absence_state(planned=False))
        self.assertFalse(off.absence_planned)
        self.assertIsNone(off.standby_start)

    def test_confirmations_return_the_assigned_ids(self):
        day = day_plan_message(4, "HA Test", ((0, 1), (420, 0)))
        self.assertEqual(decode_day_plan_confirm(day).plan_id, 4)
        week = encode_bytes(
            1,
            encode_uint(1, 2)
            + encode_string(2, "HA Test")
            + b"".join(encode_uint(number, 4) for number in range(3, 10)),
        )
        self.assertEqual(decode_week_plan_confirm(week).days, (4,) * 7)

    def test_history_and_internet_access(self):
        def entry(nickname, timestamp, message, *params):
            return encode_bytes(
                1,
                encode_string(1, nickname)
                + encode_uint(2, timestamp)
                + encode_string(3, message)
                + b"".join(encode_string(4, item) for item in params)
                + encode_bytes(4, b""),
            )

        log = decode_action_log(
            entry("ComfoRF DUTY CYCLE", 1795202340, "53 24h:53 MAX:55")
            + entry("Home Assistant", 1795202340, "REMOVED_DAYPLAN", "HA Test")
            + entry("Mac", 1795201140, "CHANGED_DAYPLAN", "Test")
        )
        self.assertEqual(len(log), 3)
        self.assertEqual(log[1].message, "REMOVED_DAYPLAN")
        self.assertEqual(log[1].params, ("HA Test",))
        self.assertEqual(log[0].params, ())
        self.assertTrue(decode_remote_access(encode_uint(1, 0)))
        self.assertFalse(decode_remote_access(encode_uint(1, 1)))
        self.assertIsNone(decode_remote_access(b""))


class FakeScheduleGateway:
    """Answer schedule, run-state and absence requests from mutable state."""

    def __init__(self):
        self.week_plan = 3
        self.planned = False
        self.start = None
        self.end = None
        self.day_plans = {
            1: DayPlan(1, "Wintertagesplan", ((0, 1), (480, 0), (1260, 1))),
            2: DayPlan(2, "Sommertagesplan", ((0, 1), (480, 2), (1350, 0))),
        }
        self.week_plans = {
            1: WeekPlan(1, "Sommerwochenplan", (2,) * 7),
            3: WeekPlan(3, "Winterwochenplan", (1,) * 7),
        }
        self.locked: set[tuple[bool, int]] = set()
        self.foreign_lock: set[tuple[bool, int]] = set()
        self.requests = []
        self.drop_functions = False

    def schedules(self):
        return b"".join(
            [encode_day_plan(plan) for plan in self.day_plans.values()]
            + [
                encode_bytes(2, bytes_value(decode_fields(encode_week_plan(plan)), 1))
                for plan in self.week_plans.values()
            ]
        )

    def request(self, request, confirm, body=b"", **_kwargs):
        self.requests.append(OperationType(request))
        op = OperationType
        if request == op.RUN_STATE_REQUEST:
            message = absence_state(
                planned=self.planned,
                start=self.start or 0,
                end=self.end or 0,
                week_plan=self.week_plan,
            )
            return SimpleNamespace(body=message)
        if request == op.SCHEDULES_REQUEST:
            return SimpleNamespace(body=self.schedules())
        if request == op.SET_ACTIVE_WEEK_PLAN_REQUEST:
            self.week_plan = uint_value(decode_fields(body), 1)
            return SimpleNamespace(body=body)
        if request == op.SET_STANDBY_TIME_RANGE_REQUEST:
            fields = decode_fields(body)
            self.planned = bool(uint_value(fields, 1))
            self.start, self.end = uint_value(fields, 2), uint_value(fields, 3)
            return SimpleNamespace(body=b"")
        if request in (
            op.LOCK_DAY_PLAN_REQUEST,
            op.LOCK_WEEK_PLAN_REQUEST,
            op.RELEASE_DAY_PLAN_REQUEST,
            op.RELEASE_WEEK_PLAN_REQUEST,
        ):
            key = (
                request in (op.LOCK_WEEK_PLAN_REQUEST, op.RELEASE_WEEK_PLAN_REQUEST),
                uint_value(decode_fields(body), 1),
            )
            if key in self.foreign_lock:
                raise GatewayResponseError(9, None)
            if request in (op.LOCK_DAY_PLAN_REQUEST, op.LOCK_WEEK_PLAN_REQUEST):
                self.locked.add(key)
            else:
                self.locked.discard(key)
            return SimpleNamespace(body=encode_uint(1, key[1]) + encode_uint(2, 1))
        if request in (op.ADD_DAY_PLAN_REQUEST, op.SET_DAY_PLAN_REQUEST):
            plan = decode_schedules(body).day_plans[0]
            if request == op.ADD_DAY_PLAN_REQUEST:
                plan = replace(plan, plan_id=max(self.day_plans) + 1)
            elif (False, plan.plan_id) not in self.locked:
                raise GatewayResponseError(9, None)
            if self.drop_functions:
                plan = replace(plan, functions=())
            self.day_plans[plan.plan_id] = plan
            return SimpleNamespace(body=encode_day_plan(replace(plan, functions=())))
        if request in (op.ADD_WEEK_PLAN_REQUEST, op.SET_WEEK_PLAN_REQUEST):
            plan = decode_week_plan_confirm(body)
            if request == op.ADD_WEEK_PLAN_REQUEST:
                plan = replace(plan, plan_id=max(self.week_plans) + 1)
            self.week_plans[plan.plan_id] = plan
            return SimpleNamespace(body=encode_week_plan(plan))
        if request == op.REMOVE_DAY_PLAN_REQUEST:
            plan_id = uint_value(decode_fields(body), 1)
            if any(plan_id in plan.days for plan in self.week_plans.values()):
                raise GatewayResponseError(12, None)
            del self.day_plans[plan_id]
            return SimpleNamespace(body=body)
        if request == op.REMOVE_WEEK_PLAN_REQUEST:
            del self.week_plans[uint_value(decode_fields(body), 1)]
            return SimpleNamespace(body=body)
        if request == op.ROOMS_REQUEST:
            return SimpleNamespace(body=rooms_message())
        raise AssertionError(f"unexpected request {request}")


def make_client(gateway):
    client = ConnectBoxClient("test.invalid", GATEWAY, APP, "a" * 64)
    client.close = Mock()
    session = SimpleNamespace(request=gateway.request)
    client._session = session
    client._connected_session = lambda: session
    client._version = VersionInfo(None, None, None, None, None)
    return client


class ClientTests(unittest.TestCase):
    def setUp(self):
        patcher = patch.object(client_mod, "LEVEL_SETTLE_POLL_INTERVAL", 0)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.gateway = FakeScheduleGateway()
        self.client = make_client(self.gateway)

    def test_active_week_plan_is_switched_and_confirmed(self):
        self.client._reload_schedules()
        result = self.client.set_active_week_plan(1)
        self.assertEqual(result.run_state.week_plan, 1)
        with self.assertRaises(CommandValidationError):
            self.client.set_active_week_plan(7)

    def test_absence_is_saved_and_switched_off(self):
        start = int(client_mod.time.time()) + 3600
        result = self.client.set_absence(True, start, start + 86400)
        self.assertTrue(result.run_state.absence_planned)
        self.assertEqual(result.run_state.standby_end, start + 86400)
        off = self.client.set_absence(False)
        self.assertFalse(off.run_state.absence_planned)
        # The reported period is sent back with mode 0, as tested.
        self.assertEqual(self.gateway.start, start)

    def test_absence_needs_a_valid_future_period(self):
        now = int(client_mod.time.time())
        for start, end, key in (
            (None, None, "absence_period_missing"),
            (now + 7200, now + 3600, "absence_period_invalid"),
            (now - 7200, now - 3600, "absence_period_past"),
        ):
            with (
                self.subTest(key=key),
                self.assertRaises(CommandValidationError) as err,
            ):
                self.client.set_absence(True, start, end)
            self.assertEqual(err.exception.translation_key, key)

    def test_day_plan_is_changed_under_its_lock_and_checked(self):
        plan = DayPlan(
            1,
            "Wintertagesplan",
            ((480, 0), (0, 1), (1290, 1)),
            (ScheduledFunction(420, 2, 20, 3),),
        )
        _snapshot, plan_id = self.client.save_day_plan(plan)
        self.assertEqual(plan_id, 1)
        stored = self.gateway.day_plans[1]
        self.assertEqual(stored.slots, ((0, 1), (480, 0), (1290, 1)))
        self.assertEqual(stored.functions[0].room_id, 3)
        op = OperationType
        self.assertEqual(
            self.gateway.requests[:3],
            [
                op.LOCK_DAY_PLAN_REQUEST,
                op.SET_DAY_PLAN_REQUEST,
                op.RELEASE_DAY_PLAN_REQUEST,
            ],
        )
        self.assertEqual(self.gateway.locked, set())

    def test_new_day_plan_gets_the_assigned_id(self):
        _snapshot, plan_id = self.client.save_day_plan(
            DayPlan(0, "Homeoffice", ((0, 1), (420, 0)))
        )
        self.assertEqual(plan_id, 3)
        self.assertEqual(self.gateway.day_plans[3].name, "Homeoffice")
        self.assertNotIn(OperationType.LOCK_DAY_PLAN_REQUEST, self.gateway.requests)

    def test_dropped_function_is_reported(self):
        self.gateway.drop_functions = True
        with self.assertRaises(ProtocolError):
            self.client.save_day_plan(
                DayPlan(
                    1, "Wintertagesplan", ((0, 1),), (ScheduledFunction(420, 2, 20, 1),)
                )
            )

    def test_plan_locked_by_another_app_is_refused(self):
        self.gateway.foreign_lock.add((False, 1))
        with self.assertRaises(CommandValidationError) as err:
            self.client.save_day_plan(DayPlan(1, "Wintertagesplan", ((0, 1),)))
        self.assertEqual(err.exception.translation_key, "plan_locked")

    def test_week_plan_add_change_and_remove(self):
        _snapshot, plan_id = self.client.save_week_plan(
            WeekPlan(0, "HA Test", (2,) * 7)
        )
        self.assertEqual(plan_id, 4)
        self.client.save_week_plan(WeekPlan(4, "HA Test 2", (1,) + (2,) * 6))
        self.assertEqual(self.gateway.week_plans[4].days[0], 1)
        result = self.client.remove_plan(4, week=True)
        self.assertIsNone(result.schedules.week_plan(4))
        self.assertEqual(self.gateway.locked, set())

    def test_day_plan_in_use_is_refused_by_the_gateway(self):
        with self.assertRaises(GatewayResponseError):
            self.client.remove_plan(1, week=False)
        self.assertEqual(self.gateway.locked, set())


def schedule_snapshot(**changes):
    data = replace(
        snapshot(rooms_message()),
        run_state=decode_run_state(absence_state(planned=False)),
        schedules=decode_schedules(schedules_message()),
    )
    return replace(data, **changes)


class EntityTests(unittest.TestCase):
    def test_week_plan_select(self):
        coordinator = FakeCoordinator(schedule_snapshot())
        calls = []

        async def activate(plan_id):
            calls.append(plan_id)

        coordinator.async_set_active_week_plan = activate
        entity = next(
            item
            for item in set_up(select, coordinator)
            if isinstance(item, select.ConnectBoxActiveWeekPlan)
        )
        self.assertEqual(entity.options, ["Sommerwochenplan", "Winterwochenplan"])
        self.assertEqual(entity.current_option, "Winterwochenplan")
        asyncio.run(entity.async_select_option("Sommerwochenplan"))
        self.assertEqual(calls, [1])

    def test_duplicate_week_plan_names_get_their_id(self):
        schedules = decode_schedules(schedules_message())
        schedules = replace(
            schedules,
            week_plans=(
                *schedules.week_plans,
                replace(schedules.week_plans[0], plan_id=5),
            ),
        )
        self.assertEqual(
            list(select.week_plan_options(schedules)),
            ["Sommerwochenplan (1)", "Winterwochenplan", "Sommerwochenplan (5)"],
        )

    def test_absence_switch_and_indicators(self):
        data = schedule_snapshot(
            run_state=decode_run_state(absence_state(planned=True)),
            remote_access=True,
        )
        coordinator = FakeCoordinator(data)
        coordinator.absence_period = lambda: (ABSENCE_START, ABSENCE_END)
        absence = next(
            item
            for item in set_up(switch, coordinator)
            if isinstance(item, switch.ConnectBoxAbsencePlannerSwitch)
        )
        self.assertTrue(absence.is_on)
        self.assertEqual(
            absence.extra_state_attributes["starts_at"], "2026-12-04T18:15:00+00:00"
        )
        indicators = {
            item.entity_description.key: item
            for item in set_up(binary_sensor, coordinator)
            if isinstance(item, binary_sensor.ConnectBoxGatewayBinarySensor)
        }
        self.assertFalse(indicators["absence_active"].is_on)
        self.assertTrue(indicators["remote_access"].is_on)

    def test_history_and_duty_cycle_sensors(self):
        log = (
            ActionLogEntry("ComfoRF DUTY CYCLE", 1795202400, "53 24h:52 MAX:55"),
            ActionLogEntry("Home Assistant", 1795202340, "CHANGED_DAYPLAN", ("Test",)),
            ActionLogEntry("Mac", 1795201140, "ADDED_DAYPLAN", ("Test",)),
        )
        coordinator = FakeCoordinator(schedule_snapshot(action_log=log))
        entities = set_up(sensor, coordinator)
        history = next(
            item
            for item in entities
            if isinstance(item, sensor.ConnectBoxHistorySensor)
        )
        duty = next(
            item
            for item in entities
            if isinstance(item, sensor.ConnectBoxDutyCycleSensor)
        )
        self.assertEqual(history.native_value, "CHANGED_DAYPLAN: Test")
        attributes = history.extra_state_attributes
        self.assertEqual(attributes["by"], "Home Assistant")
        self.assertEqual(len(attributes["entries"]), 2)
        self.assertEqual(duty.native_value, 53)
        self.assertEqual(duty.extra_state_attributes["last_24_hours"], 52)


class ServiceHelperTests(unittest.TestCase):
    def test_plans_are_found_by_name_or_id(self):
        plans = [(1, "Wintertagesplan"), (2, "Sommertagesplan"), (3, "Test")]
        self.assertEqual(services._resolve("sommertagesplan", plans), 2)
        self.assertEqual(services._resolve(3, plans), 3)
        self.assertEqual(services._resolve("1", plans), 1)
        with self.assertRaises(ServiceValidationError):
            services._resolve("Homeoffice", plans)
        with self.assertRaises(ServiceValidationError):
            services._resolve("Test", [*plans, (4, "test")])

    def test_boost_needs_a_room_with_the_boost(self):
        data = snapshot(rooms_message())
        self.assertEqual(services._function_room(data, "arbeitszimmer"), 3)
        with self.assertRaises(ServiceValidationError):
            services._function_room(data, "Küche")
        no_boost = snapshot(rooms_message(boost_duration=None))
        with self.assertRaises(ServiceValidationError):
            services._function_room(no_boost, 3)

    def test_schemas(self):
        data = services.SAVE_DAY_PLAN_SCHEMA(
            {
                "name": "Homeoffice",
                "slots": [
                    {"start": "00:00", "situation": "asleep"},
                    {"start": "07:30:00", "situation": "awake"},
                ],
                "functions": [],
            }
        )
        self.assertEqual(data["slots"][1]["start"].minute, 30)
        self.assertEqual(data["functions"], [])
        import voluptuous as vol

        with self.assertRaises(vol.Invalid):
            services.SAVE_DAY_PLAN_SCHEMA(
                {"slots": [{"start": "07:30:10", "situation": "awake"}]}
            )
        with self.assertRaises(vol.Invalid):
            services.SAVE_DAY_PLAN_SCHEMA({"name": "x" * 33})


if __name__ == "__main__":
    unittest.main()
