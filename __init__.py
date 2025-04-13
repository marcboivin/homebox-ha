"""The Homebox integration."""
from __future__ import annotations

import logging
import asyncio
from datetime import datetime, timedelta

import aiohttp
from aiohttp import ClientResponseError
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
    CONF_USERNAME,
    CONF_PASSWORD,
    CONF_AUTH_METHOD,
    AUTH_METHOD_LOGIN,
    AUTH_METHOD_TOKEN,
    CONF_USE_HTTPS,
    HOMEBOX_API_URL,
    COORDINATOR,
    SERVICE_MOVE_ITEM,
    ATTR_ITEM_ID,
    ATTR_LOCATION_ID,
    TOKEN_REFRESH_INTERVAL,
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

    # Store the entry_id so we can use it later for dynamic entity creation
    coordinator._entry_id = entry.entry_id
    coordinator._config_entry = entry

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
    # Cancel token refresh task if it exists
    coordinator = hass.data[DOMAIN][entry.entry_id].get(COORDINATOR)
    if coordinator and coordinator._token_refresh_task:
        coordinator._token_refresh_task.cancel()
        
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
        self.hass = hass
        self._entry_id = None
        self._config_entry = None
        self._entity_adder = None
        self._last_token_refresh = datetime.now()
        
        # Schedule token refresh task
        self._token_refresh_task = None
        if token:
            self._schedule_token_refresh()

    async def _async_update_data(self) -> dict:
        """Fetch data from Homebox API."""
        try:
            async with async_timeout.timeout(30):
                # Fetch locations first
                try:
                    locations = await self._fetch_locations()
                    # Check if locations is a list we can iterate through
                    if not isinstance(locations, list):
                        _LOGGER.error("Unexpected locations data format: %s", locations)
                        locations_dict = {}
                    else:
                        # Safely extract location data
                        locations_dict = {}
                        for loc in locations:
                            if isinstance(loc, dict) and "id" in loc:
                                locations_dict[loc["id"]] = loc
                            else:
                                _LOGGER.warning("Skipping invalid location data: %s", loc)
                    
                    self.locations = locations_dict
                    
                    # Fetch items
                    items = await self._fetch_items()
                    # Check if items is a list we can iterate through
                    if not isinstance(items, list):
                        _LOGGER.error("Unexpected items data format: %s", items)
                        items_dict = {}
                    else:
                        # Safely extract items data
                        items_dict = {}
                        for item in items:
                            if isinstance(item, dict) and "id" in item:
                                items_dict[item["id"]] = item
                            else:
                                _LOGGER.warning("Skipping invalid item data: %s", item)
                    
                    # Check for added or removed items
                    old_item_ids = set(self.items.keys())
                    new_item_ids = set(items_dict.keys())
                    
                    # Store the new items
                    self.items = items_dict
                    
                    # If we have an entity adder function, create new entities for new items
                    if self._entity_adder and hasattr(self.hass.data[DOMAIN], "entity_manager"):
                        added_items = new_item_ids - old_item_ids
                        removed_items = old_item_ids - new_item_ids
                        
                        if added_items:
                            _LOGGER.debug("Found %d new items to add as entities", len(added_items))
                            entity_manager = self.hass.data[DOMAIN]["entity_manager"]
                            
                            # Schedule the entity creation for the next event loop iteration
                            if self._config_entry and entity_manager:
                                self.hass.async_create_task(
                                    entity_manager.async_add_or_update_entities(
                                        self, self._config_entry, self._entity_adder
                                    )
                                )
                        
                        if removed_items:
                            _LOGGER.debug("Found %d items to remove from tracking", len(removed_items))
                            # Mark entities for removal
                            entity_manager = self.hass.data[DOMAIN]["entity_manager"]
                            if entity_manager:
                                entity_manager.remove_entities(list(removed_items))
                except Exception as data_err:
                    _LOGGER.exception("Error processing API data: %s", data_err)
                    # Provide empty data rather than failing
                    self.locations = {}
                    self.items = {}
                
                return {
                    "locations": self.locations,
                    "items": self.items,
                }
                
        except aiohttp.ClientError as err:
            status_code = getattr(err, 'status', 'unknown')
            _LOGGER.error("Error communicating with API: %s - HTTP Status: %s - URL: %s", err, status_code, self.api_url)
            raise UpdateFailed(f"Error communicating with API (HTTP {status_code}): {err}") from err
        except Exception as err:
            _LOGGER.error("Error updating data: %s", err)
            raise UpdateFailed(f"Error updating data: {err}") from err

    async def _schedule_token_refresh(self) -> None:
        """Schedule periodic token refresh."""
        if self._token_refresh_task is not None:
            self._token_refresh_task.cancel()
            
        # Schedule the first token refresh
        self._token_refresh_task = self.hass.async_create_task(self._refresh_token_periodically())
        
    async def _refresh_token_periodically(self) -> None:
        """Refresh the token periodically to prevent expiration."""
        try:
            while True:
                # Wait for refresh interval
                await asyncio.sleep(TOKEN_REFRESH_INTERVAL)
                
                # Only refresh if we have credential info and are using username/password auth
                if self._config_entry and self._config_entry.data.get(CONF_AUTH_METHOD) == AUTH_METHOD_LOGIN:
                    username = self._config_entry.data.get(CONF_USERNAME)
                    # We need to get the password from the options since it was removed from data
                    # Try to refresh the token
                    try:
                        _LOGGER.debug("Refreshing Homebox API token")
                        
                        # Try to use the refresh endpoint first
                        refresh_url = f"{self.api_url}/api/v1/users/refresh"
                        headers = {"Authorization": f"Bearer {self.token}"}
                        
                        async with self.session.get(refresh_url, headers=headers) as resp:
                            if resp.status == 200:
                                data = await resp.json()
                                if "token" in data:
                                    self.token = data["token"]
                                    _LOGGER.debug("Successfully refreshed API token")
                                    self._last_token_refresh = datetime.now()
                                    continue
                            
                            # If refresh token failed, we need to re-login
                            _LOGGER.debug("Token refresh failed, attempting to re-login")
                            
                            # If we have username and stored password, try to get a new token
                            if username and CONF_PASSWORD in self._config_entry.data:
                                from .config_flow import get_token_from_login
                                new_token = await get_token_from_login(
                                    self.session,
                                    f"{self.api_url}/api/v1",
                                    username,
                                    self._config_entry.data[CONF_PASSWORD]
                                )
                                if new_token:
                                    self.token = new_token
                                    _LOGGER.debug("Successfully obtained new token through login")
                                    self._last_token_refresh = datetime.now()
                    
                    except Exception as err:
                        _LOGGER.error("Error refreshing token: %s", err)
        
        except asyncio.CancelledError:
            # Task was cancelled, clean up
            _LOGGER.debug("Token refresh task cancelled")
        except Exception as err:
            _LOGGER.error("Unexpected error in token refresh task: %s", err)

    async def _fetch_locations(self) -> list:
        """Fetch locations from the API."""
        headers = {"Authorization": f"Bearer {self.token}"}
        url = f"{self.api_url}/api/v1/locations"
        
        try:
            _LOGGER.debug("Fetching locations from URL: %s", url)
            async with self.session.get(url, headers=headers) as resp:
                if resp.status == 401:
                    # Token might be expired, try to refresh it immediately
                    _LOGGER.warning("Authentication failed (401), attempting to refresh token")
                    await self._refresh_token_now()
                    
                    # Retry the request with the new token
                    headers = {"Authorization": f"Bearer {self.token}"}
                    async with self.session.get(url, headers=headers) as retry_resp:
                        if retry_resp.status != 200:
                            response_text = await retry_resp.text()
                            _LOGGER.error("Failed to fetch locations after token refresh - Status: %s, Response: %s", 
                                      retry_resp.status, response_text)
                            retry_resp.raise_for_status()
                        data = await retry_resp.json()
                elif resp.status != 200:
                    response_text = await resp.text()
                    _LOGGER.error("Failed to fetch locations - Status: %s, Response: %s, URL: %s", 
                              resp.status, response_text, url)
                    resp.raise_for_status()
                else:
                    data = await resp.json()
                
                # Add extra validation on response data
                if not isinstance(data, list):
                    _LOGGER.error("API returned locations in unexpected format. Expected list, got %s: %s",
                                  type(data).__name__, data)
                    return []
                
                return data
        except aiohttp.ClientError as err:
            status_code = getattr(getattr(err, 'request_info', None), 'status', 'unknown')
            _LOGGER.error("Error fetching locations: %s - HTTP Status: %s - URL: %s", err, status_code, url)
            raise
        except ValueError as err:
            # This will catch JSON decode errors
            _LOGGER.error("Error parsing locations JSON: %s - URL: %s", err, url)
            return []
            
    async def _refresh_token_now(self) -> bool:
        """Force an immediate token refresh."""
        try:
            # Try to use the refresh endpoint first
            refresh_url = f"{self.api_url}/api/v1/users/refresh"
            headers = {"Authorization": f"Bearer {self.token}"}
            
            async with self.session.get(refresh_url, headers=headers) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if "token" in data:
                        self.token = data["token"]
                        _LOGGER.debug("Successfully refreshed API token")
                        self._last_token_refresh = datetime.now()
                        return True
                
                # If refresh token failed and we have login credentials, try to re-login
                if self._config_entry and self._config_entry.data.get(CONF_AUTH_METHOD) == AUTH_METHOD_LOGIN:
                    username = self._config_entry.data.get(CONF_USERNAME)
                    if username and CONF_PASSWORD in self._config_entry.data:
                        from .config_flow import get_token_from_login
                        new_token = await get_token_from_login(
                            self.session,
                            f"{self.api_url}/api/v1",
                            username,
                            self._config_entry.data[CONF_PASSWORD]
                        )
                        if new_token:
                            self.token = new_token
                            _LOGGER.debug("Successfully obtained new token through login")
                            self._last_token_refresh = datetime.now()
                            return True
            
            return False
        except Exception as err:
            _LOGGER.error("Error during immediate token refresh: %s", err)
            return False
    
    async def _fetch_items(self) -> list:
        """Fetch items from the API."""
        headers = {"Authorization": f"Bearer {self.token}"}
        url = f"{self.api_url}/api/v1/items"
        
        try:
            _LOGGER.debug("Fetching items from URL: %s", url)
            async with self.session.get(url, headers=headers) as resp:
                if resp.status == 401:
                    # Token might be expired, try to refresh it immediately
                    _LOGGER.warning("Authentication failed (401), attempting to refresh token")
                    await self._refresh_token_now()
                    
                    # Retry the request with the new token
                    headers = {"Authorization": f"Bearer {self.token}"}
                    async with self.session.get(url, headers=headers) as retry_resp:
                        if retry_resp.status != 200:
                            response_text = await retry_resp.text()
                            _LOGGER.error("Failed to fetch items after token refresh - Status: %s, Response: %s", 
                                      retry_resp.status, response_text)
                            retry_resp.raise_for_status()
                        data = await retry_resp.json()
                elif resp.status != 200:
                    response_text = await resp.text()
                    _LOGGER.error("Failed to fetch items - Status: %s, Response: %s, URL: %s", 
                              resp.status, response_text, url)
                    resp.raise_for_status()
                else:
                    data = await resp.json()
                
                # Add extra validation on response data
                if not isinstance(data, list):
                    _LOGGER.error("API returned items in unexpected format. Expected list, got %s: %s",
                                  type(data).__name__, data)
                    return []
                    
                return data
        except aiohttp.ClientError as err:
            status_code = getattr(getattr(err, 'request_info', None), 'status', 'unknown')
            _LOGGER.error("Error fetching items: %s - HTTP Status: %s - URL: %s", err, status_code, url)
            raise
        except ValueError as err:
            # This will catch JSON decode errors
            _LOGGER.error("Error parsing items JSON: %s - URL: %s", err, url)
            return []
            
    async def move_item(self, item_id: str, location_id: str) -> bool:
        """Move an item to a new location."""
        if not self.items:
            _LOGGER.error("No items loaded yet - cannot move item")
            return False
            
        if item_id not in self.items:
            _LOGGER.error("Item ID %s not found in items: %s", item_id, list(self.items.keys()))
            return False
        
        # Extra validation to ensure item is a dictionary
        item = self.items[item_id]
        if not isinstance(item, dict):
            _LOGGER.error("Item with ID %s has invalid format: %s", item_id, item)
            return False
            
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
                if resp.status == 401:
                    # Token might be expired, try to refresh it immediately
                    _LOGGER.warning("Authentication failed (401), attempting to refresh token")
                    token_refreshed = await self._refresh_token_now()
                    
                    if token_refreshed:
                        # Retry the request with the new token
                        headers = {
                            "Authorization": f"Bearer {self.token}",
                            "Content-Type": "application/json"
                        }
                        async with self.session.put(url, headers=headers, json=update_data) as retry_resp:
                            if retry_resp.status != 200:
                                response_text = await retry_resp.text()
                                _LOGGER.error("Failed to move item after token refresh - Status: %s, Response: %s", 
                                          retry_resp.status, response_text)
                                return False
                            # Update local data
                            self.items[item_id]["locationId"] = location_id
                            await self.async_request_refresh()
                            return True
                    else:
                        # Token refresh failed
                        _LOGGER.error("Failed to move item: Token refresh failed")
                        return False
                elif resp.status != 200:
                    response_text = await resp.text()
                    _LOGGER.error("Failed to move item - Status: %s, Response: %s, URL: %s", 
                              resp.status, response_text, url)
                    resp.raise_for_status()
                    
                # Update local data
                self.items[item_id]["locationId"] = location_id
                await self.async_request_refresh()
                return True
        except aiohttp.ClientResponseError as err:
            _LOGGER.error("Failed to move item: HTTP %s - %s - URL: %s", 
                        err.status, err.message, url)
            return False
        except aiohttp.ClientError as err:
            status_code = getattr(getattr(err, 'request_info', None), 'status', 'unknown')
            _LOGGER.error("Failed to move item: %s - HTTP Status: %s - URL: %s", 
                        err, status_code, url)
            return False
        except Exception as err:
            _LOGGER.error("Failed to move item (unexpected error): %s - URL: %s", err, url)
            return False