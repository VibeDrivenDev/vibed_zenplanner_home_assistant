"""Config flow for one ZenPlanner gym."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.selector import (
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .const import DOMAIN
from .gym import (
    CannotConnect,
    Gym,
    WrongGym,
    discover,
    initial_entry,
    initial_options,
)
from .models import Authenticated, AuthFailed, ChooseGym, OrgChoice, PersonId

USER_SCHEMA = vol.Schema(
    {
        vol.Required("username"): TextSelector(),
        vol.Required("password"): TextSelector(
            TextSelectorConfig(type=TextSelectorType.PASSWORD)
        ),
    }
)
PASSWORD_SCHEMA = vol.Schema(
    {
        vol.Required("password"): TextSelector(
            TextSelectorConfig(type=TextSelectorType.PASSWORD)
        ),
    }
)


def _choice_key(choice: OrgChoice) -> str:
    return f"{choice.org_id}|{choice.user_id}"


class ZenPlannerConfigFlow(ConfigFlow, domain=DOMAIN):
    """One config entry is one gym."""

    VERSION = 1

    def __init__(self) -> None:
        self._username = ""
        self._password = ""
        self._choices: tuple[OrgChoice, ...] = ()
        self._auth: Authenticated | None = None

    async def async_step_user(self, user_input: dict[str, str] | None = None) -> ConfigFlowResult:
        if user_input is None:
            return self.async_show_form(step_id="user", data_schema=USER_SCHEMA)
        self._username = user_input["username"]
        self._password = user_input["password"]
        try:
            result = await discover(self.hass, self._username, self._password)
        except CannotConnect:
            return self.async_show_form(
                step_id="user", data_schema=USER_SCHEMA, errors={"base": "cannot_connect"}
            )
        return await self._continue(result, "user")

    async def async_step_gym(self, user_input: dict[str, str] | None = None) -> ConfigFlowResult:
        if not self._choices:
            return self.async_abort(reason="invalid_auth")
        options = {_choice_key(choice): choice.label for choice in self._choices}
        schema = vol.Schema({vol.Required("gym"): vol.In(options)})
        if user_input is None:
            return self.async_show_form(step_id="gym", data_schema=schema)
        selected = next(
            choice for choice in self._choices if _choice_key(choice) == user_input["gym"]
        )
        try:
            result = await discover(self.hass, self._username, self._password, selected)
        except CannotConnect:
            return self.async_show_form(
                step_id="gym", data_schema=schema, errors={"base": "cannot_connect"}
            )
        return await self._continue(result, "gym")

    async def async_step_people(
        self, user_input: dict[str, list[str]] | None = None
    ) -> ConfigFlowResult:
        if self._auth is None:
            return self.async_abort(reason="unknown")
        labels = {person.person_id: person.display_name for person in self._auth.family}
        schema = vol.Schema(
            {
                vol.Required(
                    "enabled_person_ids", default=[self._auth.person.person_id]
                ): cv.multi_select(labels)
            }
        )
        if user_input is None:
            return self.async_show_form(step_id="people", data_schema=schema)
        enabled = frozenset(PersonId(person_id) for person_id in user_input["enabled_person_ids"])
        return self._create(enabled)

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, str] | None = None
    ) -> ConfigFlowResult:
        if user_input is None:
            return self.async_show_form(step_id="reauth_confirm", data_schema=PASSWORD_SCHEMA)
        entry = self.hass.config_entries.async_get_entry(self.context["entry_id"])
        if entry is None:
            return self.async_abort(reason="unknown")
        gym = self.hass.data.get(DOMAIN, {}).get(entry.entry_id)
        if gym is None:
            gym = Gym(self.hass, entry)
        try:
            reason = await gym.reauthenticate(user_input["password"])
        except CannotConnect:
            return self.async_show_form(
                step_id="reauth_confirm",
                data_schema=PASSWORD_SCHEMA,
                errors={"base": "cannot_connect"},
            )
        except WrongGym:
            return self.async_show_form(
                step_id="reauth_confirm",
                data_schema=PASSWORD_SCHEMA,
                errors={"base": "wrong_gym"},
            )
        if reason is not None:
            return self.async_show_form(
                step_id="reauth_confirm",
                data_schema=PASSWORD_SCHEMA,
                errors={"base": reason},
            )
        self.hass.config_entries.async_schedule_reload(entry.entry_id)
        return self.async_abort(reason="reauth_successful")

    async def _continue(self, result: object, step_id: str) -> ConfigFlowResult:
        if isinstance(result, AuthFailed):
            schema = USER_SCHEMA if step_id == "user" else self._gym_schema()
            return self.async_show_form(
                step_id=step_id, data_schema=schema, errors={"base": result.reason}
            )
        if isinstance(result, ChooseGym):
            self._choices = result.organizations
            if not self._choices:
                return self.async_show_form(
                    step_id="user", data_schema=USER_SCHEMA, errors={"base": "invalid_auth"}
                )
            return await self.async_step_gym()
        if not isinstance(result, Authenticated):
            return self.async_abort(reason="unknown")
        return await self._after_auth(result)

    async def _after_auth(self, auth: Authenticated) -> ConfigFlowResult:
        self._auth = auth
        await self.async_set_unique_id(str(auth.partition_id))
        self._abort_if_unique_id_configured()
        if len(auth.family) > 1:
            return await self.async_step_people()
        return self._create(frozenset(person.person_id for person in auth.family))

    def _create(self, enabled: frozenset[PersonId]) -> ConfigFlowResult:
        assert self._auth is not None
        return self.async_create_entry(
            title=self._auth.school_name,
            data=initial_entry(self._auth, self._username, self._password),
            options=initial_options(enabled),
        )

    def _gym_schema(self) -> vol.Schema:
        options = {_choice_key(choice): choice.label for choice in self._choices}
        return vol.Schema({vol.Required("gym"): vol.In(options)})

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return ZenPlannerOptionsFlow(config_entry)


class ZenPlannerOptionsFlow(OptionsFlow):
    """Enable or disable people on this gym."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        self._config_entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, list[str]] | None = None
    ) -> ConfigFlowResult:
        gym: Gym | None = self.hass.data.get(DOMAIN, {}).get(self._config_entry.entry_id)
        if gym is None:
            return self.async_abort(reason="unknown")
        if user_input is None:
            await gym.async_refresh()
        family = gym.family()
        if not family:
            return self.async_abort(reason="cannot_connect")
        labels = {person.person_id: person.display_name for person in family}
        current = [person.person_id for person in family if person.person_id in gym.people_ids]
        schema = vol.Schema(
            {vol.Required("enabled_person_ids", default=current): cv.multi_select(labels)}
        )
        if user_input is None:
            return self.async_show_form(step_id="init", data_schema=schema)
        enabled = frozenset(PersonId(person_id) for person_id in user_input["enabled_person_ids"])
        return self.async_create_entry(title="", data=initial_options(enabled))
