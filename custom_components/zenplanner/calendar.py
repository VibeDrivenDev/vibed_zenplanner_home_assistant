"""Calendar of sessions one person holds."""

from __future__ import annotations

from datetime import datetime

from homeassistant.components.calendar import CalendarEntity, CalendarEvent
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .gym import Gym, enabled_ids
from .models import HeldEvent, Person, enabled_people, entity_uid, held_event


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    gym: Gym = hass.data[DOMAIN][entry.entry_id]
    people = enabled_people(gym.family(), enabled_ids(entry))
    async_add_entities(PersonCalendar(gym, person) for person in people)


class PersonCalendar(CoordinatorEntity[Gym], CalendarEntity):
    """Sessions this person holds. Open classes stay off the calendar."""

    _attr_supported_features = 0

    def __init__(self, gym: Gym, person: Person) -> None:
        super().__init__(gym)
        self._person_id = person.person_id
        self._attr_unique_id = entity_uid("calendar", gym.partition_id, person.person_id)
        self._attr_name = person.display_name
        self._attr_device_info = gym.device_info()

    @property
    def event(self) -> CalendarEvent | None:
        data = self.coordinator.data
        if data is None:
            return None
        session = data.agenda_for(self._person_id).next_held(data.school.now())
        if session is None:
            return None
        return _calendar_event(
            held_event(self.coordinator.partition_id, self._person_id, session)
        )

    async def async_get_events(
        self,
        hass: HomeAssistant,
        start_date: datetime,
        end_date: datetime,
    ) -> list[CalendarEvent]:
        data = self.coordinator.data
        if data is None:
            return []
        sessions = data.agenda_for(self._person_id).held_between(start_date, end_date)
        return [
            _calendar_event(held_event(self.coordinator.partition_id, self._person_id, session))
            for session in sessions
        ]


def _calendar_event(held: HeldEvent) -> CalendarEvent:
    return CalendarEvent(
        start=held.start,
        end=held.end,
        summary=held.summary,
        description=held.description,
        location=held.location,
        uid=held.uid,
    )
