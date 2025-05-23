"""Config Flow for Hive."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from apyhiveapi import Auth
from apyhiveapi.helper.hive_exceptions import (
    HiveApiError,
    HiveInvalid2FACode,
    HiveInvalidPassword,
    HiveInvalidUsername,
)
import voluptuous as vol

from homeassistant.config_entries import (
    SOURCE_REAUTH,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_PASSWORD, CONF_SCAN_INTERVAL, CONF_USERNAME
from homeassistant.core import callback

from . import HiveConfigEntry
from .const import CONF_CODE, CONF_DEVICE_NAME, CONFIG_ENTRY_VERSION, DOMAIN


class HiveFlowHandler(ConfigFlow, domain=DOMAIN):
    """Handle a Hive config flow."""

    VERSION = CONFIG_ENTRY_VERSION
    hive_auth: Auth

    def __init__(self) -> None:
        """Initialize the config flow."""
        self.data: dict[str, Any] = {}
        self.tokens: dict[str, str] = {}
        self.device_registration: bool = False
        self.device_name = "Home Assistant"

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Prompt user input. Create or edit entry."""
        errors: dict[str, str] = {}
        # Login to Hive with user data.
        if user_input is not None:
            self.data.update(user_input)
            self.hive_auth = Auth(
                username=self.data[CONF_USERNAME], password=self.data[CONF_PASSWORD]
            )

            # Get user from existing entry and abort if already setup
            await self.async_set_unique_id(self.data[CONF_USERNAME])
            if self.context["source"] != SOURCE_REAUTH:
                self._abort_if_unique_id_configured()

            # Login to the Hive.
            try:
                self.tokens = await self.hive_auth.login()
            except HiveInvalidUsername:
                errors["base"] = "invalid_username"
            except HiveInvalidPassword:
                errors["base"] = "invalid_password"
            except HiveApiError:
                errors["base"] = "no_internet_available"

            if self.tokens.get("ChallengeName") == "SMS_MFA":
                # Complete SMS 2FA.
                return await self.async_step_2fa()

            if not errors:
                # Complete the entry.
                try:
                    return await self.async_setup_hive_entry()
                except UnknownHiveError:
                    errors["base"] = "unknown"

        # Show User Input form.
        schema = vol.Schema(
            {vol.Required(CONF_USERNAME): str, vol.Required(CONF_PASSWORD): str}
        )
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    async def async_step_2fa(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle 2fa step."""
        errors = {}

        if user_input and user_input["2fa"] == "0000":
            self.tokens = await self.hive_auth.login()
        elif user_input:
            try:
                self.tokens = await self.hive_auth.sms_2fa(
                    user_input["2fa"], self.tokens
                )
            except HiveInvalid2FACode:
                errors["base"] = "invalid_code"
            except HiveApiError:
                errors["base"] = "no_internet_available"

            if not errors:
                if self.source == SOURCE_REAUTH:
                    return await self.async_setup_hive_entry()
                self.device_registration = True
                return await self.async_step_configuration()

        schema = vol.Schema({vol.Required(CONF_CODE): str})
        return self.async_show_form(step_id="2fa", data_schema=schema, errors=errors)

    async def async_step_configuration(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle hive configuration step."""
        errors = {}

        if user_input:
            if self.device_registration:
                self.device_name = user_input["device_name"]
                await self.hive_auth.device_registration(user_input["device_name"])
                self.data["device_data"] = await self.hive_auth.get_device_data()

            try:
                return await self.async_setup_hive_entry()
            except UnknownHiveError:
                errors["base"] = "unknown"

        schema = vol.Schema(
            {vol.Optional(CONF_DEVICE_NAME, default=self.device_name): str}
        )
        return self.async_show_form(
            step_id="configuration", data_schema=schema, errors=errors
        )

    async def async_setup_hive_entry(self) -> ConfigFlowResult:
        """Finish setup and create the config entry."""

        if "AuthenticationResult" not in self.tokens:
            raise UnknownHiveError

        # Setup the config entry
        self.data["tokens"] = self.tokens
        if self.source == SOURCE_REAUTH:
            return self.async_update_reload_and_abort(
                self._get_reauth_entry(),
                title=self.data["username"],
                data=self.data,
                reason="reauth_successful",
            )
        return self.async_create_entry(title=self.data["username"], data=self.data)

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Re Authenticate a user."""
        data = {
            CONF_USERNAME: entry_data[CONF_USERNAME],
            CONF_PASSWORD: entry_data[CONF_PASSWORD],
        }
        return await self.async_step_user(data)

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: HiveConfigEntry,
    ) -> HiveOptionsFlowHandler:
        """Hive options callback."""
        return HiveOptionsFlowHandler(config_entry)


class HiveOptionsFlowHandler(OptionsFlow):
    """Config flow options for Hive."""

    config_entry: HiveConfigEntry

    def __init__(self, config_entry: HiveConfigEntry) -> None:
        """Initialize Hive options flow."""
        self.hive = None
        self.interval = config_entry.options.get(CONF_SCAN_INTERVAL, 120)

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage the options."""
        return await self.async_step_user()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle a flow initialized by the user."""
        self.hive = self.config_entry.runtime_data
        errors: dict[str, str] = {}
        if user_input is not None:
            new_interval = user_input.get(CONF_SCAN_INTERVAL)
            assert self.hive
            await self.hive.updateInterval(new_interval)
            return self.async_create_entry(title="", data=user_input)

        schema = vol.Schema(
            {
                vol.Optional(CONF_SCAN_INTERVAL, default=self.interval): vol.All(
                    vol.Coerce(int), vol.Range(min=30)
                )
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)


class UnknownHiveError(Exception):
    """Catch unknown hive error."""
