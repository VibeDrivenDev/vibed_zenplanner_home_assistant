"""Upcoming open classes for one person."""

from __future__ import annotations

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .gym import Gym, enabled_ids
from .models import Person, enabled_people, entity_uid, open_class_view


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    gym: Gym = hass.data[DOMAIN][entry.entry_id]
    people = enabled_people(gym.family(), enabled_ids(entry))
    async_add_entities(UpcomingClassesSensor(gym, person) for person in people)


class UpcomingClassesSensor(CoordinatorEntity[Gym], SensorEntity):
    """Next open class, plus the first 10 open classes in the horizon."""

    _attr_icon = "mdi:calendar-clock"

    def __init__(self, gym: Gym, person: Person) -> None:
        super().__init__(gym)
        self._person_id = person.person_id
        self._attr_unique_id = entity_uid("upcoming", gym.partition_id, person.person_id)
        self._attr_name = person.display_name
        self._attr_device_info = gym.device_info()

    @property
    def native_value(self) -> str:
        view = self._view()
        if view is None:
            return "none"
        return view.state

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        view = self._view()
        if view is None:
            return {"count": 0, "classes": []}
        return {"count": view.count, "classes": [dict(item) for item in view.classes]}

    def _view(self):
        data = self.coordinator.data
        if data is None:
            return None
        return open_class_view(data.agenda_for(self._person_id), data.school.now())
