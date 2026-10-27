"""ZenPlanner integration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .const import DOMAIN, PLATFORMS
from .models import (
    class_payload,
    outcome_payload,
    parse_entity_uid,
    require_class_id,
)

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse

    from .gym import Gym


def _schemas():
    import voluptuous as vol
    from homeassistant.helpers import config_validation as cv

    reserve = vol.Schema(
        {
            vol.Required("entity_id"): cv.entity_ids,
            vol.Required("class_id"): cv.string,
            vol.Optional("join_waitlist", default=False): cv.boolean,
        }
    )
    cancel = vol.Schema(
        {
            vol.Required("entity_id"): cv.entity_ids,
            vol.Required("class_id"): cv.string,
        }
    )
    listing = vol.Schema({vol.Required("entity_id"): cv.entity_ids})
    return reserve, cancel, listing


def __getattr__(name: str):
    if name not in {"RESERVE_SCHEMA", "CANCEL_SCHEMA", "LIST_SCHEMA"}:
        raise AttributeError(name)
    reserve, cancel, listing = _schemas()
    globals()["RESERVE_SCHEMA"] = reserve
    globals()["CANCEL_SCHEMA"] = cancel
    globals()["LIST_SCHEMA"] = listing
    return globals()[name]


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    hass.data.setdefault(DOMAIN, {})
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    from .gym import Gym

    gym = Gym(hass, entry)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = gym
    await gym.async_config_entry_first_refresh()
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    async_register_services(hass)
    entry.async_on_unload(entry.add_update_listener(_async_entry_updated))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    hass.data[DOMAIN].pop(entry.entry_id, None)
    if not hass.data[DOMAIN]:
        for service in ("reserve", "cancel", "list_classes"):
            hass.services.async_remove(DOMAIN, service)
    return unloaded


async def _async_entry_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    # Token rotation writes the entry too. Reloading then would run during the call that saved the pair.
    from .gym import enabled_ids

    gym = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if gym is None or enabled_ids(entry) == gym.people_ids:
        return
    await hass.config_entries.async_reload(entry.entry_id)


def async_register_services(hass: HomeAssistant) -> None:
    from homeassistant.core import ServiceCall, ServiceResponse, SupportsResponse

    if hass.services.has_service(DOMAIN, "reserve"):
        return

    reserve_schema, cancel_schema, list_schema = _schemas()

    async def reserve(call: ServiceCall) -> ServiceResponse:
        return await async_handle_reserve(hass, call)

    async def cancel(call: ServiceCall) -> ServiceResponse:
        return await async_handle_cancel(hass, call)

    async def list_classes(call: ServiceCall) -> ServiceResponse:
        return await async_handle_list_classes(hass, call)

    hass.services.async_register(
        DOMAIN,
        "reserve",
        reserve,
        schema=reserve_schema,
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN,
        "cancel",
        cancel,
        schema=cancel_schema,
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN,
        "list_classes",
        list_classes,
        schema=list_schema,
        supports_response=SupportsResponse.ONLY,
    )


def _person_from_target(hass: HomeAssistant, call: ServiceCall) -> tuple[Gym, str]:
    from homeassistant.exceptions import ServiceValidationError
    from homeassistant.helpers import entity_registry as er

    raw = call.data.get("entity_id")
    if isinstance(raw, str):
        entity_ids = [raw]
    elif isinstance(raw, list):
        entity_ids = raw
    else:
        entity_ids = []
    if len(entity_ids) != 1:
        raise ServiceValidationError("Choose one calendar or upcoming sensor")
    registry_entry = er.async_get(hass).async_get(entity_ids[0])
    if registry_entry is None or not registry_entry.unique_id:
        raise ServiceValidationError("Entity is not a ZenPlanner person")
    try:
        _kind, partition, person_id = parse_entity_uid(registry_entry.unique_id)
    except ValueError as err:
        raise ServiceValidationError("Entity is not a ZenPlanner person") from err
    for gym in hass.data.get(DOMAIN, {}).values():
        if gym.partition_id == partition:
            return gym, person_id
    raise ServiceValidationError("Gym is not loaded")


def _class_id(call: ServiceCall):
    from homeassistant.exceptions import ServiceValidationError

    try:
        return require_class_id(str(call.data.get("class_id", "")))
    except ValueError as err:
        raise ServiceValidationError("Enter a class id") from err


async def async_handle_reserve(hass: HomeAssistant, call: ServiceCall) -> ServiceResponse:
    gym, person_id = _person_from_target(hass, call)
    outcome = await gym.reserve(
        person_id,
        _class_id(call),
        join_waitlist=bool(call.data.get("join_waitlist", False)),
    )
    return dict(outcome_payload(outcome))


async def async_handle_cancel(hass: HomeAssistant, call: ServiceCall) -> ServiceResponse:
    from homeassistant.exceptions import HomeAssistantError, ServiceValidationError

    from .gym import HoldRemains, UnregisterUnavailable

    gym, person_id = _person_from_target(hass, call)
    try:
        result = await gym.cancel(person_id, _class_id(call))
    except UnregisterUnavailable as err:
        raise ServiceValidationError(str(err)) from err
    except HoldRemains as err:
        raise HomeAssistantError(str(err)) from err
    return {"result": result}


async def async_handle_list_classes(hass: HomeAssistant, call: ServiceCall) -> ServiceResponse:
    from homeassistant.exceptions import ServiceValidationError

    gym, person_id = _person_from_target(hass, call)
    if gym.data is None:
        raise ServiceValidationError("The schedule is not ready")
    agenda = gym.data.agenda_for(person_id)
    rows = agenda.during_horizon(gym.data.school.now())
    return {"classes": [dict(class_payload(session)) for session in rows]}
