"""Config flow for Homebox integration."""
from __future__ import annotations

import logging
from typing import Any

import aiohttp
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession
import homeassistant.helpers.selector as selector

from .const import (
    DOMAIN,
    CONF_URL,
    CONF_TOKEN,
    CONF_USERNAME,
    CONF_PASSWORD,
    CONF_AUTH_METHOD,
    AUTH_METHOD_TOKEN,
    AUTH_METHOD_LOGIN,
    CONF_USE_HTTPS,
)

_LOGGER = logging.getLogger(__name__)

AUTH_METHOD_OPTIONS = [
    selector.SelectOptionDict(value=AUTH_METHOD_TOKEN, label="API Token"),
    selector.SelectOptionDict(
        value=AUTH_METHOD_LOGIN,
        label="Username & Password"
    ),
]

# Schema to select authentication method
AUTH_METHOD_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_URL): str,
        vol.Required(CONF_USE_HTTPS, default=True): bool,
        vol.Required(CONF_AUTH_METHOD, default=AUTH_METHOD_TOKEN):
        selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=AUTH_METHOD_OPTIONS,
                mode=selector.SelectSelectorMode.DROPDOWN
            )
        ),
    }
)

# Schema for token authentication
TOKEN_AUTH_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_URL): str,
        vol.Required(CONF_USE_HTTPS): bool,
        vol.Required(CONF_AUTH_METHOD): str,
        vol.Required(CONF_TOKEN): str,
    }
)

# Schema for username/password authentication
LOGIN_AUTH_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_URL): str,
        vol.Required(CONF_USE_HTTPS): bool,
        vol.Required(CONF_AUTH_METHOD): str,
        vol.Required(CONF_USERNAME): str,
        vol.Required(CONF_PASSWORD): str,
    }
)


async def get_token_from_login(
    session: aiohttp.ClientSession, url: str, username: str, password: str
) -> str:
    """Get authentication token using username and password."""
    login_url = f"{url}/users/login"
    try:
        async with session.post(
            login_url, json={"email": username, "password": password}
        ) as response:
            if response.status != 200:
                _LOGGER.error("Failed to authenticate: %s", response.status)
                raise InvalidAuth
            data = await response.json()
            if "token" not in data:
                _LOGGER.error("No token in response: %s", data)
                raise InvalidAuth
            return data["token"]
    except aiohttp.ClientError as error:
        _LOGGER.error("Connection error during login: %s", error)
        raise CannotConnect from error


async def validate_input(hass: HomeAssistant, data: dict[str, Any]) -> dict[str, Any]:
    """Validate the user input allows us to connect.

    Data has the keys from schema with values provided by the user.
    """
    session = async_get_clientsession(hass)
    protocol = "https" if data[CONF_USE_HTTPS] else "http"
    api_url = f"{protocol}://{data[CONF_URL].rstrip('/')}/api/v1"
    # Get token based on authentication method
    if data[CONF_AUTH_METHOD] == AUTH_METHOD_LOGIN:
        token = await get_token_from_login(
            session, api_url, data[CONF_USERNAME], data[CONF_PASSWORD]
        )
        # Store the obtained token in the data
        data[CONF_TOKEN] = token
        # We don't need to store the password in HA's configuration
        data.pop(CONF_PASSWORD)
    else:
        # Token authentication
        token = data[CONF_TOKEN]

    try:
        # Verify we can access the API with the token
        async with session.get(
            f"{api_url}/items", headers={"Authorization": f"Bearer {token}"}
        ) as response:
            if response.status != 200:
                raise InvalidAuth
            # Get user info to set a title
            async with session.get(
                f"{api_url}/users/me",
                headers={"Authorization": f"Bearer {token}"}
            ) as user_response:
                if user_response.status == 200:
                    user_data = await user_response.json()
                    title = f"Homebox ({user_data.get('email', 'Unknown')})"
                else:
                    title = "Homebox"
    except aiohttp.ClientConnectionError as error:
        raise CannotConnect from error

    # Return info to be stored in the config entry
    return {"title": title, "data": data}


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Homebox."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the config flow."""
        self._auth_method: str | None = None
        self._url: str | None = None
        self._use_https: bool = True

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the initial step to select authentication method."""
        errors: dict[str, str] = {}
        if user_input is not None:
            self._auth_method = user_input[CONF_AUTH_METHOD]
            self._url = user_input[CONF_URL]
            self._use_https = user_input[CONF_USE_HTTPS]
            if self._auth_method == AUTH_METHOD_TOKEN:
                return await self.async_step_token()
            else:
                return await self.async_step_login()
        return self.async_show_form(
            step_id="user", data_schema=AUTH_METHOD_SCHEMA, errors=errors
        )

    async def async_step_token(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the token authentication step."""
        errors: dict[str, str] = {}
        if user_input is not None:
            user_input[CONF_URL] = self._url
            user_input[CONF_USE_HTTPS] = self._use_https
            user_input[CONF_AUTH_METHOD] = AUTH_METHOD_TOKEN
            try:
                info = await validate_input(self.hass, user_input)
                return self.async_create_entry(
                    title=info["title"], data=info["data"]
                )
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except InvalidAuth:
                errors["base"] = "invalid_auth"
            except Exception:  # pylint: disable=broad-except
                _LOGGER.exception("Unexpected exception")
                errors["base"] = "unknown"
        # Pre-fill the URL from the previous step
        schema = TOKEN_AUTH_SCHEMA.extend({
            vol.Required(CONF_URL, default=self._url): str,
            vol.Required(CONF_USE_HTTPS, default=self._use_https): bool,
            vol.Required(CONF_AUTH_METHOD, default=AUTH_METHOD_TOKEN): str,
        })
        return self.async_show_form(
            step_id="token", data_schema=schema, errors=errors
        )

    async def async_step_login(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the username/password authentication step."""
        errors: dict[str, str] = {}
        if user_input is not None:
            user_input[CONF_URL] = self._url
            user_input[CONF_USE_HTTPS] = self._use_https
            user_input[CONF_AUTH_METHOD] = AUTH_METHOD_LOGIN
            try:
                info = await validate_input(self.hass, user_input)
                return self.async_create_entry(
                    title=info["title"], data=info["data"]
                )
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except InvalidAuth:
                errors["base"] = "invalid_auth"
            except Exception:  # pylint: disable=broad-except
                _LOGGER.exception("Unexpected exception")
                errors["base"] = "unknown"
        # Pre-fill the URL from the previous step
        schema = LOGIN_AUTH_SCHEMA.extend({
            vol.Required(CONF_URL, default=self._url): str,
            vol.Required(CONF_USE_HTTPS, default=self._use_https): bool,
            vol.Required(CONF_AUTH_METHOD, default=AUTH_METHOD_LOGIN): str,
        })
        return self.async_show_form(
            step_id="login", data_schema=schema, errors=errors
        )


class CannotConnect(HomeAssistantError):
    """Error to indicate we cannot connect."""


class InvalidAuth(HomeAssistantError):
    """Error to indicate there is invalid auth."""
