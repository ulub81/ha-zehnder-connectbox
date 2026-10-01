"""Standby and boost controls for Zehnder ConnectBox."""

from __future__ import annotations

import time

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import ZehnderConnectBoxConfigEntry
from .const import CONF_GATEWAY_UUID
from .entity import (
    ConnectBoxDeviceEntity,
    ConnectBoxGatewayEntity,
    supported_device_ids,
)
from .models import RunMode


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ZehnderConnectBoxConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the gateway standby switch and a boost switch per unit."""
    coordinator = entry.runtime_data
    async_add_entities([ConnectBoxVentilationSwitch(coordinator)])
    known: set[int] = set()

    @callback
    def add_new_entities() -> None:
        new_ids = supported_device_ids(coordinator) - known
        if not new_ids:
            return
        known.update(new_ids)
        async_add_entities(
            ConnectBoxBoostSwitch(coordinator, device_id) for device_id in sorted(new_ids)
        )

    add_new_entities()
    entry.async_on_unload(coordinator.async_add_listener(add_new_entities))


class ConnectBoxVentilationSwitch(ConnectBoxGatewayEntity, SwitchEntity):
    """Expose global standby as an on/off control."""

    _attr_translation_key = "central_ventilation"

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator)
        gateway_uuid = coordinator.entry.data[CONF_GATEWAY_UUID]
        self._attr_unique_id = f"{gateway_uuid}_ventilation_power"

    @property
    def is_on(self) -> bool | None:
        """Return whether the gateway is outside standby."""
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.run_state.run_mode != RunMode.OFF

    async def async_turn_on(self, **kwargs) -> None:
        """Restore the most recently verified active mode."""
        await self.coordinator.async_set_power(True)

    async def async_turn_off(self, **kwargs) -> None:
        """Put all attached ventilation units into standby."""
        await self.coordinator.async_set_power(False)


class ConnectBoxBoostSwitch(ConnectBoxDeviceEntity, SwitchEntity):
    """Run the boost of the unit's room for the duration set in the app."""

    _attr_translation_key = "boost"

    def __init__(self, coordinator, device_id: int) -> None:
        super().__init__(coordinator, device_id)
        gateway_uuid = coordinator.entry.data[CONF_GATEWAY_UUID]
        self._attr_unique_id = f"{gateway_uuid}_{device_id}_boost"

    @property
    def is_on(self) -> bool | None:
        """Return whether the room's boost is running."""
        data = self.device_data
        if data is None:
            return None
        return data[0].boost_active(time.time())

    async def async_turn_on(self, **kwargs) -> None:
        """Start the boost."""
        data = self.device_data
        if data is not None:
            await self.coordinator.async_set_boost(data[0].room_id, True)

    async def async_turn_off(self, **kwargs) -> None:
        """End the boost early."""
        data = self.device_data
        if data is not None:
            await self.coordinator.async_set_boost(data[0].room_id, False)
