"""Constants for the Homebox integration."""

DOMAIN = "homebox"

CONF_URL = "url"
CONF_TOKEN = "token"
CONF_USERNAME = "username"
CONF_PASSWORD = "password"
CONF_AUTH_METHOD = "auth_method"
AUTH_METHOD_TOKEN = "token"
AUTH_METHOD_LOGIN = "login"
CONF_USE_HTTPS = "use_https"

HOMEBOX_API_URL = "api/v1"
COORDINATOR = "coordinator"

# Token refresh configuration
TOKEN_REFRESH_INTERVAL = 60 * 60  # Refresh token every hour (in seconds)
TOKEN_EXPIRY_BUFFER = 60 * 5  # 5 minutes buffer before token expires

# Service constants
SERVICE_MOVE_ITEM = "move_item"
SERVICE_REFRESH_TOKEN = "refresh_token"
SERVICE_CREATE_ITEM = "create_item"
ATTR_ITEM_ID = "item_id"
ATTR_LOCATION_ID = "location_id"
ATTR_ITEM_NAME = "name"
ATTR_ITEM_DESCRIPTION = "description"
ATTR_ITEM_QUANTITY = "quantity"
ATTR_ITEM_ASSET_ID = "asset_id"
ATTR_ITEM_PURCHASE_PRICE = "purchase_price"
ATTR_ITEM_FIELDS = "fields"
ATTR_ITEM_LABELS = "labels"
