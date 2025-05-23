"""Sensor platform for Homebox integration."""
from __future__ import annotations

import logging
from typing import Any, cast

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.helpers import area_registry, entity_registry, device_registry

from .const import (
    DOMAIN,
    COORDINATOR,
    SPECIAL_FIELD_COFFEE,
    ENTITY_TYPE_CONTENT,
    CONTENT_PLATFORM
)

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up Homebox sensors based on a config entry."""
    coordinator = hass.data[DOMAIN][entry.entry_id][COORDINATOR]
    
    # Store the async_add_entities function for future dynamically added entities
    coordinator._entity_adder = async_add_entities
    
    # Set up entity manager to track existing entities
    if not hasattr(hass.data[DOMAIN], "entity_manager"):
        hass.data[DOMAIN]["entity_manager"] = HomeboxEntityManager(hass)
    
    entity_manager = hass.data[DOMAIN]["entity_manager"]
    
    # Add an entity for each item
    await entity_manager.async_add_or_update_entities(coordinator, entry, async_add_entities, hass)


class HomeboxEntityManager:
    """Class to manage Homebox entities and handle dynamic updates."""
    
    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize the entity manager."""
        self.hass = hass
        self._tracked_items = {}  # Dict to track item_id to entity
        self._tracked_content_entities = {}  # Dict to track content entities by item_id
        
    async def async_add_or_update_entities(
        self, coordinator, entry: ConfigEntry, async_add_entities: AddEntitiesCallback, hass: HomeAssistant
    ) -> None:
        """Add new entities for items and update existing ones."""
        new_entities = []
        new_content_entities = []
        
        # Get area registry
        ar = area_registry.async_get(hass)
        er = entity_registry.async_get(hass)
        
        # Collect all Home Assistant area names and normalize them
        ha_areas = {area.name.lower(): area.id for area in ar.async_list_areas()}
        
        # Process each item from the coordinator
        for item_id, item in coordinator.items.items():
            # Skip if we're already tracking this item
            if item_id not in self._tracked_items:
                # Create a new entity for this item
                entity = HomeboxItemSensor(coordinator, item_id, entry)
                new_entities.append(entity)
                self._tracked_items[item_id] = entity
                
                # Check for special fields like Coffee
                self._process_special_fields(coordinator, item_id, item, entry, new_content_entities)
            else:
                # Check if we need to add/update any special field entities
                self._process_special_fields(coordinator, item_id, item, entry, new_content_entities)
        
        if new_entities:
            _LOGGER.info("Adding %d new Homebox item sensors", len(new_entities))
            async_add_entities(new_entities)
            
            # Now that entities have been added and have entity_ids, 
            # associate them with areas if applicable
            for entity in new_entities:
                # Try to match the location with a Home Assistant area
                location_id = coordinator.items.get(entity.item_id, {}).get("locationId")
                if location_id and location_id in coordinator.locations:
                    location_name = coordinator.locations[location_id].get("name", "")
                    
                    # Look for a matching area (case-insensitive)
                    if location_name.lower() in ha_areas:
                        area_id = ha_areas[location_name.lower()]
                        _LOGGER.debug("Matching location '%s' with HA area '%s' (ID: %s)", 
                                     location_name, location_name, area_id)
                        
                        # If entity has been registered, update its area
                        if hasattr(entity, "entity_id"):
                            # Also get the device and assign it to the same area
                            dr = device_registry.async_get(hass)
                            device_identifiers = {(DOMAIN, f"{entry.entry_id}_{entity.item_id}")}
                            
                            # First update the entity
                            er.async_update_entity(entity.entity_id, area_id=area_id)
                            
                            # Then find and update the device
                            for device_id, device in dr.devices.items():
                                if device_identifiers.issubset(device.identifiers):
                                    dr.async_update_device(device_id, area_id=area_id)
                                    break
                                    
                            _LOGGER.info("Assigned entity %s and device to area %s", entity.entity_id, location_name)
        
        # Add any new content entities
        if new_content_entities:
            _LOGGER.info("Adding %d new Homebox content sensors", len(new_content_entities))
            async_add_entities(new_content_entities)
            
    def _process_special_fields(self, coordinator, item_id, item, entry, new_content_entities):
        """Process special fields like Coffee and create content entities."""
        # Check if this item has custom fields
        if "fields" in item and isinstance(item["fields"], dict):
            fields = item["fields"]
            
            # Check for Coffee field
            if SPECIAL_FIELD_COFFEE in fields:
                # If we're not already tracking a content entity for this item and field
                content_key = f"{item_id}_{SPECIAL_FIELD_COFFEE}"
                if content_key not in self._tracked_content_entities:
                    # Create a new content entity
                    entity = HomeboxContentSensor(
                        coordinator=coordinator,
                        item_id=item_id,
                        entry=entry,
                        field_name=SPECIAL_FIELD_COFFEE,
                        entity_type=ENTITY_TYPE_CONTENT
                    )
                    new_content_entities.append(entity)
                    self._tracked_content_entities[content_key] = entity
                    _LOGGER.debug("Created new content entity for item %s with Coffee field", item_id)
            
    def remove_entities(self, removed_ids: list) -> None:
        """Remove entities that no longer exist."""
        for item_id in removed_ids:
            if item_id in self._tracked_items:
                self._tracked_items.pop(item_id)
                _LOGGER.debug("Removed tracking for item %s", item_id)
                
            # Also check for any content entities for this item
            to_remove = []
            for content_key in self._tracked_content_entities:
                if content_key.startswith(f"{item_id}_"):
                    to_remove.append(content_key)
                    
            for key in to_remove:
                self._tracked_content_entities.pop(key, None)
                _LOGGER.debug("Removed tracking for content entity %s", key)


class HomeboxItemSensor(CoordinatorEntity, SensorEntity):
    """Representation of a Homebox Item."""

    def __init__(self, coordinator, item_id, entry):
        """Initialize the sensor."""
        super().__init__(coordinator)
        self.item_id = item_id
        self.entry = entry
        self.hass = coordinator.hass
        
        # Get initial data
        item = self.coordinator.items[item_id]
        self._attr_name = item.get("name", f"Item {item_id}")
        self._attr_unique_id = f"{DOMAIN}_{entry.entry_id}_{item_id}"
        
        # Set the icon based on item type or default
        self._attr_icon = "mdi:package-variant-closed"
        
        # Set the state to the location name
        location_id = None
        location_name = "No Location"
        
        # First check for nested location object
        if "location" in item and isinstance(item["location"], dict) and "id" in item["location"]:
            location_obj = item["location"]
            location_id = location_obj["id"]
            location_name = location_obj.get("name", "Unknown")
        elif "locationId" in item:
            # Use the locationId reference if no nested object
            location_id = item.get("locationId")
            if location_id and location_id in self.coordinator.locations:
                location_name = self.coordinator.locations[location_id].get("name", "Unknown")
                
        # Set the state and store the location ID for change detection
        if location_id:
            self._attr_native_value = location_name
            self._prev_location_id = location_id
        else:
            self._attr_native_value = "No Location"
            self._prev_location_id = None

    @property
    def device_info(self) -> DeviceInfo:
        """Return device information about this entity."""
        item = self.coordinator.items.get(self.item_id, {})
        item_name = item.get("name", f"Item {self.item_id}")
        
        # Use a separate device identifier for each item
        return DeviceInfo(
            identifiers={(DOMAIN, f"{self.entry.entry_id}_{self.item_id}")},
            name=item_name,
            manufacturer="Homebox",
            model=item.get("description", "Homebox Item"),
            sw_version=item.get("updatedAt", ""),
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the state attributes."""
        item = self.coordinator.items.get(self.item_id, {})
        
        # Get location info
        location_id = None
        location_name = "Unknown"
        location_details = {}
        
        # First check for nested location object
        if "location" in item and isinstance(item["location"], dict) and "id" in item["location"]:
            location_obj = item["location"]
            location_id = location_obj["id"]
            location_name = location_obj.get("name", "Unknown")
            
            # Add detailed location information from the nested object
            location_details = {
                "id": location_id,
                "name": location_name,
                "description": location_obj.get("description", ""),
                "parent_id": location_obj.get("parentId"),
                "path": location_obj.get("path", ""),
                "type": location_obj.get("type", ""),
            }
        else:
            # Use the locationId reference if no nested object
            location_id = item.get("locationId")
            if location_id and location_id in self.coordinator.locations:
                location = self.coordinator.locations[location_id]
                location_name = location.get("name", "Unknown")
                
                # Add more detailed location information
                location_details = {
                    "id": location_id,
                    "name": location_name,
                    "description": location.get("description", ""),
                    "parent_id": location.get("parentId"),
                    "path": location.get("path", ""),
                    "type": location.get("type", ""),
                }
        
        # Get label information
        label_ids = item.get("labelIds", [])
        label_details = []
        
        # Get linked item information if available
        linked_item_ids = item.get("linkedItemIds", [])
        linked_items = []
        
        if linked_item_ids and isinstance(linked_item_ids, list):
            for linked_id in linked_item_ids:
                if linked_id in self.coordinator.items:
                    linked_item = self.coordinator.items[linked_id]
                    linked_items.append({
                        "id": linked_id,
                        "name": linked_item.get("name", "Unknown"),
                        "description": linked_item.get("description", ""),
                    })
        
        # Combine all attributes
        return {
            "id": self.item_id,
            "name": item.get("name", "Unknown"),
            "description": item.get("description", ""),
            "location_id": location_id,
            "location_name": location_name,
            "location": location_details,
            "labels": label_ids,
            "fields": item.get("fields", {}),
            "linked_items": linked_items,
            "created_at": item.get("createdAt", ""),
            "updated_at": item.get("updatedAt", ""),
        }
        
    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        if self.item_id not in self.coordinator.items:
            return
            
        item = self.coordinator.items[self.item_id]
        
        # Get the previous location before updating
        previous_location_id = None
        if hasattr(self, '_prev_location_id'):
            previous_location_id = self._prev_location_id
            
        # Get the current location
        location_id = None
        location_name = "No Location"
        
        # First check for nested location object
        if "location" in item and isinstance(item["location"], dict) and "id" in item["location"]:
            location_obj = item["location"]
            location_id = location_obj["id"]
            location_name = location_obj.get("name", "Unknown")
        elif "locationId" in item:
            # Use the locationId reference if no nested object
            location_id = item.get("locationId")
            if location_id and location_id in self.coordinator.locations:
                location_name = self.coordinator.locations[location_id].get("name", "Unknown")
        
        # Update the state to the location name
        if location_id:
            self._attr_native_value = location_name
        else:
            self._attr_native_value = "No Location"
        
        # If location has changed, check if we should assign to a Home Assistant area
        if location_id and location_id != previous_location_id and hasattr(self, 'entity_id'):
            # Try to match with Home Assistant area - we already got the location_name above
            
            if location_name:
                # Get the area registry
                from homeassistant.helpers import area_registry, entity_registry
                ar = area_registry.async_get(self.hass)
                er = entity_registry.async_get(self.hass)
                
                # Find area with matching name (case insensitive)
                ha_areas = {area.name.lower(): area.id for area in ar.async_list_areas()}
                if location_name.lower() in ha_areas:
                    area_id = ha_areas[location_name.lower()]
                    
                    # Assign entity to this area
                    er.async_update_entity(self.entity_id, area_id=area_id)
                    _LOGGER.debug("Updated entity %s area to match new location: %s", 
                                self.entity_id, location_name)
        
        # Store current location for future comparison
        self._prev_location_id = location_id
            
        self.async_write_ha_state()


class HomeboxContentSensor(CoordinatorEntity, SensorEntity):
    """Representation of a Homebox Content sensor based on a special field."""

    def __init__(self, coordinator, item_id, entry, field_name, entity_type):
        """Initialize the content sensor."""
        super().__init__(coordinator)
        self.item_id = item_id
        self.entry = entry
        self.hass = coordinator.hass
        self.field_name = field_name
        self.entity_type = entity_type
        
        # Get initial data
        item = self.coordinator.items[item_id]
        item_name = item.get("name", f"Item {item_id}")
        
        # Set entity properties
        self._attr_name = f"{item_name} {field_name.capitalize()}"
        self._attr_unique_id = f"{DOMAIN}_{entry.entry_id}_{item_id}_{field_name}_{entity_type}"
        
        # Set icon based on field type
        if field_name == SPECIAL_FIELD_COFFEE:
            self._attr_icon = "mdi:coffee"
        else:
            self._attr_icon = "mdi:counter"
        
        # Set initial value
        if "fields" in item and isinstance(item["fields"], dict) and field_name in item["fields"]:
            self._attr_native_value = item["fields"][field_name]
        else:
            self._attr_native_value = "Unknown"

    @property
    def device_info(self) -> DeviceInfo:
        """Return device information about this entity."""
        item = self.coordinator.items.get(self.item_id, {})
        item_name = item.get("name", f"Item {self.item_id}")
        
        # Use the same device identifier as the parent item entity
        return DeviceInfo(
            identifiers={(DOMAIN, f"{self.entry.entry_id}_{self.item_id}")},
            name=item_name,
            manufacturer="Homebox",
            model=item.get("description", "Homebox Item"),
            sw_version=item.get("updatedAt", ""),
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the state attributes."""
        item = self.coordinator.items.get(self.item_id, {})
        
        # Return item details that are relevant for content
        return {
            "item_id": self.item_id,
            "item_name": item.get("name", "Unknown"),
            "field_name": self.field_name,
            "entity_type": self.entity_type,
            "updated_at": item.get("updatedAt", ""),
        }
        
    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        if self.item_id not in self.coordinator.items:
            return
            
        item = self.coordinator.items[self.item_id]
        
        # Update the state from the field value
        if "fields" in item and isinstance(item["fields"], dict) and self.field_name in item["fields"]:
            self._attr_native_value = item["fields"][self.field_name]
        else:
            self._attr_native_value = "Unknown"
            
        self.async_write_ha_state()