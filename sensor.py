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

from .const import DOMAIN, COORDINATOR

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
    await entity_manager.async_add_or_update_entities(coordinator, entry, async_add_entities)


class HomeboxEntityManager:
    """Class to manage Homebox entities and handle dynamic updates."""
    
    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize the entity manager."""
        self.hass = hass
        self._tracked_items = {}  # Dict to track item_id to entity
        
    async def async_add_or_update_entities(
        self, coordinator, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
    ) -> None:
        """Add new entities for items and update existing ones."""
        new_entities = []
        
        # Process each item from the coordinator
        for item_id, item in coordinator.items.items():
            # Skip if we're already tracking this item
            if item_id in self._tracked_items:
                continue
                
            # Create a new entity for this item
            entity = HomeboxItemSensor(coordinator, item_id, entry)
            new_entities.append(entity)
            self._tracked_items[item_id] = entity
        
        if new_entities:
            _LOGGER.info("Adding %d new Homebox item sensors", len(new_entities))
            async_add_entities(new_entities)
            
    def remove_entities(self, removed_ids: list) -> None:
        """Remove entities that no longer exist."""
        for item_id in removed_ids:
            if item_id in self._tracked_items:
                self._tracked_items.pop(item_id)
                _LOGGER.debug("Removed tracking for item %s", item_id)


class HomeboxItemSensor(CoordinatorEntity, SensorEntity):
    """Representation of a Homebox Item."""

    def __init__(self, coordinator, item_id, entry):
        """Initialize the sensor."""
        super().__init__(coordinator)
        self.item_id = item_id
        self.entry = entry
        
        # Get initial data
        item = self.coordinator.items[item_id]
        self._attr_name = item.get("name", f"Item {item_id}")
        self._attr_unique_id = f"{DOMAIN}_{entry.entry_id}_{item_id}"
        
        # Set the icon based on item type or default
        self._attr_icon = "mdi:package-variant-closed"
        
        # Set the state to the location name
        location_id = item.get("locationId")
        if location_id and location_id in self.coordinator.locations:
            self._attr_native_value = self.coordinator.locations[location_id].get("name", "Unknown")
        else:
            self._attr_native_value = "No Location"

    @property
    def device_info(self) -> DeviceInfo:
        """Return device information about this entity."""
        return DeviceInfo(
            identifiers={(DOMAIN, self.entry.entry_id)},
            name="Homebox",
            manufacturer="Homebox",
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the state attributes."""
        item = self.coordinator.items.get(self.item_id, {})
        
        # Get location info
        location_id = item.get("locationId")
        location_name = "Unknown"
        location_details = {}
        
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
        
        # Update the state to the location name
        location_id = item.get("locationId")
        if location_id and location_id in self.coordinator.locations:
            self._attr_native_value = self.coordinator.locations[location_id].get("name", "Unknown")
        else:
            self._attr_native_value = "No Location"
            
        self.async_write_ha_state()