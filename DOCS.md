# Homebox Integration for Home Assistant

This integration connects Home Assistant with [Homebox](https://hay-kot.github.io/homebox/), the self-hosted inventory management system. Track your items, locations, and manage your inventory directly from Home Assistant.

## Features

- Creates sensors for each Homebox item
- Shows location information for items
- Allows moving items between locations via service calls
- Support for linked items between inventory items
- Automatically refreshes authentication tokens
- Provides manual token refresh for troubleshooting

## Installation

### HACS Installation (Recommended)

1. Open HACS in your Home Assistant instance
2. Click on "Integrations"
3. Click the three dots in the top-right corner and select "Custom repositories"
4. Add the URL: `https://github.com/yourusername/homebox_ha` with category "Integration"
5. Click "Add"
6. Search for "Homebox" in the integrations
7. Click "Download"
8. Restart Home Assistant

### Manual Installation

1. Download the latest release
2. Copy the `custom_components/homebox` directory to your Home Assistant `custom_components` directory
3. Restart Home Assistant

## Configuration

The integration is configured via the Home Assistant UI:

1. Go to **Settings** > **Devices & Services**
2. Click the **+ Add Integration** button
3. Search for "Homebox" and select it

### Authentication Options

The integration supports two authentication methods:

#### API Token Authentication

- **Homebox API URL**: The URL to your Homebox instance (without http:// or https://)
- **Use HTTPS**: Toggle on to use HTTPS, toggle off to use HTTP
- **API Token**: Your Homebox API token (can be created in the Homebox web interface)

##### Getting an API Token with cURL

You can generate a Homebox API token using cURL from the command line:

```bash
# For HTTPS
curl -X POST "https://your-homebox-instance/api/v1/users/login" \
     -H "Content-Type: application/json" \
     -d '{"email":"your-email@example.com", "password":"your-password"}'

# For HTTP
curl -X POST "http://your-homebox-instance/api/v1/users/login" \
     -H "Content-Type: application/json" \
     -d '{"email":"your-email@example.com", "password":"your-password"}'
```

This command will return a JSON response containing your token:

```json
{
  "token": "your-api-token-here",
  "user": { ... }
}
```

Copy the token value and use it in the integration setup. 

**Important:** Only copy the token value itself - do not include "Bearer " as a prefix. The integration will add this automatically. For example, if you get a token "abc123xyz", enter just "abc123xyz" in the integration setup, not "Bearer abc123xyz".

#### Username & Password Authentication

- **Homebox API URL**: The URL to your Homebox instance (without http:// or https://)
- **Use HTTPS**: Toggle on to use HTTPS, toggle off to use HTTP
- **Username**: Your Homebox username
- **Password**: Your Homebox password

## Usage

### Entities

The integration creates sensor entities for each item in your Homebox inventory:

- Entity ID format: `sensor.homebox_[item_name]`
- State: The current location name of the item
- Attributes:
  - `id`: The Homebox ID of the item
  - `name`: The name of the item
  - `description`: The item description
  - `location_id`: The ID of the item's location
  - `location_name`: The name of the location
  - `location`: Detailed location information
  - `labels`: Any labels attached to the item
  - `fields`: Custom fields for the item
  - `linked_items`: Any linked items
  - `created_at`: Creation timestamp
  - `updated_at`: Last update timestamp

### Services

#### homebox.move_item

Move an item to a new location.

**Parameters:**
- `item_id`: The ID of the item to move
- `location_id`: The ID of the destination location

**Example:**
```yaml
service: homebox.move_item
data:
  item_id: "12345"
  location_id: "67890"
```

#### homebox.create_item

Create a new item in Homebox.

**Required Parameters:**
- `name`: The name of the item
- `location_id`: The ID of the location for the item

**Optional Parameters:**
- `description`: Description of the item
- `quantity`: Quantity of the item (integer)
- `asset_id`: Asset ID or SKU for the item
- `purchase_price`: Purchase price of the item (float)
- `fields`: Custom fields as a JSON object (e.g., {"warranty": "2 years"})
- `labels`: Array of label IDs to attach to the item

**Example:**
```yaml
service: homebox.create_item
data:
  name: "Kitchen Mixer"
  location_id: "67890"
  description: "KitchenAid mixer, red"
  quantity: 1
  asset_id: "KSM-12345"
  purchase_price: 299.99
  fields:
    warranty: "2 years"
    color: "red"
    purchased_from: "Amazon"
  labels:
    - "label-id-1"
    - "label-id-2"
```

**Notes on Creating Items:**
- After creating an item, a notification will appear in Home Assistant with the new item's details
- If creation fails, an error notification will be shown
- The new item will automatically appear as a sensor after the next data refresh
- You can find available location IDs by:
  1. Looking at the attributes of existing item sensors
  2. Using the developer tools to inspect the coordinator data in `hass.data['homebox'][entry_id]['coordinator'].locations`
  3. Using cURL to fetch locations: `curl -X GET "https://your-homebox-instance/api/v1/locations" -H "Authorization: Bearer your-api-token"`

#### homebox.refresh_token

Manually trigger a token refresh and see detailed logs. This is useful for troubleshooting authentication issues.

**Example:**
```yaml
service: homebox.refresh_token
```

### Manual Token Refresh Feature

The integration includes a manual token refresh feature that can help diagnose authentication problems:

1. Go to **Developer Tools** > **Services**
2. Select the `homebox.refresh_token` service
3. Click **Call Service**
4. A notification will appear in Home Assistant with detailed logs of the token refresh process
5. Check the notification to see the current token, refresh attempts, API responses, and final result

This feature is particularly useful when:
- You're experiencing 401 Unauthorized errors
- You need to force a token refresh without waiting for automatic refresh
- You want to verify that authentication is working correctly
- You're troubleshooting connection issues with your Homebox instance

The notification will show:
- Current token (first 10 characters, for security)
- API responses during the refresh attempt
- Success or failure of the refresh
- New token (if refresh was successful)
- Any errors encountered during the process

## Troubleshooting

### Common Issues

1. **Connection Failed**
   - Check that your Homebox instance is running and accessible
   - Verify the URL is correct and doesn't include http:// or https://
   - Ensure the "Use HTTPS" toggle matches your Homebox setup

2. **Authentication Failed**
   - Verify your API token or username/password
   - Try manually refreshing the token using the service
   - Check that your token has not expired in Homebox

3. **Sensors Not Updating**
   - Verify that your Homebox API is accessible
   - Manually refresh the token and check for errors
   - Restart Home Assistant if entities are missing

### Debug Logs

To enable debug logs for the integration:

1. Add the following to your `configuration.yaml`:
```yaml
logger:
  default: info
  logs:
    custom_components.homebox: debug
```
2. Restart Home Assistant
3. Check the logs for detailed information about the integration's operation

### Verify API Connection

You can verify your Homebox API connection and authentication using cURL:

```bash
# Test connection with API token (replace with your info)
curl -X GET "https://your-homebox-instance/api/v1/items" \
     -H "Authorization: Bearer your-api-token"

# Test locations endpoint with API token (replace with your info)
curl -X GET "https://your-homebox-instance/api/v1/locations" \
     -H "Authorization: Bearer your-api-token"
```

If these commands return valid JSON responses, your API connection is working correctly. These tests can help identify whether authentication issues are related to your Homebox API configuration or the Home Assistant integration.

## Support

- For bugs and feature requests, please [open an issue on GitHub](https://github.com/yourusername/homebox_ha/issues)
- For general questions, please use the [Home Assistant community forums](https://community.home-assistant.io/)

## Contributing

Contributions are welcome! Please feel free to submit a Pull Request.