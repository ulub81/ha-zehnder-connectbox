"""Offline checks for the combined fan status, translated presets and fault codes."""

from __future__ import annotations

import asyncio
import json
import sys
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from custom_components.zehnder_connectbox import binary_sensor, fan, sensor
from custom_components.zehnder_connectbox.models import (
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
from custom_components.zehnder_connectbox.status import (
    FAN_STATUS_OPTIONS,
    error_codes,
    fan_status,
    situation,
)

COMPONENT = REPO / "custom_components" / "zehnder_connectbox"
NOW = time.time()


def prop(identity, value: bytes) -> PropertyValue:
    return PropertyValue(PropertyKey(30, 255, 0, *identity), value)


def make_device(*, errors=(), props=()) -> AttachedDevice:
    return AttachedDevice(
        device_id=1,
        product_type=30,
        product_variant=1,
        hardware_version=None,
        software_version=None,
        battery_state=None,
        signal_strength=67,
        errors=tuple(errors),
        level_zero_supported=True,
        filter_warning=False,
        filter_runtime=None,
        filter_maximum=None,
        properties=(
            prop((38, 0, 3), (1020).to_bytes(2, "little")),
            prop((38, 1, 3), (860).to_bytes(2, "little")),
            *props,
        ),
    )


def make_room(level, *, boost_until=None, device=None) -> Room:
    return Room(
        room_id=7,
        name="Büro",
        room_type=None,
        target_level=level,
        ventilation=(VentilationValue(TemperatureMode.AWAKE, 2),),
        devices=(device or make_device(),),
        boost_until=boost_until,
    )


def run_state(mode=RunMode.AUTOMATIC, *, summer=None, summer_end=None, tmode=0):
    return RunState(
        run_mode=mode,
        temperature_mode=tmode,
        standby=None,
        standby_mode=None,
        summer_ventilation=summer,
        summer_ventilation_end=summer_end,
        errors=(),
    )


# --- pure status logic ---------------------------------------------------------


@pytest.mark.parametrize(
    ("level", "expected"),
    [
        (0, "standby"),
        (1, "level_1"),
        (2, "level_2"),
        (3, "level_3"),
        (4, "level_4"),
        (5, "auto"),
        (9, None),
    ],
)
def test_status_follows_room_level(level, expected):
    room = make_room(level)
    assert fan_status(run_state(), room, room.devices[0], NOW) == expected


def test_global_standby_wins():
    room = make_room(3, boost_until=int(NOW) + 600)
    assert fan_status(run_state(RunMode.OFF), room, room.devices[0], NOW) == "off"


def test_running_boost_wins_over_level():
    room = make_room(2, boost_until=int(NOW) + 600)
    assert fan_status(run_state(), room, room.devices[0], NOW) == "boost"


def test_expired_boost_shows_level():
    room = make_room(2, boost_until=int(NOW) - 5)
    assert fan_status(run_state(), room, room.devices[0], NOW) == "level_2"


def test_summer_ventilation_needs_a_role():
    state = run_state(summer=True, summer_end=int(NOW) + 3600)
    # Without a reported role the unit does not take part.
    room = make_room(2)
    assert fan_status(state, room, room.devices[0], NOW) == "level_2"


def test_status_options_are_translated_and_have_icons():
    for name in ("strings.json", "translations/de.json", "translations/en.json"):
        data = json.loads((COMPONENT / name).read_text())
        states = data["entity"]["sensor"]["fan_status"]["state"]
        assert set(states) == set(FAN_STATUS_OPTIONS)
        presets = data["entity"]["fan"]["ventilation"]["state_attributes"][
            "preset_mode"
        ]["state"]
        assert set(presets) == {*fan.PRESET_LEVELS, fan.PRESET_AUTO}
    icons = json.loads((COMPONENT / "icons.json").read_text())
    assert set(icons["entity"]["sensor"]["fan_status"]["state"]) == set(
        FAN_STATUS_OPTIONS
    )
    de = json.loads((COMPONENT / "translations/de.json").read_text())
    assert (
        de["entity"]["fan"]["ventilation"]["state_attributes"]["preset_mode"]["state"][
            "level_1"
        ]
        == "Stufe 1"
    )


def test_situation_names():
    assert situation(run_state(tmode=TemperatureMode.ASLEEP)) == "asleep"
    assert situation(run_state(tmode=42)) is None


def test_error_codes_merge_without_duplicates():
    device = make_device(
        errors=(0, 12), props=(prop((37, 0, 1), b"\x0c"), prop((37, 1, 1), b"\x03"))
    )
    assert error_codes(device) == [12, 3]
    assert error_codes(make_device()) == []


# --- entities ------------------------------------------------------------------


class FakeCoordinator:
    def __init__(self, data):
        self.data = data
        self.entry = SimpleNamespace(data={"gateway_uuid": "gw"})
        self.last_update_success = True

    def async_add_listener(self, callback):
        return lambda: None


def snapshot(room, state=None):
    return GatewaySnapshot(
        version=VersionInfo(None, None, None, None, None),
        run_state=state or run_state(),
        rooms=(room,),
    )


def setup(platform, data):
    added = []
    entry = SimpleNamespace(
        runtime_data=FakeCoordinator(data), async_on_unload=lambda unsub: None
    )
    asyncio.run(platform.async_setup_entry(None, entry, added.extend))
    return added


def test_status_sensor_entity():
    entities = setup(sensor, snapshot(make_room(5)))
    (status,) = [e for e in entities if isinstance(e, sensor.ConnectBoxFanStatusSensor)]
    assert status.unique_id == "gw_1_fan_status"
    assert status.translation_key == "fan_status"
    assert status.native_value == "auto"
    assert status.options == FAN_STATUS_OPTIONS
    assert status.available is True
    attrs = status.extra_state_attributes
    assert attrs["level"] is None
    assert attrs["situation"] == "awake"
    assert attrs["supply_fan_speed"] == 860
    assert attrs["exhaust_fan_speed"] == 1020
    assert attrs["fault"] is False
    assert attrs["error_codes"] == []
    assert attrs["boost_until"] is None


def test_status_sensor_is_added_once():
    data = snapshot(make_room(2))
    added = []
    listeners = []
    coordinator = FakeCoordinator(data)
    coordinator.async_add_listener = lambda cb: listeners.append(cb) or (lambda: None)
    entry = SimpleNamespace(runtime_data=coordinator, async_on_unload=lambda u: None)
    asyncio.run(sensor.async_setup_entry(None, entry, added.extend))
    listeners[0]()
    assert sum(isinstance(e, sensor.ConnectBoxFanStatusSensor) for e in added) == 1


def test_fault_sensor_lists_codes():
    device = make_device(errors=(4,))
    entities = setup(binary_sensor, snapshot(make_room(2, device=device)))
    (fault,) = [e for e in entities if e.entity_description.key == "fault"]
    assert fault.is_on is True
    assert fault.extra_state_attributes == {"error_codes": [4]}
    (filt,) = [e for e in entities if e.entity_description.key == "filter_warning"]
    assert filt.extra_state_attributes is None


def test_fan_uses_translated_preset_names():
    (entity,) = setup(fan, snapshot(make_room(3)))
    assert entity.preset_mode == "level_3"
    assert entity.percentage == 75
    room = replace(make_room(5))
    entity.coordinator.data = snapshot(room)
    assert entity.preset_mode == "auto"
