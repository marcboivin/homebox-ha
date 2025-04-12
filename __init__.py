"""The Homebox integration."""
from __future__ import annotations

import logging
from datetime import timedelta

import aiohttp
import async_timeout
import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import entity_registry
from homeassistant.helpers.typing import ConfigType
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    DOMAIN, 
    CONF_URL, 
    CONF_TOKEN,
    CONF_USE_HTTPS,
    HOMEBOX_API_URL,
    COORDINATOR,
    SERVICE_MOVE_ITEM,
    ATTR_ITEM_ID,
    ATTR_LOCATION_ID,
)

_LOGGER = logging.getLogger(__name__)

CONFIG_SCHEMA = vol.Schema({DOMAIN: vol.Schema({})}, extra=vol.ALLOW_EXTRA)

PLATFORMS: list[str] = ["sensor"]

MOVE_ITEM_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_ITEM_ID): str,
        vol.Required(ATTR_LOCATION_ID): str,
    }
)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the Homebox component."""
    hass.data.setdefault(DOMAIN, {})
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Homebox from a config entry."""
    session = async_get_clientsession(hass)
    
    # Determine the protocol (http or https)
    use_https = entry.data.get(CONF_USE_HTTPS, True)
    protocol = "https" if use_https else "http"
    
    # Construct the base URL
    base_url = f"{protocol}://{entry.data[CONF_URL]}"
    
    coordinator = HomeboxDataUpdateCoordinator(
        hass, 
        _LOGGER, 
        name=DOMAIN,
        session=session,
        api_url=base_url,
        token=entry.data[CONF_TOKEN],
    )

    await coordinator.async_config_entry_first_refresh()
    hass.data[DOMAIN][entry.entry_id] = {COORDINATOR: coordinator}

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    
    # Register services
    async def handle_move_item(call: ServiceCall) -> None:
        """Handle the move item service call."""
        item_id = call.data.get(ATTR_ITEM_ID)
        location_id = call.data.get(ATTR_LOCATION_ID)
        result = await coordinator.move_item(item_id, location_id)
        if not result:
            _LOGGER.error(
                "Failed to move item %s to location %s", 
                item_id, 
                location_id
            )

    hass.services.async_register(
        DOMAIN, SERVICE_MOVE_ITEM, handle_move_item, schema=MOVE_ITEM_SCHEMA
    )
    
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)
    
    return unload_ok


class HomeboxDataUpdateCoordinator(DataUpdateCoordinator):
    """Class to manage fetching Homebox data."""

    def __init__(
        self,
        hass: HomeAssistant,
        logger: logging.Logger,
        name: str,
        session: aiohttp.ClientSession,
        api_url: str,
        token: str,
    ) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            logger,
            name=name,
            update_interval=timedelta(minutes=30),
        )
        self.session = session
        self.api_url = api_url.rstrip("/")  # Base URL without /api/v1
        self.token = token
        self.locations = {}
        self.items = {}

    async def _async_update_data(self) -> dict:
        """Fetch data from Homebox API."""
        try:
            async with async_timeout.timeout(30):
                # Fetch locations first
                locations = await self._fetch_locations()
                self.locations = {loc["id"]: loc for loc in locations}
                
                # Fetch items
                items = await self._fetch_items()
                self.items = {item["id"]: item for item in items}
                
                return {
                    "locations": self.locations,
                    "items": self.items,
                }
                
        except aiohttp.ClientError as err:
            _LOGGER.error("Error communicating with API: %s - URL: %s", err, self.api_url)
            raise UpdateFailed(f"Error communicating with API: {err}") from err
        except Exception as err:
            _LOGGER.error("Error updating data: %s", err)
            raise UpdateFailed(f"Error updating data: {err}") from err

    async def _fetch_locations(self) -> list:
        """Fetch locations from the API."""
        headers = {"Authorization": f"Bearer {self.token}"}
        url = f"{self.api_url}/api/v1/locations"
        
        try:
            _LOGGER.debug("Fetching locations from URL: %s", url)
            async with self.session.get(url, headers=headers) as resp:
                resp.raise_for_status()
                data = await resp.json()
                return data
        except aiohttp.ClientError as err:
            _LOGGER.error("Error fetching locations: %s - URL: %s", err, url)
            raise
    
    async def _fetch_items(self) -> list:
        """Fetch items from the API."""
        headers = {"Authorization": f"Bearer {self.token}"}
        url = f"{self.api_url}/api/v1/items"
        
        try:
            _LOGGER.debug("Fetching items from URL: %s", url)
            async with self.session.get(url, headers=headers) as resp:
                resp.raise_for_status()
                data = await resp.json()
                return data
        except aiohttp.ClientError as err:
            _LOGGER.error("Error fetching items: %s - URL: %s", err, url)
            raise
            
    async def move_item(self, item_id: str, location_id: str) -> bool:
        """Move an item to a new location."""
        if item_id not in self.items:
            _LOGGER.error("Item ID %s not found", item_id)
            return False
            
        item = self.items[item_id]
        
        # Prepare the update data
        update_data = {
            "locationId": location_id
        }
        
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json"
        }
        
        url = f"{self.api_url}/api/v1/items/{item_id}"
        
        try:
            _LOGGER.debug("Moving item, URL: %s", url)
            async with self.session.put(url, headers=headers, json=update_data) as resp:
                resp.raise_for_status()
                # Update local data
                self.items[item_id]["locationId"] = location_id
                await self.async_request_refresh()
                return True
        except Exception as err:
            _LOGGER.error("Failed to move item: %s - URL: %s", err, url)
            return False