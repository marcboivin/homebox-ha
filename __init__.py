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
from homeassistant.core import Event, HomeAssistant, ServiceCall, callback
from typing import Any
from homeassistant.helpers import entity_registry, area_registry, device_registry, selector, service
from homeassistant.helpers.typing import ConfigType
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.components import persistent_notification
from homeassistant.helpers.event import async_track_state_change_event

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
    SERVICE_FILL_ITEM,
    ATTR_ITEM_ID,
    ATTR_LOCATION_ID,
    ATTR_ITEM_NAME,
    ATTR_ITEM_DESCRIPTION,
    ATTR_ITEM_QUANTITY,
    ATTR_ITEM_ASSET_ID,
    ATTR_ITEM_PURCHASE_PRICE,
    ATTR_ITEM_FIELDS,
    ATTR_ITEM_LABELS,
    ATTR_COFFEE_VALUE,
    TOKEN_REFRESH_INTERVAL,
    EVENT_AREA_REGISTRY_UPDATED,
    sanitize_token,
    SPECIAL_FIELD_COFFEE,
    ENTITY_TYPE_CONTENT,
    CONTENT_PLATFORM,
)

_LOGGER = logging.getLogger(__name__)

CONFIG_SCHEMA = vol.Schema({DOMAIN: vol.Schema({})}, extra=vol.ALLOW_EXTRA)

PLATFORMS: list[str] = ["sensor"]

# Define base schemas (will be replaced with dynamic ones in setup_entry)
MOVE_ITEM_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_ITEM_ID): str,
        vol.Required(ATTR_LOCATION_ID): str,
    }
)


@callback
def _get_schema_with_location_selector(hass: HomeAssistant, entry_id: str) -> vol.Schema:
    """Get a schema with location selector populated with Homebox locations."""
    coordinator = hass.data[DOMAIN][entry_id][COORDINATOR]
    
    # Create location options for selector
    location_options = []
    
    for location_id, location in coordinator.locations.items():
        location_name = location.get("name", f"Location {location_id}")
        location_options.append(
            selector.SelectOptionDict(
                value=location_id,
                label=f"{location_name} (ID: {location_id})"
            )
        )
    
    # Sort by location name for better UX
    location_options.sort(key=lambda x: x["label"])
    
    # Create location selector
    location_selector = selector.SelectSelector(
        selector.SelectSelectorConfig(
            options=location_options,
            mode=selector.SelectSelectorMode.DROPDOWN,
            translation_key="location_id"
        )
    )
    
    # Create a schema with the location selector
    schema = vol.Schema(
        {
            vol.Required(ATTR_LOCATION_ID): location_selector,
        }
    )
    
    return schema


@callback
def _get_schema_with_item_selector(hass: HomeAssistant, entry_id: str) -> vol.Schema:
    """Get a schema with item selector populated with Homebox items."""
    coordinator = hass.data[DOMAIN][entry_id][COORDINATOR]
    
    # Create item options for selector
    item_options = []
    
    for item_id, item in coordinator.items.items():
        item_name = item.get("name", f"Item {item_id}")
        
        # Get location name if available
        location_name = "Unknown Location"
        location_id = item.get("locationId")
        if location_id and location_id in coordinator.locations:
            location_name = coordinator.locations[location_id].get("name", "Unknown Location")
        
        # Create a label with name, ID and location
        item_options.append(
            selector.SelectOptionDict(
                value=item_id,
                label=f"{item_name} (ID: {item_id}, Location: {location_name})"
            )
        )
    
    # Sort by item name for better UX
    item_options.sort(key=lambda x: x["label"])
    
    # Create item selector
    item_selector = selector.SelectSelector(
        selector.SelectSelectorConfig(
            options=item_options,
            mode=selector.SelectSelectorMode.DROPDOWN,
            translation_key="item_id"
        )
    )
    
    # Create a schema with the item selector
    schema = vol.Schema(
        {
            vol.Required(ATTR_ITEM_ID): item_selector,
        }
    )
    
    return schema


@callback
def _get_move_item_schema(hass: HomeAssistant, entry_id: str) -> vol.Schema:
    """Get a schema for the move_item service."""
    item_schema = _get_schema_with_item_selector(hass, entry_id)
    location_schema = _get_schema_with_location_selector(hass, entry_id)
    
    # Combine the schemas
    combined_schema = vol.Schema({**item_schema.schema, **location_schema.schema})
    return combined_schema


@callback
def _get_create_item_schema(hass: HomeAssistant, entry_id: str) -> vol.Schema:
    """Get a schema for the create_item service."""
    location_schema = _get_schema_with_location_selector(hass, entry_id)
    
    # Add the other fields to the schema
    schema = vol.Schema({
        vol.Required(ATTR_ITEM_NAME): str,
        **location_schema.schema,
        vol.Optional(ATTR_ITEM_DESCRIPTION): str,
        vol.Optional(ATTR_ITEM_QUANTITY): vol.Coerce(int),
        vol.Optional(ATTR_ITEM_ASSET_ID): str,
        vol.Optional(ATTR_ITEM_PURCHASE_PRICE): vol.Coerce(float),
        vol.Optional(ATTR_ITEM_FIELDS): dict,
        vol.Optional(ATTR_ITEM_LABELS): list,
    })
    
    return schema


@callback
def _async_register_services_with_selectors(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Register services with dynamic selectors."""
    entry_id = entry.entry_id
    coordinator = hass.data[DOMAIN][entry_id][COORDINATOR]
    
    # Register handle refresh callback - updates the service schemas after coordinator refresh
    @callback
    def _refresh_service_schemas(*_):
        """Refresh service schemas with updated data from coordinator."""
        _LOGGER.debug("Refreshing service schemas with updated location and item data")
        _async_register_services_with_selectors(hass, entry)
    
    # First time registration or internal update/refresh
    if hasattr(coordinator, "_service_refresh_remove_callable"):
        coordinator._service_refresh_remove_callable()
    
    # Store the remove callback function
    coordinator._service_refresh_remove_callable = coordinator.async_add_listener(_refresh_service_schemas)
    
    # Register/update the services
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
                # Find device and entities by the device identifier
                device_identifiers = {(DOMAIN, f"{entry_id}_{item_id}")}
                entity_id = None
                
                # Get device registry
                dr = device_registry.async_get(hass)
                
                # Find the entity by device identifier
                for entity in er.entities.values():
                    if entity.device_id:
                        # Get the device to check its identifiers
                        device = dr.async_get(entity.device_id)
                        if device and device_identifiers.issubset(device.identifiers):
                            entity_id = entity.entity_id
                            break
                
                if entity_id:
                    _LOGGER.info("Assigning entity %s to area %s (ID: %s)", 
                                entity_id, location_name, area_id)
                    
                    # Update the entity
                    er.async_update_entity(entity_id, area_id=area_id)
                    
                    # Also update the device
                    for device_id, device in dr.devices.items():
                        if device_identifiers.issubset(device.identifiers):
                            dr.async_update_device(device_id, area_id=area_id)
                            break
                    
                    area_assigned = True
                    notification_text += f"\n- Assigned to area: {location_name}"
            
            persistent_notification.create(
                hass,
                notification_text,
                title="Item Moved",
                notification_id=f"{DOMAIN}_item_moved"
            )
    
    # Get schema for move_item service
    move_item_schema = _get_move_item_schema(hass, entry_id)
    
    # Check if service is already registered and remove it
    if hass.services.has_service(DOMAIN, SERVICE_MOVE_ITEM):
        hass.services.async_remove(DOMAIN, SERVICE_MOVE_ITEM)
    
    # Register the service with the new schema
    hass.services.async_register(
        DOMAIN, SERVICE_MOVE_ITEM, handle_move_item, schema=move_item_schema
    )
    
    # Create Item service schema and registration handling
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
                    
                    # Get device registry
                    dr = device_registry.async_get(hass)
                    
                    # Look for entities with this device identifier pattern
                    device_identifiers = {(DOMAIN, f"{entry.entry_id}_{item_id_or_error}")}
                    entity_id = None
                    
                    # Find the entity by device identifier
                    for entity in er.entities.values():
                        if entity.device_id:
                            # Get the device to check its identifiers
                            device = dr.async_get(entity.device_id)
                            if device and device_identifiers.issubset(device.identifiers):
                                entity_id = entity.entity_id
                                break
                    
                    if entity_id:
                        _LOGGER.info("Assigning entity %s to area %s (ID: %s)", 
                                    entity_id, location_name, area_id)
                        
                        # Update the entity
                        er.async_update_entity(entity_id, area_id=area_id)
                        
                        # Also update the device
                        for device_id, device in dr.devices.items():
                            if device_identifiers.issubset(device.identifiers):
                                dr.async_update_device(device_id, area_id=area_id)
                                break
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
    
    # Get schema for create_item service
    create_item_schema = _get_create_item_schema(hass, entry_id)
    
    # Check if service is already registered and remove it
    if hass.services.has_service(DOMAIN, SERVICE_CREATE_ITEM):
        hass.services.async_remove(DOMAIN, SERVICE_CREATE_ITEM)
    
    # Register the service with the new schema
    hass.services.async_register(
        DOMAIN, SERVICE_CREATE_ITEM, handle_create_item, schema=create_item_schema
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
    
    # Register area registry change listener to sync area changes to Homebox
    @callback
    def _handle_area_registry_update(event: Event[Any]) -> None:
        """Handle area registry update events."""
        area_id = event.data.get("area_id")
        action = event.data.get("action")
        
        if not area_id:
            return
            
        # Get area information
        ar = area_registry.async_get(hass)
        area = ar.async_get_area(area_id)
        
        if not area:
            _LOGGER.debug("Area %s not found for %s action", area_id, action)
            return
            
        area_name = area.name
        _LOGGER.debug("Area registry update: %s (ID: %s) - Action: %s", 
                     area_name, area_id, action)
        
        if action == "update":
            # Update existing location in Homebox if we have a matching one
            exists, existing_id = coordinator.get_location_by_name(area_name)
            if exists:
                _LOGGER.info("Updating Homebox location to match renamed HA area: %s", area_name)
                
                # Update the location in Homebox
                hass.async_create_task(coordinator.update_location(
                    location_id=existing_id,
                    name=area_name,
                    description=f"Synchronized from Home Assistant area: {area_name}"
                ))
    
    # Register area registry update listener
    coordinator._area_registry_unsub = async_track_state_change_event(
        hass, EVENT_AREA_REGISTRY_UPDATED, _handle_area_registry_update
    )
    
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
                dr = device_registry.async_get(hass)
                
                # Find device and entities by the device identifier
                device_identifiers = {(DOMAIN, f"{entry.entry_id}_{item_id}")}
                entity_id = None
                
                # Find the entity by device identifier
                for entity in er.entities.values():
                    if entity.device_id:
                        # Get the device to check its identifiers
                        device = dr.async_get(entity.device_id)
                        if device and device_identifiers.issubset(device.identifiers):
                            entity_id = entity.entity_id
                            break
                
                if entity_id:
                    _LOGGER.info("Assigning entity %s to area %s (ID: %s)", 
                                entity_id, location_name, area_id)
                    
                    # Update the entity
                    er.async_update_entity(entity_id, area_id=area_id)
                    
                    # Also update the device
                    for device_id, device in dr.devices.items():
                        if device_identifiers.issubset(device.identifiers):
                            dr.async_update_device(device_id, area_id=area_id)
                            break
                    
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
                # Check auth method and log helpful information
                auth_method = coordinator._config_entry.data.get(CONF_AUTH_METHOD, "unknown")
                if auth_method == AUTH_METHOD_TOKEN:
                    _LOGGER.warning("Token refresh failed. Using existing token: %s (Auth method: TOKEN - cannot refresh via login)", truncated_token)
                    _LOGGER.info("To refresh tokens with TOKEN auth method, you need to manually update the token in the integration configuration")
                else:
                    _LOGGER.warning("Token refresh failed. Using existing token: %s (Auth method: %s)", truncated_token, auth_method)
                    
                # Log config entry data with sensitive info redacted
                data_keys = list(coordinator._config_entry.data.keys())
                _LOGGER.debug("Config entry data contains keys: %s", data_keys)
                
            # Create a persistent notification with all the logs
            log_text = "\n".join(handler.logs)
            
            # Add helpful information for users
            if not result:
                auth_method = coordinator._config_entry.data.get(CONF_AUTH_METHOD, "unknown")
                log_text += "\n\n--- Troubleshooting Tips ---\n"
                
                if auth_method == AUTH_METHOD_TOKEN:
                    log_text += (
                        "You are using API Token authentication. When using this method, "
                        "tokens cannot be automatically refreshed.\n\n"
                        "To fix this issue:\n"
                        "1. Get a new token from the Homebox interface\n"
                        "2. Update the integration by removing and re-adding it with the new token\n"
                    )
                else:
                    log_text += (
                        "You are using Username/Password authentication, but token refresh failed.\n\n"
                        "Possible reasons:\n"
                        "1. Your Homebox instance may not support token refresh\n"
                        "2. Username/Password is no longer valid\n"
                        "3. Your Homebox instance may be using a different API endpoint for refresh\n\n"
                        "To fix this issue:\n"
                        "1. Make sure your Homebox instance is running and accessible\n"
                        "2. Try removing and re-adding the integration with your current credentials\n"
                    )
            
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

    # Register token refresh service (this doesn't need selectors)
    hass.services.async_register(
        DOMAIN, SERVICE_REFRESH_TOKEN, handle_refresh_token
    )
    
    # Register services with selectors
    _async_register_services_with_selectors(hass, entry)
    
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
    # This doesn't need selectors but should be registered after initial setup
    if hass.services.has_service(DOMAIN, SERVICE_SYNC_AREAS):
        hass.services.async_remove(DOMAIN, SERVICE_SYNC_AREAS)
        
    hass.services.async_register(
        DOMAIN, SERVICE_SYNC_AREAS, handle_sync_areas
    )
    
    # Service to fill item with coffee field
    async def handle_fill_item(call: ServiceCall) -> None:
        """Handle setting the Coffee field for an item."""
        item_id = call.data.get(ATTR_ITEM_ID)
        coffee_value = call.data.get(ATTR_COFFEE_VALUE)
        
        if not item_id:
            _LOGGER.error("Item ID is required")
            persistent_notification.create(
                hass,
                "Item ID is required for fill_item service.",
                title="Item Fill Failed",
                notification_id=f"{DOMAIN}_item_fill_failed"
            )
            return
            
        if not coffee_value:
            _LOGGER.error("Coffee value is required")
            persistent_notification.create(
                hass,
                "Coffee value is required for fill_item service.",
                title="Item Fill Failed",
                notification_id=f"{DOMAIN}_item_fill_failed"
            )
            return
            
        # Ensure the item exists
        if item_id not in coordinator.items:
            _LOGGER.error("Item with ID %s not found", item_id)
            persistent_notification.create(
                hass,
                f"Item with ID {item_id} not found.",
                title="Item Fill Failed",
                notification_id=f"{DOMAIN}_item_fill_failed"
            )
            return
            
        # Set the coffee field value
        result, message = await coordinator.set_item_coffee_field(item_id, coffee_value)
        
        if result:
            # Success notification
            item_name = coordinator.items.get(item_id, {}).get("name", f"Item {item_id}")
            notification_text = f"Successfully set Coffee field for:\n- Item: {item_name}\n- Value: {coffee_value}"
            persistent_notification.create(
                hass,
                notification_text,
                title="Coffee Field Updated",
                notification_id=f"{DOMAIN}_item_filled"
            )
        else:
            # Failure notification
            _LOGGER.error("Failed to set coffee field: %s", message)
            persistent_notification.create(
                hass,
                f"Failed to set Coffee field: {message}",
                title="Coffee Field Update Failed",
                notification_id=f"{DOMAIN}_item_fill_failed"
            )
    
    # Register the fill item service
    if hass.services.has_service(DOMAIN, SERVICE_FILL_ITEM):
        hass.services.async_remove(DOMAIN, SERVICE_FILL_ITEM)
        
    # Get schema for fill_item service with item selector
    fill_item_schema = _get_schema_with_item_selector(hass, entry.entry_id)
    fill_item_schema = fill_item_schema.extend({
        vol.Required(ATTR_COFFEE_VALUE): str,
    })
    
    hass.services.async_register(
        DOMAIN, SERVICE_FILL_ITEM, handle_fill_item, schema=fill_item_schema
    )
    
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    # Cancel token refresh task if it exists
    coordinator = hass.data[DOMAIN][entry.entry_id].get(COORDINATOR)
    if coordinator:
        # Cancel token refresh task
        if hasattr(coordinator, "_token_refresh_task") and coordinator._token_refresh_task:
            coordinator._token_refresh_task.cancel()
        
        # Remove service refresh listener
        if hasattr(coordinator, "_service_refresh_remove_callable") and coordinator._service_refresh_remove_callable:
            coordinator._service_refresh_remove_callable()
            
        # Remove area registry listener
        if hasattr(coordinator, "_area_registry_unsub") and coordinator._area_registry_unsub:
            coordinator._area_registry_unsub()
    
    # Unload platforms
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    
    if unload_ok:
        # Clean up services if this is the last instance
        if len(hass.data[DOMAIN]) == 1:
            for service_name in [SERVICE_MOVE_ITEM, SERVICE_CREATE_ITEM, SERVICE_REFRESH_TOKEN, SERVICE_SYNC_AREAS, SERVICE_FILL_ITEM]:
                if hass.services.has_service(DOMAIN, service_name):
                    hass.services.async_remove(DOMAIN, service_name)
        
        # Remove this entry's data
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
            
    async def update_location(self, location_id: str, name: str, description: str = "") -> bool:
        """Update a location in Homebox.
        
        Args:
            location_id: ID of the location to update
            name: New name for the location
            description: Optional description for the location
            
        Returns:
            Boolean indicating success or failure
        """
        # Get authentication headers
        headers = self._get_auth_headers({"Content-Type": "application/json"})
        
        url = f"{self.api_url}/api/v1/locations/{location_id}"
        
        # Prepare the location data for API
        location_data = {
            "name": name,
            "description": description
        }
        
        try:
            # Show truncated token in logs
            truncated_token = self.token[:10] + "..." if self.token and len(self.token) > 13 else "[none]"
            _LOGGER.debug("Updating location, URL: %s with token: %s, data: %s", url, truncated_token, location_data)
            
            async with self.session.put(url, headers=headers, json=location_data) as resp:
                if resp.status == 401:
                    # Token might be expired, try to refresh it immediately
                    resp_text = await resp.text()
                    _LOGGER.warning("Authentication failed (401, response: %s), attempting to refresh token", resp_text)
                    token_refreshed = await self._refresh_token_now()
                    _LOGGER.debug("Token refresh result: %s", "Success" if token_refreshed else "Failed")
                    
                    if token_refreshed:
                        # Retry the request with the new token
                        headers = self._get_auth_headers({"Content-Type": "application/json"})
                        
                        async with self.session.put(url, headers=headers, json=location_data) as retry_resp:
                            if retry_resp.status != 200:
                                response_text = await retry_resp.text()
                                _LOGGER.error("Failed to update location after token refresh - Status: %s, Response: %s", 
                                          retry_resp.status, response_text)
                                return False
                                
                            # Location updated successfully after token refresh
                            # Request a refresh to update our local data
                            await self.async_request_refresh()
                            _LOGGER.info("Successfully updated location: %s (ID: %s)", name, location_id)
                            return True
                    else:
                        # Token refresh failed
                        _LOGGER.error("Failed to update location: Token refresh failed")
                        return False
                        
                elif resp.status != 200:
                    response_text = await resp.text()
                    _LOGGER.error("Failed to update location - Status: %s, Response: %s, URL: %s", 
                              resp.status, response_text, url)
                    return False
                    
                # Location updated successfully
                # Request a refresh to update our local data
                await self.async_request_refresh()
                
                _LOGGER.info("Successfully updated location: %s (ID: %s)", name, location_id)
                return True
                
        except aiohttp.ClientResponseError as err:
            _LOGGER.error("Failed to update location: HTTP %s - %s - URL: %s", 
                        err.status, err.message, url)
            return False
        except aiohttp.ClientError as err:
            status_code = getattr(getattr(err, 'request_info', None), 'status', 'unknown')
            _LOGGER.error("Failed to update location: %s - HTTP Status: %s - URL: %s", 
                        err, status_code, url)
            return False
        except Exception as err:
            _LOGGER.error("Failed to update location (unexpected error): %s - URL: %s", err, url)
            return False

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
        
        # Store the token, ensuring it's properly sanitized
        self.token = self._sanitize_token(token)
        
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
        
    def _sanitize_token(self, token: str) -> str:
        """Remove 'Bearer ' prefix from token if present."""
        return sanitize_token(token)
        
    def _get_auth_headers(self, additional_headers: dict = None) -> dict:
        """Get authentication headers with bearer token.
        
        Args:
            additional_headers: Optional additional headers to include
            
        Returns:
            Dict with Authorization header and any additional headers
        """
        headers = {"Authorization": f"Bearer {self.token}"}
        
        if additional_headers:
            headers.update(additional_headers)
            
        return headers

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
                                # Process location information
                                # Some versions of Homebox include a nested location object instead of just locationId
                                if "location" in item and isinstance(item["location"], dict) and "id" in item["location"]:
                                    location_obj = item["location"]
                                    # Extract location ID and ensure locationId is set for compatibility
                                    item["locationId"] = location_obj["id"]
                                    
                                    # Make sure the location is also in our locations dictionary
                                    if location_obj["id"] not in self.locations:
                                        self.locations[location_obj["id"]] = location_obj
                                        _LOGGER.debug("Added location from item data: %s", location_obj["name"])
                                
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
                    # Try to refresh the token
                    try:
                        # Show a truncated version of the token for debugging
                        truncated_token = self.token[:10] + "..." if self.token and len(self.token) > 13 else "[none]"
                        _LOGGER.debug("Refreshing Homebox API token [Current: %s] for API URL: %s", 
                                     truncated_token, self.api_url)
                        
                        # Instead of duplicating the logic, use our existing refresh method
                        refresh_result = await self._refresh_token_now()
                        
                        if refresh_result:
                            _LOGGER.debug("Periodic token refresh successful")
                            continue
                        else:
                            _LOGGER.warning("Periodic token refresh failed, will try again later")
                    
                    except Exception as err:
                        _LOGGER.error("Error refreshing token: %s", err)
        
        except asyncio.CancelledError:
            # Task was cancelled, clean up
            _LOGGER.debug("Token refresh task cancelled")
        except Exception as err:
            _LOGGER.error("Unexpected error in token refresh task: %s", err)

    async def _fetch_locations(self) -> list:
        """Fetch locations from the API."""
        headers = self._get_auth_headers()
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
                    headers = self._get_auth_headers()
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
            
            # Get authentication headers
            headers = self._get_auth_headers()
            
            async with self.session.get(refresh_url, headers=headers) as resp:
                resp_status = resp.status
                try:
                    resp_text = await resp.text()
                    _LOGGER.debug("Token refresh response: Status: %s, Body: %s", resp_status, resp_text)
                    
                    if resp_status == 200:
                        try:
                            data = await resp.json()
                            if "token" in data:
                                self.token = data["token"]
                                new_truncated = self.token[:10] + "..." if self.token and len(self.token) > 13 else "[none]"
                                _LOGGER.debug("Successfully refreshed API token: %s → %s",
                                            truncated_token, new_truncated)
                                self._last_token_refresh = datetime.now()
                                return True
                            else:
                                _LOGGER.warning("Token refresh response did not contain a token field: %s", data)
                        except ValueError as json_err:
                            _LOGGER.warning("Failed to parse token refresh response as JSON: %s", json_err)
                    else:
                        _LOGGER.warning("Token refresh failed with status code %s: %s", resp_status, resp_text)
                except Exception as text_err:
                    _LOGGER.warning("Error getting response text: %s", text_err)
                
                # If refresh token failed and we have login credentials, try to re-login
                if self._config_entry and self._config_entry.data.get(CONF_AUTH_METHOD) == AUTH_METHOD_LOGIN:
                    username = self._config_entry.data.get(CONF_USERNAME)
                    # Try to get password from either data or options
                    password = None
                    if CONF_PASSWORD in self._config_entry.data:
                        password = self._config_entry.data.get(CONF_PASSWORD)
                    elif hasattr(self._config_entry, 'options') and CONF_PASSWORD in self._config_entry.options:
                        password = self._config_entry.options.get(CONF_PASSWORD)
                    
                    # Log detailed information
                    if not username:
                        _LOGGER.warning("Cannot refresh token via login: Username is missing")
                        _LOGGER.debug("Available config data keys: %s", list(self._config_entry.data.keys()))
                    elif not password:
                        _LOGGER.warning("Cannot refresh token via login: Password is missing from both data and options")
                        _LOGGER.debug("Available data keys: %s", list(self._config_entry.data.keys()))
                        if hasattr(self._config_entry, 'options'):
                            _LOGGER.debug("Available options keys: %s", list(self._config_entry.options.keys()))
                    else:
                        _LOGGER.debug("Attempting to get new token via login with username: %s", username)
                        try:
                            from .config_flow import get_token_from_login
                            new_token = await get_token_from_login(
                                self.session,
                                f"{self.api_url}/api/v1",
                                username,
                                password
                            )
                            if new_token:
                                self.token = new_token
                                new_truncated = self.token[:10] + "..." if self.token and len(self.token) > 13 else "[none]"
                                _LOGGER.debug("Successfully obtained new token through login: %s → %s", 
                                            truncated_token, new_truncated)
                                self._last_token_refresh = datetime.now()
                                return True
                            else:
                                _LOGGER.warning("Failed to get new token via login: No token returned")
                        except Exception as login_err:
                            _LOGGER.warning("Error refreshing token via login: %s", login_err)
            
            _LOGGER.debug("Token refresh failed via both refresh endpoint and login")
            return False
        except Exception as err:
            _LOGGER.error("Error during immediate token refresh: %s", err)
            return False
    
    async def _fetch_items(self) -> list:
        """Fetch items from the API."""
        headers = self._get_auth_headers()
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
                    headers = self._get_auth_headers()
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
        
        # Get authentication headers
        headers = self._get_auth_headers({"Content-Type": "application/json"})
        
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
                        headers = self._get_auth_headers({"Content-Type": "application/json"})
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
        # Get authentication headers
        headers = self._get_auth_headers({"Content-Type": "application/json"})
        
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
                        headers = self._get_auth_headers({"Content-Type": "application/json"})
                        
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
        # Get authentication headers
        headers = self._get_auth_headers({"Content-Type": "application/json"})
        
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
                        headers = self._get_auth_headers({"Content-Type": "application/json"})
                        
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
    
    async def set_item_coffee_field(self, item_id: str, coffee_value: str) -> tuple[bool, str]:
        """Set the Coffee field for an item.
        
        Args:
            item_id: ID of the item to update
            coffee_value: Value to set for the Coffee field
            
        Returns:
            Tuple of (success, message)
        """
        # Get authentication headers
        headers = self._get_auth_headers({"Content-Type": "application/json"})
        
        # Endpoint for setting a custom field
        url = f"{self.api_url}/api/v1/items/{item_id}/fields"
        
        # Prepare the field data
        field_data = {
            "name": SPECIAL_FIELD_COFFEE,
            "type": "text",
            "value": coffee_value
        }
        
        try:
            # Check if the item exists
            if item_id not in self.items:
                _LOGGER.error("Item with ID %s not found", item_id)
                return False, f"Item with ID {item_id} not found"
                
            # Show truncated token in logs
            truncated_token = self.token[:10] + "..." if self.token and len(self.token) > 13 else "[none]"
            _LOGGER.debug("Setting coffee field, URL: %s with token: %s, data: %s", url, truncated_token, field_data)
            
            # Check if the field already exists
            existing_field_id = None
            item = self.items[item_id]
            fields = item.get("fields", {})
            
            if SPECIAL_FIELD_COFFEE in fields:
                # Field exists, need to update it
                _LOGGER.debug("Coffee field already exists for item %s, will update existing field", item_id)
                
                # Get all fields to find the field ID for the coffee field
                fields_url = f"{self.api_url}/api/v1/items/{item_id}/fields"
                async with self.session.get(fields_url, headers=headers) as fields_resp:
                    if fields_resp.status == 200:
                        fields_data = await fields_resp.json()
                        
                        # Check response format - either a list or an object with a fields property
                        if isinstance(fields_data, list):
                            all_fields = fields_data
                        elif isinstance(fields_data, dict) and "fields" in fields_data:
                            all_fields = fields_data["fields"]
                        else:
                            all_fields = []
                        
                        # Find the coffee field
                        for field in all_fields:
                            if isinstance(field, dict) and field.get("name") == SPECIAL_FIELD_COFFEE:
                                existing_field_id = field.get("id")
                                break
                
                if existing_field_id:
                    # Update the existing field
                    update_url = f"{self.api_url}/api/v1/items/{item_id}/fields/{existing_field_id}"
                    async with self.session.put(update_url, headers=headers, json=field_data) as resp:
                        if resp.status == 401:
                            # Token might be expired, try to refresh it immediately
                            resp_text = await resp.text()
                            _LOGGER.warning("Authentication failed (401, response: %s), attempting to refresh token", resp_text)
                            token_refreshed = await self._refresh_token_now()
                            
                            if token_refreshed:
                                # Retry the request with the new token
                                headers = self._get_auth_headers({"Content-Type": "application/json"})
                                
                                async with self.session.put(update_url, headers=headers, json=field_data) as retry_resp:
                                    if retry_resp.status != 200:
                                        response_text = await retry_resp.text()
                                        _LOGGER.error("Failed to update coffee field after token refresh - Status: %s, Response: %s", 
                                                  retry_resp.status, response_text)
                                        return False, f"HTTP {retry_resp.status}: {response_text}"
                                    
                                    # Field updated successfully after token refresh
                                    result = await retry_resp.json()
                                    await self.async_request_refresh()
                                    _LOGGER.info("Successfully updated coffee field for item %s", item_id)
                                    return True, "Coffee field updated successfully"
                            else:
                                # Token refresh failed
                                _LOGGER.error("Failed to update coffee field: Token refresh failed")
                                return False, "Authentication failed and token refresh failed"
                        
                        elif resp.status != 200:
                            response_text = await resp.text()
                            _LOGGER.error("Failed to update coffee field - Status: %s, Response: %s", 
                                      resp.status, response_text)
                            return False, f"HTTP {resp.status}: {response_text}"
                        
                        # Field updated successfully
                        result = await resp.json()
                        await self.async_request_refresh()
                        _LOGGER.info("Successfully updated coffee field for item %s", item_id)
                        return True, "Coffee field updated successfully"
                else:
                    # Couldn't find the field ID, create a new field
                    _LOGGER.debug("Coffee field exists in item data but couldn't find field ID, creating new field")
            
            # Create a new field
            async with self.session.post(url, headers=headers, json=field_data) as resp:
                if resp.status == 401:
                    # Token might be expired, try to refresh it immediately
                    resp_text = await resp.text()
                    _LOGGER.warning("Authentication failed (401, response: %s), attempting to refresh token", resp_text)
                    token_refreshed = await self._refresh_token_now()
                    
                    if token_refreshed:
                        # Retry the request with the new token
                        headers = self._get_auth_headers({"Content-Type": "application/json"})
                        
                        async with self.session.post(url, headers=headers, json=field_data) as retry_resp:
                            if retry_resp.status not in (200, 201):
                                response_text = await retry_resp.text()
                                _LOGGER.error("Failed to create coffee field after token refresh - Status: %s, Response: %s", 
                                          retry_resp.status, response_text)
                                return False, f"HTTP {retry_resp.status}: {response_text}"
                            
                            # Field created successfully after token refresh
                            result = await retry_resp.json()
                            await self.async_request_refresh()
                            _LOGGER.info("Successfully created coffee field for item %s", item_id)
                            return True, "Coffee field created successfully"
                    else:
                        # Token refresh failed
                        _LOGGER.error("Failed to create coffee field: Token refresh failed")
                        return False, "Authentication failed and token refresh failed"
                
                elif resp.status not in (200, 201):
                    response_text = await resp.text()
                    _LOGGER.error("Failed to create coffee field - Status: %s, Response: %s", 
                              resp.status, response_text)
                    return False, f"HTTP {resp.status}: {response_text}"
                
                # Field created successfully
                result = await resp.json()
                await self.async_request_refresh()
                _LOGGER.info("Successfully created coffee field for item %s", item_id)
                return True, "Coffee field created successfully"
                
        except aiohttp.ClientResponseError as err:
            _LOGGER.error("Failed to set coffee field: HTTP %s - %s - URL: %s", 
                        err.status, err.message, url)
            return False, f"HTTP {err.status}: {err.message}"
        except aiohttp.ClientError as err:
            status_code = getattr(getattr(err, 'request_info', None), 'status', 'unknown')
            _LOGGER.error("Failed to set coffee field: %s - HTTP Status: %s - URL: %s", 
                        err, status_code, url)
            return False, f"Client error: {err}"
        except Exception as err:
            _LOGGER.error("Failed to set coffee field (unexpected error): %s - URL: %s", err, url)
            return False, f"Unexpected error: {err}"