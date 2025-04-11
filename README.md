# Homebox Integration for Home Assistant

This custom component for Home Assistant integrates with the [Homebox](https://hay-kot.github.io/homebox/) inventory management system, allowing you to view and manage your inventory items from within Home Assistant.

## Features

- Syncs Homebox items with Home Assistant as sensors
- Maps Homebox locations to Home Assistant areas
- Displays item details including location, description, and custom fields
- Service to move items between locations

## Installation

### Using HACS (recommended)

1. Make sure you have [HACS](https://hacs.xyz/) installed
2. Go to HACS → Integrations → Click the three dots in the top right corner → Custom repositories
3. Add this repository URL and select "Integration" as the category
4. Click "Add"
5. Search for "Homebox" and install it
6. Restart Home Assistant

### Manual Installation

1. Download the latest release
2. Create a `custom_components/homebox` directory in your Home Assistant configuration directory
3. Extract the contents of the release into the directory
4. Restart Home Assistant

## Configuration

1. Go to Settings → Devices & Services → Add Integration
2. Search for "Homebox"
3. Enter your Homebox API URL (e.g., `https://homebox.example.com`)
4. Enter your Homebox API token
5. Click "Submit"

## Usage

### Item Sensors

Each item from your Homebox inventory will appear as a sensor in Home Assistant. The sensor state shows the item's current location.

Sensor attributes include:
- Item ID
- Name
- Description
- Location details
- Labels
- Custom fields

### Services

#### homebox.move_item

Move an item to a new location.

| Parameter | Description |
|-----------|-------------|
| item_id | ID of the item to move |
| location_id | ID of the destination location |

Example:
```yaml
service: homebox.move_item
data:
  item_id: "12345"
  location_id: "6789"
```

## Automations Examples

### Move item when a door is opened
```yaml
automation:
  - alias: "Move keys when front door opens"
    trigger:
      - platform: state
        entity_id: binary_sensor.front_door
        to: "on"
    action:
      - service: homebox.move_item
        data:
          item_id: "12345"  # ID of your keys
          location_id: "6789"  # ID of "Entryway" location
```

### Notify when item is moved
```yaml
automation:
  - alias: "Notify when important item is moved"
    trigger:
      - platform: state
        entity_id: sensor.homebox_important_item
    action:
      - service: notify.mobile_app
        data:
          title: "Item Moved"
          message: "{{ trigger.entity_id.split('_')[-1] }} moved to {{ trigger.to_state.state }}"
```

## Troubleshooting

- Ensure your Homebox API URL is correct and accessible from Home Assistant
- Verify your API token has the necessary permissions
- Check Home Assistant logs for detailed error messages

## License

This project is licensed under the MIT License - see the LICENSE file for details.