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
from homeassistant.helpers import entity_registry, area_registry
from homeassistant.helpers.typing import ConfigType
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.components import persistent_notification

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
    SERVICE_REFRESH_TOKEN,
    SERVICE_CREATE_ITEM,
    SERVICE_SYNC_AREAS,
    ATTR_ITEM_ID,
    ATTR_LOCATION_ID,
    ATTR_ITEM_NAME,
    ATTR_ITEM_DESCRIPTION,
    ATTR_ITEM_QUANTITY,
    ATTR_ITEM_ASSET_ID,
    ATTR_ITEM_PURCHASE_PRICE,
    ATTR_ITEM_FIELDS,
    ATTR_ITEM_LABELS,
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
        
        # Check if the destination location matches a Home Assistant area
        area_id = None
        location_name = None
        
        # If we have a location ID, check against our known locations
        if location_id and location_id in coordinator.locations:
            location_name = coordinator.locations[location_id].get("name", "")
            
            # Check if there's a matching Home Assistant area with the same name
            ar = area_registry.async_get(hass)
            er = entity_registry.async_get(hass)
            
            # Find area with matching name (case insensitive)
            ha_areas = {area.name.lower(): area.id for area in ar.async_list_areas()}
            if location_name.lower() in ha_areas:
                area_id = ha_areas[location_name.lower()]
                _LOGGER.debug("Location %s (%s) matches Home Assistant area", 
                            location_name, location_id)
        
        # Move the item
        result = await coordinator.move_item(item_id, location_id)
        if not result:
            _LOGGER.error(
                "Failed to move item %s to location %s", 
                item_id, 
                location_id
            )
            
            # Create notification for failure
            persistent_notification.create(
                hass,
                f"Failed to move item {item_id} to location {location_id}",
                title="Item Move Failed",
                notification_id=f"{DOMAIN}_item_move_failed"
            )
        else:
            # Item was moved successfully
            item_name = coordinator.items.get(item_id, {}).get("name", f"Item {item_id}")
            
            # Create notification for success
            notification_text = f"Successfully moved item:\n- Name: {item_name}\n- To: {location_name}"
            
            # If there's a matching area, assign the entity to it
            area_assigned = False
            if area_id:
                er = entity_registry.async_get(hass)
                entity_unique_id = f"{DOMAIN}_{entry.entry_id}_{item_id}"
                entity_id = None
                
                # Find the entity by its unique ID
                for entity in er.entities.values():
                    if entity.unique_id == entity_unique_id:
                        entity_id = entity.entity_id
                        break
                
                if entity_id:
                    _LOGGER.info("Assigning entity %s to area %s (ID: %s)", 
                                entity_id, location_name, area_id)
                    er.async_update_entity(entity_id, area_id=area_id)
                    area_assigned = True
                    notification_text += f"\n- Assigned to area: {location_name}"
            
            persistent_notification.create(
                hass,
                notification_text,
                title="Item Moved",
                notification_id=f"{DOMAIN}_item_moved"
            )

    async def handle_refresh_token(call: ServiceCall) -> None:
        """Handle the refresh token service call with detailed logging."""
        # Create a StringIO to capture logs
        class TokenRefreshHandler(logging.Handler):
            """Handler to capture token refresh logs."""
            
            def __init__(self):
                """Initialize the handler."""
                super().__init__()
                self.logs = []
                
            def emit(self, record):
                """Process log record."""
                log_entry = self.format(record)
                self.logs.append(log_entry)
        
        # Add temporary handler to capture logs
        handler = TokenRefreshHandler()
        handler.setLevel(logging.DEBUG)
        handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
        _LOGGER.addHandler(handler)
        
        # Store current log level and set to DEBUG temporarily
        previous_level = _LOGGER.level
        _LOGGER.setLevel(logging.DEBUG)
        
        try:
            # Get the coordinator that has the token refresh method
            _LOGGER.info("Starting manual token refresh...")
            
            # Show current token (truncated)
            truncated_token = coordinator.token[:10] + "..." if coordinator.token and len(coordinator.token) > 13 else "[none]"
            _LOGGER.info("Current token: %s", truncated_token)
            
            # Perform token refresh
            result = await coordinator._refresh_token_now()
            
            # Log the result
            if result:
                new_token = coordinator.token[:10] + "..." if coordinator.token and len(coordinator.token) > 13 else "[none]"
                _LOGGER.info("Token refresh successful. New token: %s", new_token)
            else:
                _LOGGER.warning("Token refresh failed. Using existing token: %s", truncated_token)
                
            # Create a persistent notification with all the logs
            log_text = "\n".join(handler.logs)
            persistent_notification.create(
                hass,
                log_text,
                title="Token Refresh Results",
                notification_id=f"{DOMAIN}_token_refresh"
            )
            
        finally:
            # Restore previous logging configuration
            _LOGGER.removeHandler(handler)
            _LOGGER.setLevel(previous_level)

    # Register both services
    hass.services.async_register(
        DOMAIN, SERVICE_MOVE_ITEM, handle_move_item, schema=MOVE_ITEM_SCHEMA
    )
    
    # Register the token refresh service
    hass.services.async_register(
        DOMAIN, SERVICE_REFRESH_TOKEN, handle_refresh_token
    )
    
    # Create a service to create items
    CREATE_ITEM_SCHEMA = vol.Schema({
        vol.Required(ATTR_ITEM_NAME): str,
        vol.Required(ATTR_LOCATION_ID): str,
        vol.Optional(ATTR_ITEM_DESCRIPTION): str,
        vol.Optional(ATTR_ITEM_QUANTITY): vol.Coerce(int),
        vol.Optional(ATTR_ITEM_ASSET_ID): str,
        vol.Optional(ATTR_ITEM_PURCHASE_PRICE): vol.Coerce(float),
        vol.Optional(ATTR_ITEM_FIELDS): dict,
        vol.Optional(ATTR_ITEM_LABELS): list,
    })
    
    async def handle_create_item(call: ServiceCall) -> None:
        """Handle the create item service call."""
        # Check if location matches a Home Assistant area
        location_id = call.data.get(ATTR_LOCATION_ID)
        area_id = None
        location_name = None
        
        # If we have a location ID, check against our known locations
        if location_id and location_id in coordinator.locations:
            location_name = coordinator.locations[location_id].get("name", "")
            
            # Check if there's a matching Home Assistant area with the same name
            ar = area_registry.async_get(hass)
            er = entity_registry.async_get(hass)
            
            # Find area with matching name (case insensitive)
            ha_areas = {area.name.lower(): area.id for area in ar.async_list_areas()}
            if location_name.lower() in ha_areas:
                area_id = ha_areas[location_name.lower()]
                _LOGGER.debug("Location %s (%s) matches Home Assistant area", 
                            location_name, location_id)
            
        # Create the item
        result, item_id_or_error = await coordinator.create_item(call.data)
        
        if result:
            # Success! Create a notification to show the new item ID
            _LOGGER.info("Item created successfully with ID: %s", item_id_or_error)
            
            # Refresh data to get the latest items and make sure our entity is created
            await coordinator.async_refresh()
            
            # Try to find and register the entity with the area if applicable
            if area_id:
                # We need to wait for entity creation which may happen after the next refresh
                async def register_entity_with_area():
                    """Register the entity with the area after it's been created."""
                    # Wait for a moment to allow entity creation to complete
                    await asyncio.sleep(2)
                    
                    # Re-get the registry as it may have changed
                    er = entity_registry.async_get(hass)
                    
                    # Look for entities with this unique ID pattern
                    # The format is typically domain_entry_id_item_id
                    entity_unique_id = f"{DOMAIN}_{entry.entry_id}_{item_id_or_error}"
                    entity_id = None
                    
                    # Find the entity by its unique ID
                    for entity in er.entities.values():
                        if entity.unique_id == entity_unique_id:
                            entity_id = entity.entity_id
                            break
                    
                    if entity_id:
                        _LOGGER.info("Assigning entity %s to area %s (ID: %s)", 
                                    entity_id, location_name, area_id)
                        er.async_update_entity(entity_id, area_id=area_id)
                    else:
                        _LOGGER.warning("Could not find entity for newly created item to assign to area")
                
                # Schedule the area assignment
                hass.async_create_task(register_entity_with_area())
            
            persistent_notification.create(
                hass,
                f"Successfully created new item:\n"
                f"- Name: {call.data.get(ATTR_ITEM_NAME)}\n"
                f"- ID: {item_id_or_error}" + 
                (f"\n- Assigned to area: {location_name}" if area_id else ""),
                title="Item Created",
                notification_id=f"{DOMAIN}_item_created"
            )
        else:
            # Creation failed
            _LOGGER.error("Failed to create item: %s", item_id_or_error)
            
            persistent_notification.create(
                hass,
                f"Failed to create item: {item_id_or_error}",
                title="Item Creation Failed",
                notification_id=f"{DOMAIN}_item_creation_failed"
            )
    
    # Register the create item service
    hass.services.async_register(
        DOMAIN, SERVICE_CREATE_ITEM, handle_create_item, schema=CREATE_ITEM_SCHEMA
    )
    
    # Service to sync Home Assistant areas to Homebox locations
    async def handle_sync_areas(call: ServiceCall) -> None:
        """Handle syncing Home Assistant areas to Homebox locations."""
        # Get all Home Assistant areas
        ar = area_registry.async_get(hass)
        areas = ar.async_list_areas()
        
        # Prepare notification content
        created_count = 0
        already_exists_count = 0
        failed_count = 0
        failed_areas = []
        notification_lines = ["Sync results:"]
        
        # For each area, create a location in Homebox if it doesn't exist
        for area in areas:
            # Check if location already exists in Homebox (case-insensitive)
            exists, existing_id = coordinator.get_location_by_name(area.name)
            
            if exists:
                _LOGGER.debug("Location '%s' already exists in Homebox with ID: %s", area.name, existing_id)
                already_exists_count += 1
                continue
            
            # Create new location
            result, location_id_or_error = await coordinator.create_location(
                name=area.name,
                description=f"Synchronized from Home Assistant area: {area.name}"
            )
            
            if result:
                _LOGGER.info("Created Homebox location '%s' with ID: %s from HA area", 
                           area.name, location_id_or_error)
                created_count += 1
            else:
                _LOGGER.error("Failed to create Homebox location for area '%s': %s", 
                            area.name, location_id_or_error)
                failed_count += 1
                failed_areas.append(area.name)
        
        # Refresh to get the latest data
        await coordinator.async_refresh()
        
        # Create notification with results
        notification_lines.append(f"- Created: {created_count} locations")
        notification_lines.append(f"- Already existed: {already_exists_count} locations")
        
        if failed_count > 0:
            notification_lines.append(f"- Failed: {failed_count} locations")
            notification_lines.append("  Failed areas: " + ", ".join(failed_areas))
        
        persistent_notification.create(
            hass,
            "\n".join(notification_lines),
            title="Homebox Area Synchronization",
            notification_id=f"{DOMAIN}_sync_areas"
        )
    
    # Register the sync areas service
    hass.services.async_register(
        DOMAIN, SERVICE_SYNC_AREAS, handle_sync_areas
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
    """Class to manage fetching Homebox data and performing API operations."""
    
    async def async_added_to_hass(self) -> None:
        """When added to HASS, schedule token refresh."""
        await super().async_added_to_hass()
        if self.token:
            truncated_token = self.token[:10] + "..." if self.token and len(self.token) > 13 else "[none]"
            _LOGGER.debug("Setting up token refresh for token [%s] with API URL: %s", 
                         truncated_token, self.api_url)
            await self._schedule_token_refresh()

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
        # Token refresh will be scheduled in async_added_to_hass

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
                        # Show a truncated version of the token for debugging
                        truncated_token = self.token[:10] + "..." if self.token and len(self.token) > 13 else "[none]"
                        _LOGGER.debug("Refreshing Homebox API token [Current: %s] for API URL: %s", 
                                     truncated_token, self.api_url)
                        
                        # Try to use the refresh endpoint first
                        refresh_url = f"{self.api_url}/api/v1/users/refresh"
                        
                        # Make sure token doesn't already start with "Bearer"
                        token_value = self.token
                        if token_value.startswith("Bearer "):
                            token_value = token_value[7:]  # Remove "Bearer " prefix
                            
                        headers = {"Authorization": f"Bearer {token_value}"}
                        
                        async with self.session.get(refresh_url, headers=headers) as resp:
                            if resp.status == 200:
                                data = await resp.json()
                                if "token" in data:
                                    self.token = data["token"]
                                    new_truncated = self.token[:10] + "..." if self.token and len(self.token) > 13 else "[none]" 
                                    _LOGGER.debug("Successfully refreshed API token: %s → %s",
                                                 truncated_token, new_truncated)
                                    self._last_token_refresh = datetime.now()
                                    continue
                            
                            # If refresh token failed, we need to re-login
                            resp_status = resp.status
                            resp_text = await resp.text()
                            _LOGGER.debug("Token refresh failed (status: %s, response: %s), attempting to re-login", 
                                         resp_status, resp_text)
                            
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
                                    new_truncated = self.token[:10] + "..." if self.token and len(self.token) > 13 else "[none]"
                                    _LOGGER.debug("Successfully obtained new token through login: %s → %s", 
                                                 truncated_token, new_truncated)
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
        # Make sure token doesn't already start with "Bearer"
        token_value = self.token
        if token_value.startswith("Bearer "):
            token_value = token_value[7:]  # Remove "Bearer " prefix
            
        headers = {"Authorization": f"Bearer {token_value}"}
        url = f"{self.api_url}/api/v1/locations"
        
        try:
            # Show truncated token in logs
            truncated_token = self.token[:10] + "..." if self.token and len(self.token) > 13 else "[none]"
            _LOGGER.debug("Fetching locations from URL: %s with token: %s", url, truncated_token)
            async with self.session.get(url, headers=headers) as resp:
                if resp.status == 401:
                    # Token might be expired, try to refresh it immediately
                    resp_text = await resp.text()
                    _LOGGER.warning("Authentication failed (401, response: %s), attempting to refresh token", resp_text)
                    refresh_result = await self._refresh_token_now()
                    _LOGGER.debug("Token refresh result: %s", "Success" if refresh_result else "Failed")
                    
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
                
                # Check the format of the response
                # Some versions of Homebox return a paginated response with the locations in a 'locations' field
                # while others return the locations directly as a list
                if isinstance(data, dict) and "locations" in data and isinstance(data["locations"], list):
                    _LOGGER.debug("Handling paginated locations format from API")
                    locations_data = data["locations"]
                elif isinstance(data, list):
                    _LOGGER.debug("Handling direct locations list format from API")
                    locations_data = data
                else:
                    _LOGGER.error("API returned locations in unexpected format. Expected list or {locations: list}, got %s: %s",
                                 type(data).__name__, data)
                    return []
                
                return locations_data
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
            # Show a truncated version of the token for debugging
            truncated_token = self.token[:10] + "..." if self.token and len(self.token) > 13 else "[none]"
            _LOGGER.debug("Attempting immediate token refresh [Current: %s] for API URL: %s", 
                         truncated_token, self.api_url)
                         
            # Try to use the refresh endpoint first
            refresh_url = f"{self.api_url}/api/v1/users/refresh"
            
            # Make sure token doesn't already start with "Bearer"
            token_value = self.token
            if token_value.startswith("Bearer "):
                token_value = token_value[7:]  # Remove "Bearer " prefix
                
            headers = {"Authorization": f"Bearer {token_value}"}
            
            async with self.session.get(refresh_url, headers=headers) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if "token" in data:
                        self.token = data["token"]
                        new_truncated = self.token[:10] + "..." if self.token and len(self.token) > 13 else "[none]"
                        _LOGGER.debug("Successfully refreshed API token: %s → %s",
                                     truncated_token, new_truncated)
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
                            new_truncated = self.token[:10] + "..." if self.token and len(self.token) > 13 else "[none]"
                            _LOGGER.debug("Successfully obtained new token through login: %s → %s", 
                                         truncated_token, new_truncated)
                            self._last_token_refresh = datetime.now()
                            return True
            
            _LOGGER.debug("Token refresh failed via both refresh endpoint and login")
            return False
        except Exception as err:
            _LOGGER.error("Error during immediate token refresh: %s", err)
            return False
    
    async def _fetch_items(self) -> list:
        """Fetch items from the API."""
        # Make sure token doesn't already start with "Bearer"
        token_value = self.token
        if token_value.startswith("Bearer "):
            token_value = token_value[7:]  # Remove "Bearer " prefix
            
        headers = {"Authorization": f"Bearer {token_value}"}
        url = f"{self.api_url}/api/v1/items"
        
        try:
            # Show truncated token in logs
            truncated_token = self.token[:10] + "..." if self.token and len(self.token) > 13 else "[none]"
            _LOGGER.debug("Fetching items from URL: %s with token: %s", url, truncated_token)
            async with self.session.get(url, headers=headers) as resp:
                if resp.status == 401:
                    # Token might be expired, try to refresh it immediately
                    resp_text = await resp.text()
                    _LOGGER.warning("Authentication failed (401, response: %s), attempting to refresh token", resp_text)
                    refresh_result = await self._refresh_token_now()
                    _LOGGER.debug("Token refresh result: %s", "Success" if refresh_result else "Failed")
                    
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
                
                # Check the format of the response
                # Some versions of Homebox return a paginated response with the items in an 'items' field
                # while others return the items directly as a list
                if isinstance(data, dict) and "items" in data and isinstance(data["items"], list):
                    _LOGGER.debug("Handling paginated items format from API")
                    items_data = data["items"]
                elif isinstance(data, list):
                    _LOGGER.debug("Handling direct items list format from API")
                    items_data = data
                else:
                    _LOGGER.error("API returned items in unexpected format. Expected list or {items: list}, got %s: %s",
                                 type(data).__name__, data)
                    return []
                    
                return items_data
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
        
        # Make sure token doesn't already start with "Bearer"
        token_value = self.token
        if token_value.startswith("Bearer "):
            token_value = token_value[7:]  # Remove "Bearer " prefix
            
        headers = {
            "Authorization": f"Bearer {token_value}",
            "Content-Type": "application/json"
        }
        
        url = f"{self.api_url}/api/v1/items/{item_id}"
        
        try:
            # Show truncated token in logs
            truncated_token = self.token[:10] + "..." if self.token and len(self.token) > 13 else "[none]"
            _LOGGER.debug("Moving item, URL: %s with token: %s", url, truncated_token)
            async with self.session.put(url, headers=headers, json=update_data) as resp:
                if resp.status == 401:
                    # Token might be expired, try to refresh it immediately
                    resp_text = await resp.text()
                    _LOGGER.warning("Authentication failed (401, response: %s), attempting to refresh token", resp_text)
                    token_refreshed = await self._refresh_token_now()
                    _LOGGER.debug("Token refresh result: %s", "Success" if token_refreshed else "Failed")
                    
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
            
    def get_location_by_name(self, name: str) -> tuple[bool, str]:
        """Check if a location with the given name already exists.
        
        Args:
            name: Name of the location to check
            
        Returns:
            Tuple of (exists, location_id or None)
        """
        # Case-insensitive search for location by name
        for location_id, location in self.locations.items():
            if location.get("name", "").lower() == name.lower():
                return True, location_id
        return False, None

    async def create_location(self, name: str, description: str = "") -> tuple[bool, str]:
        """Create a new location in Homebox.
        
        Args:
            name: Name of the location
            description: Optional description
            
        Returns:
            Tuple of (success, location_id or error message)
        """
        # Make sure token doesn't already start with "Bearer"
        token_value = self.token
        if token_value.startswith("Bearer "):
            token_value = token_value[7:]  # Remove "Bearer " prefix
            
        headers = {
            "Authorization": f"Bearer {token_value}",
            "Content-Type": "application/json"
        }
        
        url = f"{self.api_url}/api/v1/locations"
        
        # Prepare the location data for API
        location_data = {
            "name": name,
            "description": description
        }
        
        try:
            # Show truncated token in logs
            truncated_token = self.token[:10] + "..." if self.token and len(self.token) > 13 else "[none]"
            _LOGGER.debug("Creating location, URL: %s with token: %s, data: %s", url, truncated_token, location_data)
            
            async with self.session.post(url, headers=headers, json=location_data) as resp:
                if resp.status == 401:
                    # Token might be expired, try to refresh it immediately
                    resp_text = await resp.text()
                    _LOGGER.warning("Authentication failed (401, response: %s), attempting to refresh token", resp_text)
                    token_refreshed = await self._refresh_token_now()
                    _LOGGER.debug("Token refresh result: %s", "Success" if token_refreshed else "Failed")
                    
                    if token_refreshed:
                        # Retry the request with the new token
                        token_value = self.token
                        if token_value.startswith("Bearer "):
                            token_value = token_value[7:]
                            
                        headers = {
                            "Authorization": f"Bearer {token_value}",
                            "Content-Type": "application/json"
                        }
                        
                        async with self.session.post(url, headers=headers, json=location_data) as retry_resp:
                            if retry_resp.status not in (200, 201):
                                response_text = await retry_resp.text()
                                _LOGGER.error("Failed to create location after token refresh - Status: %s, Response: %s", 
                                          retry_resp.status, response_text)
                                return False, f"HTTP {retry_resp.status}: {response_text}"
                                
                            # Location created successfully after token refresh
                            new_location = await retry_resp.json()
                            # Request a refresh to update our local data
                            await self.async_request_refresh()
                            return True, new_location.get("id", "")
                    else:
                        # Token refresh failed
                        _LOGGER.error("Failed to create location: Token refresh failed")
                        return False, "Authentication failed and token refresh failed"
                        
                elif resp.status not in (200, 201):
                    response_text = await resp.text()
                    _LOGGER.error("Failed to create location - Status: %s, Response: %s, URL: %s", 
                              resp.status, response_text, url)
                    return False, f"HTTP {resp.status}: {response_text}"
                    
                # Location created successfully
                new_location = await resp.json()
                location_id = new_location.get("id", "")
                
                # Request a refresh to update our local data
                await self.async_request_refresh()
                
                _LOGGER.info("Successfully created location: %s (ID: %s)", name, location_id)
                return True, location_id
                
        except aiohttp.ClientResponseError as err:
            _LOGGER.error("Failed to create location: HTTP %s - %s - URL: %s", 
                        err.status, err.message, url)
            return False, f"HTTP {err.status}: {err.message}"
        except aiohttp.ClientError as err:
            status_code = getattr(getattr(err, 'request_info', None), 'status', 'unknown')
            _LOGGER.error("Failed to create location: %s - HTTP Status: %s - URL: %s", 
                        err, status_code, url)
            return False, f"Client error: {err}"
        except Exception as err:
            _LOGGER.error("Failed to create location (unexpected error): %s - URL: %s", err, url)
            return False, f"Unexpected error: {err}"
            
    async def create_item(self, data: dict) -> tuple[bool, str]:
        """Create a new item in Homebox.
        
        Args:
            data: Dictionary containing item data
            
        Returns:
            Tuple of (success, item_id or error message)
        """
        # Make sure token doesn't already start with "Bearer"
        token_value = self.token
        if token_value.startswith("Bearer "):
            token_value = token_value[7:]  # Remove "Bearer " prefix
            
        headers = {
            "Authorization": f"Bearer {token_value}",
            "Content-Type": "application/json"
        }
        
        url = f"{self.api_url}/api/v1/items"
        
        # Prepare the item data for API
        item_data = {
            "name": data.get(ATTR_ITEM_NAME, ""),
            "description": data.get(ATTR_ITEM_DESCRIPTION, ""),
            "locationId": data.get(ATTR_LOCATION_ID, ""),
        }
        
        # Add optional fields if provided
        if ATTR_ITEM_QUANTITY in data:
            item_data["quantity"] = data[ATTR_ITEM_QUANTITY]
        if ATTR_ITEM_ASSET_ID in data:
            item_data["assetId"] = data[ATTR_ITEM_ASSET_ID]
        if ATTR_ITEM_PURCHASE_PRICE in data:
            item_data["purchasePrice"] = data[ATTR_ITEM_PURCHASE_PRICE]
        if ATTR_ITEM_FIELDS in data and isinstance(data[ATTR_ITEM_FIELDS], dict):
            item_data["fields"] = data[ATTR_ITEM_FIELDS]
        if ATTR_ITEM_LABELS in data and isinstance(data[ATTR_ITEM_LABELS], list):
            item_data["labelIds"] = data[ATTR_ITEM_LABELS]
        
        try:
            # Show truncated token in logs
            truncated_token = self.token[:10] + "..." if self.token and len(self.token) > 13 else "[none]"
            _LOGGER.debug("Creating item, URL: %s with token: %s, data: %s", url, truncated_token, item_data)
            
            async with self.session.post(url, headers=headers, json=item_data) as resp:
                if resp.status == 401:
                    # Token might be expired, try to refresh it immediately
                    resp_text = await resp.text()
                    _LOGGER.warning("Authentication failed (401, response: %s), attempting to refresh token", resp_text)
                    token_refreshed = await self._refresh_token_now()
                    _LOGGER.debug("Token refresh result: %s", "Success" if token_refreshed else "Failed")
                    
                    if token_refreshed:
                        # Retry the request with the new token
                        token_value = self.token
                        if token_value.startswith("Bearer "):
                            token_value = token_value[7:]
                            
                        headers = {
                            "Authorization": f"Bearer {token_value}",
                            "Content-Type": "application/json"
                        }
                        
                        async with self.session.post(url, headers=headers, json=item_data) as retry_resp:
                            if retry_resp.status not in (200, 201):
                                response_text = await retry_resp.text()
                                _LOGGER.error("Failed to create item after token refresh - Status: %s, Response: %s", 
                                          retry_resp.status, response_text)
                                return False, f"HTTP {retry_resp.status}: {response_text}"
                                
                            # Item created successfully after token refresh
                            new_item = await retry_resp.json()
                            # Request a refresh to update our local data
                            await self.async_request_refresh()
                            return True, new_item.get("id", "")
                    else:
                        # Token refresh failed
                        _LOGGER.error("Failed to create item: Token refresh failed")
                        return False, "Authentication failed and token refresh failed"
                        
                elif resp.status not in (200, 201):
                    response_text = await resp.text()
                    _LOGGER.error("Failed to create item - Status: %s, Response: %s, URL: %s", 
                              resp.status, response_text, url)
                    return False, f"HTTP {resp.status}: {response_text}"
                    
                # Item created successfully
                new_item = await resp.json()
                item_id = new_item.get("id", "")
                
                # Request a refresh to update our local data
                await self.async_request_refresh()
                
                _LOGGER.info("Successfully created item: %s (ID: %s)", data.get(ATTR_ITEM_NAME, ""), item_id)
                return True, item_id
                
        except aiohttp.ClientResponseError as err:
            _LOGGER.error("Failed to create item: HTTP %s - %s - URL: %s", 
                        err.status, err.message, url)
            return False, f"HTTP {err.status}: {err.message}"
        except aiohttp.ClientError as err:
            status_code = getattr(getattr(err, 'request_info', None), 'status', 'unknown')
            _LOGGER.error("Failed to create item: %s - HTTP Status: %s - URL: %s", 
                        err, status_code, url)
            return False, f"Client error: {err}"
        except Exception as err:
            _LOGGER.error("Failed to create item (unexpected error): %s - URL: %s", err, url)
            return False, f"Unexpected error: {err}"