"""Zehnder ConnectBox integration."""

from __future__ import annotations

from uuid import UUID

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import CONF_HOST, Platform
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
)
from homeassistant.exceptions import HomeAssistantError

from .client import ConnectBoxClient
from .const import (
    CONF_APP_UUID,
    CONF_CERTIFICATE_SHA256,
    CONF_GATEWAY_UUID,
    DOMAIN,
)
from .coordinator import ZehnderConnectBoxCoordinator

PLATFORMS = (
    Platform.BUTTON,
    Platform.FAN,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SWITCH,
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
)

type ZehnderConnectBoxConfigEntry = ConfigEntry[ZehnderConnectBoxCoordinator]


async def async_setup_entry(
    hass: HomeAssistant, entry: ZehnderConnectBoxConfigEntry
) -> bool:
    """Set up one paired ConnectBox."""
    client = ConnectBoxClient(
        entry.data[CONF_HOST],
        UUID(entry.data[CONF_GATEWAY_UUID]),
        UUID(entry.data[CONF_APP_UUID]),
        entry.data[CONF_CERTIFICATE_SHA256],
    )
    coordinator = ZehnderConnectBoxCoordinator(hass, entry, client)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    _register_test_request(hass)
    return True


# Temporary test build: send one request to the ConnectBox and return what
# answers it, by structure. Used to match the official app's schedule and
# absence-planner messages; removed again once they are known.
SERVICE_TEST_REQUEST = "test_request"
TEST_REQUEST_SCHEMA = vol.Schema(
    {
        vol.Required("request"): vol.All(int, vol.Range(min=1, max=1023)),
        vol.Optional("body", default=""): str,
        vol.Optional("show_text", default=False): bool,
    }
)


def _register_test_request(hass: HomeAssistant) -> None:
    if hass.services.has_service(DOMAIN, SERVICE_TEST_REQUEST):
        return

    async def handle(call: ServiceCall) -> ServiceResponse:
        from .diagnostics import test_response_structure
        from .protocol import ProtocolError
        from .transport import TransportError

        entries = [
            item
            for item in hass.config_entries.async_entries(DOMAIN)
            if item.state is ConfigEntryState.LOADED
        ]
        if not entries:
            raise HomeAssistantError("no ConnectBox is loaded")
        entry = entries[0]
        try:
            body = bytes.fromhex(call.data["body"])
        except ValueError as err:
            raise HomeAssistantError("body must be hexadecimal") from err
        try:
            answer_type, result, description, answer = (
                await entry.runtime_data.async_test_request(call.data["request"], body)
            )
        except (ProtocolError, TransportError, OSError) as err:
            return {"error": f"{type(err).__name__}: {err}"}
        return {
            "type": answer_type,
            "result": result,
            "description": description,
            "length": len(answer),
            "body": test_response_structure(
                entry, answer, show_text=call.data["show_text"]
            ),
        }

    hass.services.async_register(
        DOMAIN,
        SERVICE_TEST_REQUEST,
        handle,
        schema=TEST_REQUEST_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )


async def async_unload_entry(
    hass: HomeAssistant, entry: ZehnderConnectBoxConfigEntry
) -> bool:
    """Unload an entry and close its local connection."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.async_close()
    return unloaded
