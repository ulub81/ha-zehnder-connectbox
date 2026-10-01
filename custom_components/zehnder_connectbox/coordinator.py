"""Shared connection and state coordinator for Zehnder ConnectBox."""

from __future__ import annotations

import asyncio
import logging
import random
import time
from datetime import timedelta
from functools import partial

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .client import ConnectBoxClient
from .const import (
    CONF_GATEWAY_UUID,
    DEFAULT_NAME,
    DOMAIN,
    FILTER_PROPERTY_REFRESH_INTERVAL,
    POLL_INTERVAL,
    PROPERTY_REFRESH_INTERVAL,
)
from .models import GatewaySnapshot, Room, RunMode, RunState
from .profiles import (
    CANDIDATE_WRITE_LOG_SIZE,
    CAPTURE_PROPERTY_GROUPS,
    CAPTURE_TIME_BUDGET,
    FILTER_RUNTIME,
    ROOM_STATE_FAN_CLASS,
    SUPPLY_ONLY_CANDIDATE,
    format_version,
    product_name,
    supply_only_candidate_key,
    supports_sensor_status,
)
from .protocol import ProtocolError, room_state_record
from .session import GatewayResponseError
from .transport import CertificateMismatchError, TransportError

_LOGGER = logging.getLogger(__name__)
MAX_RETRY_INTERVAL = 300


class ZehnderConnectBoxCoordinator(DataUpdateCoordinator[GatewaySnapshot]):
    """Coordinate all I/O for one ConnectBox config entry."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: ConnectBoxClient,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=entry.title or DEFAULT_NAME,
            update_interval=POLL_INTERVAL,
            config_entry=entry,
        )
        self.entry = entry
        self.client = client
        self._last_property_refresh = 0.0
        self._last_filter_property_refresh = 0.0
        self._failures = 0
        self._last_non_off_mode = RunMode.MANUAL
        self._io_lock = asyncio.Lock()
        # Temporary test build: outcome of the supply-only candidate writes,
        # shown in the diagnostics capture.
        self.candidate_writes: list[dict[str, object]] = []

    async def _async_update_data(self) -> GatewaySnapshot:
        now = time.monotonic()
        refresh_properties = (
            self.data is None
            or now - self._last_property_refresh
            >= PROPERTY_REFRESH_INTERVAL
        )
        refresh_filter_properties = (
            self.data is None
            or now - self._last_filter_property_refresh
            >= FILTER_PROPERTY_REFRESH_INTERVAL
        )
        try:
            async with self._io_lock:
                snapshot = await self.hass.async_add_executor_job(
                    partial(
                        self.client.read_snapshot,
                        refresh_properties=(
                            refresh_properties or refresh_filter_properties
                        ),
                        refresh_filter_properties=refresh_filter_properties,
                    )
                )
        except CertificateMismatchError as err:
            self._set_failure_interval()
            raise UpdateFailed(
                "The ConnectBox certificate no longer matches the paired gateway"
            ) from err
        except (ProtocolError, TransportError, OSError) as err:
            self._set_failure_interval()
            raise UpdateFailed("Unable to communicate with the ConnectBox") from err

        self._failures = 0
        self.update_interval = POLL_INTERVAL
        if refresh_properties or refresh_filter_properties:
            self._last_property_refresh = time.monotonic()
        if refresh_filter_properties:
            self._last_filter_property_refresh = self._last_property_refresh
        self._remember_mode(snapshot)
        self._register_devices(snapshot)
        return snapshot

    async def async_set_level(
        self, room_id: int, level: int, temperature_mode: int | None = None
    ) -> None:
        """Set and confirm a room level for the active or a given situation."""
        async with self._io_lock:
            snapshot = await self.hass.async_add_executor_job(
                self.client.set_level, room_id, level, temperature_mode
            )
        self._accept_command_snapshot(snapshot)

    async def async_set_situation(self, temperature_mode: int) -> None:
        """Select a situation of the manual mode and confirm it."""
        async with self._io_lock:
            snapshot = await self.hass.async_add_executor_job(
                self.client.set_situation, temperature_mode
            )
        self._accept_command_snapshot(snapshot)

    async def async_set_mode(self, mode: RunMode) -> None:
        """Set and confirm the system run mode."""
        async with self._io_lock:
            snapshot = await self.hass.async_add_executor_job(
                self.client.set_run_mode, mode
            )
        self._accept_command_snapshot(snapshot)

    async def async_set_summer_ventilation(self, enabled: bool) -> None:
        """Set and confirm the gateway-wide summer ventilation interval."""
        async with self._io_lock:
            snapshot = await self.hass.async_add_executor_job(
                self.client.set_summer_ventilation, enabled
            )
        self._accept_command_snapshot(snapshot)

    async def async_set_summer_configuration(
        self, *, enabled: bool | None = None, duration_hours: int | None = None
    ) -> None:
        """Set and confirm the gateway-wide summer function configuration."""
        async with self._io_lock:
            snapshot = await self.hass.async_add_executor_job(
                partial(
                    self.client.set_summer_configuration,
                    enabled=enabled,
                    duration_hours=duration_hours,
                )
            )
        self._accept_command_snapshot(snapshot)

    async def async_set_power(self, enabled: bool) -> None:
        """Enter standby or restore the last verified active mode."""
        mode = self._last_non_off_mode if enabled else RunMode.OFF
        await self.async_set_mode(mode)

    async def async_reset_filter_timer(self, device_id: int) -> None:
        """Reset a supported unit's filter timer and refresh its telemetry."""
        data = self.data.find_device(device_id) if self.data is not None else None
        if data is None:
            raise ProtocolError("device is no longer available")
        device = data[1]
        property_value = next(
            (
                value
                for value in device.properties
                if value.key.value_identity == FILTER_RUNTIME.key
                and value.value is not None
                and len(value.value) == FILTER_RUNTIME.length
            ),
            None,
        )
        if property_value is None:
            raise ProtocolError("filter runtime is not available for this device")

        async with self._io_lock:
            snapshot = await self.hass.async_add_executor_job(
                self.client.reset_filter_timer, device_id, property_value.key
            )
        self._last_property_refresh = time.monotonic()
        self._last_filter_property_refresh = self._last_property_refresh
        self._accept_command_snapshot(snapshot)

    def _start_test_write(self, **details: object) -> dict[str, object]:
        """Temporary test build: add a pending entry to the test-write log."""
        record: dict[str, object] = {
            "at": dt_util.utcnow().isoformat(timespec="seconds"),
            **details,
            "result": "pending",
        }
        self.candidate_writes.append(record)
        del self.candidate_writes[:-CANDIDATE_WRITE_LOG_SIZE]
        return record

    async def _run_test_write(
        self, record: dict[str, object], job, *args
    ) -> GatewaySnapshot:
        """Temporary test build: run a test write and log the gateway's answer."""
        try:
            async with self._io_lock:
                snapshot = await self.hass.async_add_executor_job(job, *args)
        except GatewayResponseError as err:
            record["result"] = f"rejected with result {err.result}"
            raise
        except Exception as err:
            record["result"] = f"failed: {type(err).__name__}: {err}"[:160]
            raise
        record["result"] = "confirmed"
        return snapshot

    async def async_write_supply_only_candidate(
        self, device_id: int, value: int
    ) -> None:
        """Temporary test build: write the supply-only candidate and log it."""
        data = self.data.find_device(device_id) if self.data is not None else None
        key = supply_only_candidate_key(data[1]) if data is not None else None
        if key is None:
            raise ProtocolError("the test property is not available for this device")

        record = self._start_test_write(
            device=device_id,
            target="property " + ".".join(str(part) for part in SUPPLY_ONLY_CANDIDATE),
            value=value,
        )
        snapshot = await self._run_test_write(
            record, self.client.write_supply_only_candidate, device_id, key, value
        )
        self._last_property_refresh = time.monotonic()
        self._accept_command_snapshot(snapshot)

    async def async_write_room_state_test(self, device_id: int, value: int) -> None:
        """Temporary test build: write the room's fan record byte and log it.

        The log keeps the record's first byte as the gateway reports it after
        the write, so a stored but ignored value can be told apart.
        """
        data = self.data.find_device(device_id) if self.data is not None else None
        room = data[0] if data is not None else None
        if room is None or room_state_record(room, ROOM_STATE_FAN_CLASS) is None:
            raise ProtocolError("the room has no fan record to write")

        record = self._start_test_write(
            device=device_id,
            room=room.room_id,
            target=f"room record class {ROOM_STATE_FAN_CLASS} byte 0",
            value=value,
        )
        snapshot = await self._run_test_write(
            record,
            self.client.write_room_state_test,
            room.room_id,
            ROOM_STATE_FAN_CLASS,
            value,
        )
        updated = next(
            (item for item in snapshot.rooms if item.room_id == room.room_id), None
        )
        stored = room_state_record(updated, ROOM_STATE_FAN_CLASS)
        record["read_back"] = stored[0] if stored else None
        self._last_property_refresh = time.monotonic()
        self._accept_command_snapshot(snapshot)

    async def async_capture(
        self,
    ) -> tuple[RunState, tuple[Room, ...], dict[int, dict[str, object]]]:
        """Temporary capture build: read app properties and a fresh state.

        Used only when the diagnostics are downloaded. The app's properties are
        requested for every ComfoSpot 50; nothing is written.
        """
        snapshot = self.data
        devices = (
            [device for room in snapshot.rooms for device in room.devices]
            if snapshot is not None
            else []
        )
        targets = tuple(
            (device.device_id, device.product_type, CAPTURE_PROPERTY_GROUPS)
            for device in devices
            if supports_sensor_status(device)
        )
        deadline = time.monotonic() + CAPTURE_TIME_BUDGET
        async with self._io_lock:
            return await self.hass.async_add_executor_job(
                partial(self.client.capture, targets, deadline=deadline)
            )

    async def async_close(self) -> None:
        """Close the client outside Home Assistant's event loop."""
        async with self._io_lock:
            await self.hass.async_add_executor_job(self.client.close)

    def _accept_command_snapshot(self, snapshot: GatewaySnapshot) -> None:
        self._remember_mode(snapshot)
        self._register_devices(snapshot)
        self.async_set_updated_data(snapshot)

    def _remember_mode(self, snapshot: GatewaySnapshot) -> None:
        try:
            mode = RunMode(snapshot.run_state.run_mode)
        except ValueError:
            return
        if mode is not RunMode.OFF:
            self._last_non_off_mode = mode

    def _set_failure_interval(self) -> None:
        self._failures += 1
        seconds = min(
            POLL_INTERVAL.total_seconds() * (2 ** (self._failures - 1)),
            MAX_RETRY_INTERVAL,
        )
        self.update_interval = timedelta(seconds=seconds + random.uniform(0, 2))

    def _register_devices(self, snapshot: GatewaySnapshot) -> None:
        registry = dr.async_get(self.hass)
        gateway_uuid = self.entry.data[CONF_GATEWAY_UUID]
        gateway_device = registry.async_get_or_create(
            config_entry_id=self.entry.entry_id,
            identifiers={(DOMAIN, gateway_uuid)},
            manufacturer="Zehnder",
            model="ConnectBox CU-RF-ZMA",
            name=self.entry.title or DEFAULT_NAME,
            sw_version=format_version(snapshot.version.connectbox_version),
        )
        for room in snapshot.rooms:
            for device in room.devices:
                registry.async_get_or_create(
                    config_entry_id=self.entry.entry_id,
                    identifiers={(DOMAIN, f"{gateway_uuid}:{device.device_id}")},
                    manufacturer="Zehnder",
                    model=product_name(device),
                    name=f"{room.name} {product_name(device)}",
                    hw_version=(
                        str(device.hardware_version)
                        if device.hardware_version not in (None, 0)
                        else None
                    ),
                    sw_version=format_version(device.software_version),
                    suggested_area=room.name,
                    via_device_id=gateway_device.id,
                )
