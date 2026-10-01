"""Maintenance actions for supported ConnectBox ventilation units."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import ZehnderConnectBoxConfigEntry
from .const import CONF_GATEWAY_UUID, PRODUCT_VARIANT_COMFOSPOT_50
from .entity import ConnectBoxDeviceEntity
from .models import AttachedDevice
from .profiles import (
    FILTER_RUNTIME,
    SUPPLY_ONLY_CANDIDATE_VALUES,
    is_supported,
    supply_only_candidate_key,
)
from .protocol import ProtocolError
from .transport import TransportError


def _supports_filter_reset(device: AttachedDevice) -> bool:
    """Return whether the verified ComfoSpot filter counter is writable."""
    value = device.property_bytes(FILTER_RUNTIME.key)
    return (
        is_supported(device)
        and device.product_variant == PRODUCT_VARIANT_COMFOSPOT_50
        and value is not None
        and len(value) == FILTER_RUNTIME.length
    )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ZehnderConnectBoxConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up filter-reset buttons and add newly attached devices dynamically."""
    coordinator = entry.runtime_data
    known: set[int] = set()
    known_candidates: set[int] = set()

    @callback
    def add_new_entities() -> None:
        if coordinator.data is None:
            return
        devices = [device for room in coordinator.data.rooms for device in room.devices]
        supported = {
            device.device_id for device in devices if _supports_filter_reset(device)
        }
        new_ids = supported - known
        if new_ids:
            async_add_entities(
                ConnectBoxFilterResetButton(coordinator, device_id)
                for device_id in sorted(new_ids)
            )
            known.update(new_ids)

        # Temporary test build: supply-only candidate buttons.
        candidates = {
            device.device_id
            for device in devices
            if supply_only_candidate_key(device) is not None
        }
        new_candidates = candidates - known_candidates
        if new_candidates:
            async_add_entities(
                ConnectBoxSupplyOnlyCandidateButton(coordinator, device_id, value)
                for device_id in sorted(new_candidates)
                for value in SUPPLY_ONLY_CANDIDATE_VALUES
            )
            known_candidates.update(new_candidates)

    add_new_entities()
    entry.async_on_unload(coordinator.async_add_listener(add_new_entities))


class ConnectBoxFilterResetButton(ConnectBoxDeviceEntity, ButtonEntity):
    """Reset the filter timer after physical filter maintenance."""

    _attr_translation_key = "reset_filter_timer"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:air-filter"

    def __init__(self, coordinator, device_id: int) -> None:
        super().__init__(coordinator, device_id)
        gateway_uuid = coordinator.entry.data[CONF_GATEWAY_UUID]
        self._attr_unique_id = f"{gateway_uuid}_{device_id}_reset_filter_timer"

    @property
    def available(self) -> bool:
        """Only allow reset while the verified runtime property is available."""
        if not super().available:
            return False
        data = self.device_data
        return data is not None and _supports_filter_reset(data[1])

    async def async_press(self) -> None:
        """Run the gateway-confirmed filter reset sequence."""
        await self.coordinator.async_reset_filter_timer(self.device_id)


class ConnectBoxSupplyOnlyCandidateButton(ConnectBoxDeviceEntity, ButtonEntity):
    """Temporary test build: write the supply-only candidate 38.0.5.

    Pressing it sends one device-property write with the button's value. It
    tests whether the unit accepts the value it reported in supply-only
    operation as a command; the outcome is logged in the diagnostics.
    """

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:test-tube"

    def __init__(self, coordinator, device_id: int, value: int) -> None:
        super().__init__(coordinator, device_id)
        self._value = value
        self._attr_translation_key = f"supply_only_candidate_{value}"
        gateway_uuid = coordinator.entry.data[CONF_GATEWAY_UUID]
        self._attr_unique_id = (
            f"{gateway_uuid}_{device_id}_supply_only_candidate_{value}"
        )

    @property
    def available(self) -> bool:
        """Only offer the write while the unit reports the fan's values."""
        if not super().available:
            return False
        data = self.device_data
        return data is not None and supply_only_candidate_key(data[1]) is not None

    async def async_press(self) -> None:
        """Send the candidate write and report a rejection."""
        try:
            await self.coordinator.async_write_supply_only_candidate(
                self.device_id, self._value
            )
        except (ProtocolError, TransportError, OSError) as err:
            raise HomeAssistantError(
                f"Test write 38.0.5 = {self._value} failed: {err}"
            ) from err
