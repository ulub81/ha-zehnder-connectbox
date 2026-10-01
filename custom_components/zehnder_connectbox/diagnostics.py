"""Privacy-preserving diagnostics for Zehnder ConnectBox."""

from __future__ import annotations

import time
from collections import Counter
from typing import Any
from uuid import UUID

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from . import ZehnderConnectBoxConfigEntry
from .client import capture_value
from .const import CONF_APP_UUID, CONF_GATEWAY_UUID, CONF_REMOTE_UUID
from .models import Room, RunMode, TemperatureMode
from .profiles import (
    CO2_SENSOR_STATUS,
    EXHAUST_FAN_SPEED,
    EXTRACT_AIR_SENSOR_STATUS,
    EXTRACT_AIR_TEMPERATURE,
    HUMIDITY_SENSOR_STATUS,
    INCOMING_AIR_SENSOR_STATUS,
    INCOMING_AIR_TEMPERATURE,
    KNOWN_SENSOR_TYPES,
    SENSOR_TYPE_CO2,
    SENSOR_TYPE_HUMIDITY,
    SUPPLY_FAN_SPEED,
    board_reading,
    format_version,
    has_filter_warning,
    has_sensor_board,
    is_supported,
    product_name,
    sensor_available,
    supply_only_operation,
    supports_sensor_mode,
    temperature_value,
)
from .protobuf import (
    Field,
    ProtobufDecodeError,
    WireType,
    bytes_value,
    bytes_values,
    decode_fields,
    uint_value,
)


def _temperature_mode_name(mode: int) -> str:
    """Return a readable name for a room ventilation temperature mode."""
    try:
        return TemperatureMode(mode).name.lower()
    except ValueError:
        return str(mode)


def _is_text(value: bytes) -> bool:
    """Return whether a byte field looks like text, such as a name."""
    try:
        return value.decode().isprintable()
    except UnicodeDecodeError:
        return False


def _nested_message(value: bytes, *, numbers_only: bool) -> tuple[Field, ...] | None:
    """Return nested fields when a byte field is a message and not text.

    Room and device data may hold identities, so only messages of plain
    numbers are expanded there. Run-state messages are expanded completely.
    """
    if not value or _is_text(value):
        return None
    try:
        fields = decode_fields(value)
    except ProtobufDecodeError:
        return None
    if not fields:
        return None
    if numbers_only and not all(item.wire_type is WireType.VARINT for item in fields):
        return None
    return fields


def _identity_labels(rooms: tuple[Room, ...], entry_data: Any) -> dict[bytes, str]:
    """Temporary: byte values that identify the home, named instead of shown."""
    labels: dict[bytes, str] = {}
    for key in (CONF_GATEWAY_UUID, CONF_APP_UUID, CONF_REMOTE_UUID):
        try:
            labels[UUID(str(entry_data[key])).bytes] = key
        except (KeyError, TypeError, ValueError):
            continue
    for room in rooms:
        if not room.raw:
            continue
        try:
            room_fields = decode_fields(room.raw)
        except ProtobufDecodeError:
            continue
        name = bytes_value(room_fields, 2)
        if name:
            labels[name] = f"room[{room.room_id}].f2"
        for device_raw in bytes_values(room_fields, 8):
            try:
                device_fields = decode_fields(device_raw)
            except ProtobufDecodeError:
                continue
            device_id = uint_value(device_fields, 1)
            for number in (2, 3):
                value = bytes_value(device_fields, number)
                if value:
                    labels[value] = (
                        f"room[{room.room_id}].device[{device_id}].f{number}"
                    )
    return labels


def _probe(value: bytes, identities: dict[bytes, str], depth: int = 0) -> Any:
    """Temporary: show the structure of an unknown room field.

    Identities are named instead of shown and text shows only its length.
    Nested messages are opened up to three levels; other values of up to
    eight bytes are shown as hex.
    """
    label = identities.get(value)
    if label is not None:
        return f"same as {label}"
    if _is_text(value):
        return f"text(len={len(value)})"
    if depth < 3:
        try:
            fields = decode_fields(value)
        except ProtobufDecodeError:
            fields = ()
        if fields:
            out: dict[str, Any] = {}
            seen: Counter[int] = Counter()
            for item in fields:
                index = seen[item.number]
                seen[item.number] += 1
                key = f"f{item.number}" + (f"[{index}]" if index else "")
                if item.wire_type is WireType.VARINT:
                    out[key] = int(item.value)
                elif item.wire_type is WireType.BYTES:
                    out[key] = _probe(bytes(item.value), identities, depth + 1)
                else:
                    raw = bytes(item.value)
                    out[key] = {
                        "hex": raw.hex(),
                        "uint_le": int.from_bytes(raw, "little"),
                    }
            return out
    if len(value) <= 8:
        return {"hex": value.hex()}
    return f"bytes(len={len(value)})"


def _flatten(
    fields: tuple[Field, ...],
    prefix: str,
    out: dict[str, Any],
    context: str,
    identities: dict[bytes, str] | None = None,
) -> None:
    """Temporary: flatten gateway fields without names, text, or other bytes.

    Unknown byte fields of a room are shown by structure (see _probe).
    """
    seen: Counter[int] = Counter()
    for item in fields:
        index = seen[item.number]
        seen[item.number] += 1
        key = f"{prefix}.f{item.number}" + (f"[{index}]" if index else "")
        if item.wire_type is WireType.VARINT:
            out[key] = int(item.value)
            continue
        if item.wire_type is not WireType.BYTES:
            out[key] = f"fixed(len={len(item.value)})"
            continue
        value = bytes(item.value)
        if context in ("room", "device") and item.number == 2:
            continue  # room name or device serial number
        if context == "room" and item.number == 8:
            nested = decode_fields(value)
            device_prefix = f"{prefix}.device[{uint_value(nested, 1)}]"
            _flatten(nested, device_prefix, out, "device", identities)
            continue
        if context == "device" and item.number == 101:
            nested = decode_fields(value)
            key_fields = decode_fields(bytes_value(nested, 1) or b"")
            identity = ".".join(
                str(uint_value(key_fields, number)) for number in (4, 5, 6)
            )
            raw_value = bytes_value(nested, 2)
            out[f"{prefix}.prop[{identity}]"] = (
                capture_value(raw_value) if raw_value else None
            )
            continue
        run_state = context.startswith("run_state")
        nested = _nested_message(value, numbers_only=not run_state)
        if nested is None and context == "room":
            out[key] = _probe(value, identities or {})
        elif nested is None:
            out[key] = f"bytes(len={len(value)})"
        elif context == "room" and item.number == 19:
            mode = _temperature_mode_name(uint_value(nested, 1) or 0)
            _flatten(nested, f"{prefix}.vent[{mode}]", out, "nested", identities)
        else:
            _flatten(
                nested,
                key,
                out,
                "run_state_nested" if run_state else "nested",
                identities,
            )


def _raw_state(
    run_state_raw: bytes,
    rooms: tuple[Room, ...],
    identities: dict[bytes, str] | None = None,
) -> dict[str, Any]:
    """Temporary: all numeric run-state, room, and device fields."""
    out: dict[str, Any] = {}
    if run_state_raw:
        _flatten(decode_fields(run_state_raw), "run_state", out, "run_state")
    for room in rooms:
        if room.raw:
            _flatten(
                decode_fields(room.raw),
                f"room[{room.room_id}]",
                out,
                "room",
                identities,
            )
    return out


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ZehnderConnectBoxConfigEntry
) -> dict[str, Any]:
    """Return only sanitized operational facts; never return local identities."""
    coordinator = entry.runtime_data
    snapshot = coordinator.data
    if snapshot is None:
        return {"available": False}

    try:
        run_mode = RunMode(snapshot.run_state.run_mode).name.lower()
    except ValueError:
        run_mode = "unknown"
    devices = [device for room in snapshot.rooms for device in room.devices]
    result: dict[str, Any] = {
        "available": coordinator.last_update_success,
        "config_entry_version": entry.version,
        "gateway": {
            "system_version": snapshot.version.system_version,
            "comfonet_version": snapshot.version.comfonet_version,
            "property_list_version": snapshot.version.property_list_version,
            "zone_state_list_version": snapshot.version.zone_state_list_version,
            "connectbox_version": snapshot.version.connectbox_version,
            "run_mode": run_mode,
            "temperature_mode": snapshot.run_state.temperature_mode,
            "standby": snapshot.run_state.standby,
            "summer_ventilation_active": snapshot.run_state.summer_ventilation,
            "summer_ventilation_enabled": (
                snapshot.summer_ventilation_settings.enabled
                if snapshot.summer_ventilation_settings is not None
                else None
            ),
            "summer_ventilation_duration_hours": (
                snapshot.summer_ventilation_settings.duration_hours
                if snapshot.summer_ventilation_settings is not None
                else None
            ),
            "room_count": len(snapshot.rooms),
            "attached_device_count": len(devices),
        },
        "attached_devices": [
            {
                "model": product_name(device),
                "product_type": device.product_type,
                "product_variant": device.product_variant,
                "supported": is_supported(device),
                "summer_ventilation_available": device.summer_ventilation_available,
                "filter_warning": has_filter_warning(device),
                "filter_warning_reported": device.filter_warning,
                "fault_present": any(value != 0 for value in device.errors),
                "hardware_version": device.hardware_version,
                "software_version": format_version(device.software_version),
                "signal_strength": device.signal_strength,
                "temperature_telemetry_available": any(
                    value.key.class_id == 25 and value.value is not None
                    for value in device.properties
                ),
                "fan_telemetry_available": any(
                    value.key.class_id == 38
                    and value.key.property_id == 3
                    and value.value is not None
                    for value in device.properties
                ),
                "ventilation_level": room.level_for_mode(
                    snapshot.run_state.temperature_mode
                ),
                "current_level": room.current_level(
                    snapshot.run_state.temperature_mode
                ),
                "temporary_change_active": room.temporary_until is not None,
                "boost_active": room.boost_active(time.time()),
                "boost_duration": room.boost_duration,
                "ventilation_values": {
                    _temperature_mode_name(value.temperature_mode): value.level
                    for value in room.ventilation
                },
                "sensor_board_reported": has_sensor_board(device),
                "sensor_mode_supported": supports_sensor_mode(room, device),
                "humidity": board_reading(device, SENSOR_TYPE_HUMIDITY),
                "co2": board_reading(device, SENSOR_TYPE_CO2),
                # Readings of not yet mapped sensor types, such as a VOC board.
                "other_sensor_readings": {
                    str(sensor_type): value
                    for sensor_type, value in device.sensor_readings
                    if sensor_type not in KNOWN_SENSOR_TYPES
                },
                "extract_air_temperature": temperature_value(
                    device, EXTRACT_AIR_TEMPERATURE, EXTRACT_AIR_SENSOR_STATUS
                ),
                "incoming_air_temperature": temperature_value(
                    device, INCOMING_AIR_TEMPERATURE, INCOMING_AIR_SENSOR_STATUS
                ),
                "extract_air_sensor_available": sensor_available(
                    device, EXTRACT_AIR_SENSOR_STATUS
                ),
                "incoming_air_sensor_available": sensor_available(
                    device, INCOMING_AIR_SENSOR_STATUS
                ),
                "humidity_sensor_available": sensor_available(
                    device, HUMIDITY_SENSOR_STATUS
                ),
                "co2_sensor_available": sensor_available(device, CO2_SENSOR_STATUS),
                "exhaust_fan_speed": EXHAUST_FAN_SPEED.value(device),
                "supply_fan_speed": SUPPLY_FAN_SPEED.value(device),
                "supply_only_operation": supply_only_operation(device),
            }
            for room in snapshot.rooms
            for device in room.devices
        ],
    }

    # Temporary capture build: numeric run-state, room, and device fields and
    # the app's ComfoSpot 50 properties, read fresh at download time, to
    # compare states before and after a change in the app or on a unit.
    # Only read requests are sent.
    capture: dict[str, Any] = {
        "captured_at": dt_util.utcnow().isoformat(timespec="seconds")
    }
    run_state_raw, rooms = snapshot.run_state.raw, snapshot.rooms
    try:
        run_state, rooms, properties = await coordinator.async_capture()
    except Exception as err:  # noqa: BLE001 - diagnostics must still be returned
        capture["source"] = "last poll"
        capture["error"] = type(err).__name__
    else:
        run_state_raw = run_state.raw
        capture["source"] = "fresh read"
        capture["app_properties"] = {
            str(device_id): values for device_id, values in properties.items()
        }
    try:
        capture["fields"] = _raw_state(
            run_state_raw, rooms, _identity_labels(rooms, getattr(entry, "data", {}))
        )
    except ProtobufDecodeError:
        capture["fields"] = {"status": "failed", "error": "ProtobufDecodeError"}
    result["capture"] = capture
    return result
