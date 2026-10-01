"""Privacy-preserving diagnostics for Zehnder ConnectBox."""

from __future__ import annotations

from collections import Counter
from typing import Any

from homeassistant.core import HomeAssistant

from . import ZehnderConnectBoxConfigEntry
from .models import GatewaySnapshot, RunMode, TemperatureMode
from .profiles import (
    CO2_SENSOR_STATUS,
    EXHAUST_FAN_SPEED,
    EXTRACT_AIR_SENSOR_STATUS,
    EXTRACT_AIR_TEMPERATURE,
    HUMIDITY_SENSOR_STATUS,
    INCOMING_AIR_SENSOR_STATUS,
    INCOMING_AIR_TEMPERATURE,
    SUPPLY_FAN_SPEED,
    format_version,
    has_filter_warning,
    has_sensor_board,
    is_supported,
    product_name,
    sensor_available,
    supports_sensor_mode,
    temperature_value,
)
from .protobuf import (
    Field,
    ProtobufDecodeError,
    WireType,
    bytes_value,
    decode_fields,
    uint_value,
)


def _temperature_mode_name(mode: int) -> str:
    """Return a readable name for a room ventilation temperature mode."""
    try:
        return TemperatureMode(mode).name.lower()
    except ValueError:
        return str(mode)


def _varint_message(value: bytes) -> tuple[Field, ...] | None:
    """Return nested fields when a byte field is a message of plain numbers."""
    try:
        fields = decode_fields(value)
    except ProtobufDecodeError:
        return None
    if fields and all(item.wire_type is WireType.VARINT for item in fields):
        return fields
    return None


def _flatten(
    fields: tuple[Field, ...], prefix: str, out: dict[str, Any], context: str
) -> None:
    """Flatten gateway fields without names, strings, or other byte contents."""
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
        if context == "room" and item.number == 2:
            continue  # room name
        if context == "room" and item.number == 8:
            nested = decode_fields(value)
            device_prefix = f"{prefix}.device[{uint_value(nested, 1)}]"
            _flatten(nested, device_prefix, out, "device")
            continue
        if context == "room" and item.number == 19:
            nested = decode_fields(value)
            mode = _temperature_mode_name(uint_value(nested, 1) or 0)
            _flatten(nested, f"{prefix}.vent[{mode}]", out, "vent")
            continue
        if context == "device" and item.number == 101:
            nested = decode_fields(value)
            raw_key = bytes_value(nested, 1)
            raw_value = bytes_value(nested, 2)
            if raw_key is not None:
                key_fields = decode_fields(raw_key)
                identity = ".".join(
                    str(uint_value(key_fields, number)) for number in (4, 5, 6)
                )
                out[f"{prefix}.prop[{identity}]"] = (
                    raw_value.hex() if raw_value is not None else None
                )
            continue
        nested = _varint_message(value)
        if nested is not None:
            _flatten(nested, key, out, "nested")
        else:
            out[key] = f"bytes(len={len(value)})"


def _raw_rooms(snapshot: GatewaySnapshot) -> dict[str, Any]:
    """Temporary: all numeric room and device fields for comparing two states."""
    out: dict[str, Any] = {}
    for room in snapshot.rooms:
        if room.raw:
            _flatten(decode_fields(room.raw), f"room[{room.room_id}]", out, "room")
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
            "standby_mode": snapshot.run_state.standby_mode,
            "summer_ventilation": snapshot.run_state.summer_ventilation,
            "run_state_errors": list(snapshot.run_state.errors),
            "room_count": len(snapshot.rooms),
            "attached_device_count": len(devices),
        },
        "attached_devices": [
            {
                "model": product_name(device),
                "product_type": device.product_type,
                "product_variant": device.product_variant,
                "supported": is_supported(device),
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
                "ventilation_values": {
                    _temperature_mode_name(value.temperature_mode): value.level
                    for value in room.ventilation
                },
                "sensor_board_reported": has_sensor_board(device),
                "sensor_mode_supported": supports_sensor_mode(room, device),
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
                "humidity_status_raw": HUMIDITY_SENSOR_STATUS.value(device),
                "co2_status_raw": CO2_SENSOR_STATUS.value(device),
                "exhaust_fan_speed": EXHAUST_FAN_SPEED.value(device),
                "supply_fan_speed": SUPPLY_FAN_SPEED.value(device),
            }
            for room in snapshot.rooms
            for device in room.devices
        ],
    }

    # Temporary: numeric room/device fields and a one-off, read-only property
    # scan, used to locate sensor-board values and locally changed fan levels.
    try:
        result["raw_rooms"] = _raw_rooms(snapshot)
    except ProtobufDecodeError:
        result["raw_rooms"] = {"status": "failed", "error": "ProtobufDecodeError"}
    try:
        result["property_scan"] = await coordinator.async_scan_properties()
    except Exception as err:  # noqa: BLE001 - diagnostics must still be returned
        result["property_scan"] = {"status": "failed", "error": type(err).__name__}
    return result
