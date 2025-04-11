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

    # Add an entity for each item
    entities = []
    for item_id, item in coordinator.items.items():
        entities.append(HomeboxItemSensor(coordinator, item_id, entry))

    async_add_entities(entities)


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
        if location_id and location_id in self.coordinator.locations:
            location_name = self.coordinator.locations[location_id].get("name", "Unknown")
            
        return {
            "id": self.item_id,
            "name": item.get("name", "Unknown"),
            "description": item.get("description", ""),
            "location_id": location_id,
            "location_name": location_name,
            "labels": item.get("labelIds", []),
            "fields": item.get("fields", {}),
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