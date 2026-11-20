"""Shared connection and state coordinator for Zehnder ConnectBox."""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Callable
from datetime import timedelta
from functools import partial

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    HomeAssistantError,
    ServiceValidationError,
)
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
from .errors import CommandValidationError
from .models import GatewaySnapshot, Room, RunMode, RunState
from .profiles import (
    CANDIDATE_WRITE_LOG_SIZE,
    CAPTURE_PROPERTY_GROUPS,
    CAPTURE_TIME_BUDGET,
    FILTER_RUNTIME,
    format_version,
    level_one_runtime_key,
    product_name,
    summer_ventilation_role_key,
    supports_sensor_status,
)
from .protocol import ProtocolError
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
        # Temporary test build: outcome of the boost candidate writes, shown
        # in the diagnostics capture.
        self.candidate_writes: list[dict[str, object]] = []
        # Temporary capture build: units whose app properties are read when
        # the diagnostics are downloaded (see async_capture). Not stored.
        self.capture_device_ids: set[int] = set()

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
            raise ConfigEntryAuthFailed(
                translation_domain=DOMAIN,
                translation_key="certificate_changed",
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
        snapshot = await self._async_execute_command(
            partial(self.client.set_level, room_id, level, temperature_mode)
        )
        self._accept_command_snapshot(snapshot)

    async def async_set_situation(self, temperature_mode: int) -> None:
        """Select a situation of the manual mode and confirm it."""
        snapshot = await self._async_execute_command(
            partial(self.client.set_situation, temperature_mode)
        )
        self._accept_command_snapshot(snapshot)

    async def async_set_mode(self, mode: RunMode) -> None:
        """Set and confirm the system run mode."""
        snapshot = await self._async_execute_command(
            partial(self.client.set_run_mode, mode)
        )
        self._accept_command_snapshot(snapshot)

    async def async_set_summer_ventilation(self, enabled: bool) -> None:
        """Set and confirm the gateway-wide summer ventilation interval."""
        snapshot = await self._async_execute_command(
            partial(self.client.set_summer_ventilation, enabled)
        )
        self._accept_command_snapshot(snapshot)

    async def async_set_summer_configuration(
        self, *, enabled: bool | None = None, duration_hours: int | None = None
    ) -> None:
        """Set and confirm the gateway-wide summer function configuration."""
        snapshot = await self._async_execute_command(
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
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="device_unavailable"
            )
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
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="filter_runtime_unavailable"
            )

        snapshot = await self._async_execute_command(
            partial(self.client.reset_filter_timer, device_id, property_value.key)
        )
        self._last_property_refresh = time.monotonic()
        self._last_filter_property_refresh = self._last_property_refresh
        self._accept_command_snapshot(snapshot)

    async def async_set_summer_ventilation_role(
        self, device_id: int, role: int
    ) -> None:
        """Write a unit's summer ventilation role and confirm it."""
        data = self.data.find_device(device_id) if self.data is not None else None
        if data is None:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="device_unavailable"
            )
        key = summer_ventilation_role_key(data[1])
        if key is None:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="summer_role_unavailable"
            )
        snapshot = await self._async_execute_command(
            partial(self.client.set_summer_ventilation_role, device_id, key, role)
        )
        self._accept_command_snapshot(snapshot)

    async def async_set_level_one_runtime(self, device_id: int, minutes: int) -> None:
        """Write a unit's runtime of level 1 per hour and confirm it."""
        data = self.data.find_device(device_id) if self.data is not None else None
        if data is None:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="device_unavailable"
            )
        key = level_one_runtime_key(data[1])
        if key is None:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="level_one_runtime_unavailable",
            )
        snapshot = await self._async_execute_command(
            partial(self.client.set_level_one_runtime, device_id, key, minutes)
        )
        self._accept_command_snapshot(snapshot)

    async def async_set_boost_duration(self, room_id: int, minutes: int) -> None:
        """Temporary test build: write a room's boost duration and log it."""
        await self._async_boost_candidate(
            room_id,
            "boost_duration",
            minutes,
            partial(self.client.set_boost_duration, room_id, minutes),
        )

    async def async_set_boost(self, room_id: int, active: bool) -> None:
        """Start or stop a room's boost and log the outcome for the capture."""
        data = self.data
        room = (
            next((item for item in data.rooms if item.room_id == room_id), None)
            if data is not None
            else None
        )
        duration = room.boost_duration if room is not None else None
        await self._async_boost_candidate(
            room_id,
            "boost_start" if active else "boost_stop",
            (duration or 0) if active else 0,
            partial(self.client.set_boost, room_id, active),
        )

    async def _async_boost_candidate(
        self,
        room_id: int,
        write: str,
        value: int,
        command: Callable[[], GatewaySnapshot],
    ) -> None:
        """Temporary test build: run a boost candidate write and log its outcome.

        The log is listed in the diagnostics capture under candidate_writes,
        with the gateway's answer and the boost values it reports afterwards.
        """

        def read_back(snapshot: GatewaySnapshot) -> dict[str, object] | None:
            room = next(
                (item for item in snapshot.rooms if item.room_id == room_id), None
            )
            if room is None:
                return None
            return {
                "boost_duration": room.boost_duration,
                "boost_ends_in_seconds": (
                    room.boost_until - int(time.time()) if room.boost_until else None
                ),
            }

        await self._async_logged_write(
            {"room": room_id, "write": write, "value": value}, command, read_back
        )

    async def _async_logged_write(
        self,
        details: dict[str, object],
        command: Callable[[], GatewaySnapshot],
        read_back: Callable[[GatewaySnapshot], dict[str, object] | None],
    ) -> None:
        """Run a write and log its outcome under candidate_writes."""
        record: dict[str, object] = {
            "at": dt_util.utcnow().isoformat(timespec="seconds"),
            **details,
            "result": "pending",
        }
        self.candidate_writes.append(record)
        del self.candidate_writes[:-CANDIDATE_WRITE_LOG_SIZE]
        try:
            snapshot = await self._async_execute_command(command)
        except HomeAssistantError as err:
            cause = err.__cause__
            if isinstance(cause, GatewayResponseError):
                record["result"] = f"rejected: result {cause.result}: {cause}"
            elif cause is not None:
                record["result"] = f"{type(cause).__name__}: {cause}"
            else:
                record["result"] = type(err).__name__
            raise
        record["result"] = "confirmed"
        values = read_back(snapshot)
        if values is not None:
            record["read_back"] = values
        self._accept_command_snapshot(snapshot)

    async def async_capture(
        self,
    ) -> tuple[RunState, tuple[Room, ...], dict[int, dict[str, object]]]:
        """Temporary capture build: read app properties and a fresh state.

        Used only when the diagnostics are downloaded. The app's properties are
        requested only for the ComfoSpot 50 units whose capture switch is on,
        because every property read goes over the radio link and counts
        against its duty cycle; nothing is written. The room model and run
        state are always read fresh; they come from the gateway itself.
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
            and device.device_id in self.capture_device_ids
        )
        deadline = time.monotonic() + CAPTURE_TIME_BUDGET
        async with self._io_lock:
            return await self.hass.async_add_executor_job(
                partial(self.client.capture, targets, deadline=deadline)
            )

    async def async_unsolicited_frames(
        self,
    ) -> dict[int, tuple[int, tuple[tuple[float, int | None, int | None, bytes], ...]]]:
        """Temporary capture build: copy the frames that answered no request.

        Taken under the I/O lock, so no request adds a frame meanwhile.
        """
        async with self._io_lock:
            return {
                operation_type: (entry.count, tuple(entry.last))
                for operation_type, entry in self.client.unsolicited_frames.items()
            }

    async def async_close(self) -> None:
        """Close the client outside Home Assistant's event loop."""
        async with self._io_lock:
            await self.hass.async_add_executor_job(self.client.close)

    async def _async_execute_command(
        self, command: Callable[[], GatewaySnapshot]
    ) -> GatewaySnapshot:
        """Serialize commands and translate expected failures at the HA boundary."""
        try:
            async with self._io_lock:
                return await self.hass.async_add_executor_job(command)
        except CommandValidationError as err:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key=err.translation_key
            ) from err
        except CertificateMismatchError as err:
            self.entry.async_start_reauth(self.hass)
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="certificate_changed"
            ) from err
        except GatewayResponseError as err:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="command_rejected"
            ) from err
        except ProtocolError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="command_failed"
            ) from err
        except (TransportError, OSError) as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="cannot_connect"
            ) from err

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
