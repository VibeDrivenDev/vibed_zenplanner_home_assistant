"""What ZenPlanner's HTTP service can observe."""

from __future__ import annotations

import asyncio
import json
import tempfile
from dataclasses import dataclass
from pathlib import Path

import aiohttp.web
import pytest
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError, ServiceValidationError
from homeassistant.helpers.entity_registry import DATA_REGISTRY

import custom_components.zenplanner.gym as gym_mod
from custom_components.zenplanner import (
    CANCEL_SCHEMA,
    LIST_SCHEMA,
    RESERVE_SCHEMA,
    async_handle_cancel,
    async_handle_reserve,
)
from custom_components.zenplanner.const import DOMAIN
from custom_components.zenplanner.gym import Gym, HoldRemains, UnregisterUnavailable, initial_entry
from custom_components.zenplanner.models import (
    Authenticated,
    AuthFailed,
    ChooseGym,
    ClassId,
    HeldUncancellable,
    PersonId,
    Reserved,
    Unavailable,
    Waitlisted,
)

START = "2026-10-27T12:00:00-06:00"
END = "2026-10-27T13:00:00-06:00"


def schema_keys(schema) -> set[str]:
    return {getattr(key, "schema", key) for key in schema.schema}


def test_service_schema_has_no_internal_ids() -> None:
    assert schema_keys(RESERVE_SCHEMA) == {"entity_id", "class_id", "join_waitlist"}
    assert schema_keys(CANCEL_SCHEMA) == {"entity_id", "class_id"}
    assert schema_keys(LIST_SCHEMA) == {"entity_id"}
    filled = RESERVE_SCHEMA({"entity_id": "calendar.ada_lovelace", "class_id": "c1"})
    assert filled["join_waitlist"] is False


@dataclass
class Hit:
    method: str
    path: str
    headers: dict[str, str]
    body: object
    raw: bytes


class Api:
    def __init__(self) -> None:
        self.hits: list[Hit] = []
        self.login_status = "SUCCESS"
        self.login_token = "access-a"
        self.login_refresh = "refresh-a"
        self.organizations: list[dict] = [
            {
                "userId": "user-1",
                "organization": {"id": "part-1", "name": "South Gym", "schoolName": "South"},
            }
        ]
        self.profile_headers: dict[str, str] = {}
        self.family_headers: dict[str, str] = {}
        self.family = [
            {"personId": "ada", "familyId": "fam-1", "firstName": "Ada", "lastName": "Lovelace"}
        ]
        self.class_rows: list[dict] = []
        self.reservation_rows: list[dict] = []
        self.reserve_status = "RESERVED"
        self.waitlist_status = "WAITLISTED"
        self.delete_status = 200
        self.delete_error = None
        self.bad_body_on: set[str] = set()
        self.authorized_profile_401s = 0
        self.refresh_profile_status = 200
        self.rotate_refresh_on_401 = None
        self.recovery_headers: dict[str, str] = {}

    def matching(self, suffix: str) -> list[Hit]:
        return [hit for hit in self.hits if hit.path.endswith(suffix)]

    async def handle(self, request: aiohttp.web.Request) -> aiohttp.web.StreamResponse:
        raw = await request.read()
        body = json.loads(raw) if raw else None
        hit = Hit(
            request.method,
            request.path,
            {key.lower(): value for key, value in request.headers.items()},
            body,
            raw,
        )
        self.hits.append(hit)
        return self._respond(hit)

    def _respond(self, hit: Hit) -> aiohttp.web.StreamResponse:
        path = hit.path
        for suffix in self.bad_body_on:
            if path.endswith(suffix):
                return aiohttp.web.Response(
                    status=200,
                    headers=self.family_headers,
                    body=b"not-json",
                )
        if path.endswith("/auth/v1/login"):
            return self._json(
                {
                    "status": self.login_status,
                    "token": self.login_token,
                    "refreshToken": self.login_refresh,
                    "organizationUsers": self.organizations,
                }
            )
        if path.endswith("/profiles/current"):
            return self._profile(hit)
        if path.endswith("/family-members"):
            return self._json({"payloadArray": self.family, "error": None}, headers=self.family_headers)
        if path.endswith("/member/calendars/reserve"):
            return self._json({"payload": {"reservationStatus": self.reserve_status}, "error": None})
        if path.endswith("/calendars/waitlist") and hit.method == "POST":
            return self._json({"payload": {"reservationStatus": self.waitlist_status}, "error": None})
        if hit.method == "DELETE" and ("/calendars/reserve/" in path or "/calendars/waitlist/" in path):
            if self.delete_status == 404:
                return aiohttp.web.Response(status=404, body=b"")
            return self._json({"payload": None, "error": self.delete_error})
        if path.endswith("/calendars/reservations"):
            return self._json({"payloadArray": self.reservation_rows, "error": None})
        if path.endswith("/calendars/classes"):
            return self._json(
                {"payload": {"classes": self.class_rows, "reservationCount": len(self.class_rows)}, "error": None}
            )
        return aiohttp.web.Response(status=404, text=path)

    def _profile(self, hit: Hit) -> aiohttp.web.StreamResponse:
        authorized = bool(hit.headers.get("authorization"))
        if authorized and self.authorized_profile_401s:
            self.authorized_profile_401s -= 1
            headers = {}
            if self.rotate_refresh_on_401:
                headers["X-Auth-Refresh"] = self.rotate_refresh_on_401
            return aiohttp.web.Response(status=401, headers=headers, body=b"{}")
        if not authorized and self.refresh_profile_status != 200:
            return aiohttp.web.Response(status=self.refresh_profile_status, body=b"{}")
        headers = dict(self.recovery_headers if not authorized and self.recovery_headers else self.profile_headers)
        return self._json(
            {
                "payload": {
                    "partition": {
                        "partitionId": "other-partition",
                        "schoolName": "South Gym",
                        "timezone": "America/Denver",
                    },
                    "person": {
                        "personId": "ada",
                        "familyId": "fam-1",
                        "firstName": "Ada",
                        "lastName": "Lovelace",
                    },
                },
                "error": None,
            },
            headers=headers,
        )

    def _json(self, payload: dict, *, status: int = 200, headers: dict[str, str] | None = None):
        return aiohttp.web.Response(
            status=status,
            headers=headers,
            body=json.dumps(payload).encode(),
            content_type="application/json",
        )


class Entry:
    def __init__(self, data: dict, options: dict) -> None:
        self.data = dict(data)
        self.options = dict(options)
        self.entry_id = "entry-1"
        self.title = data.get("school_name", "South Gym")
        self.pref_disable_polling = True
        self.domain = DOMAIN
        self.reauth_started = False

    def async_on_unload(self, func):
        return func

    def async_start_reauth(self, hass) -> None:
        self.reauth_started = True


class _People:
    def async_get(self, entity_id: str):
        if entity_id == "calendar.ada_lovelace":
            return type("Row", (), {"unique_id": "calendar:part-1:ada"})()
        return None


class Entries:
    def __init__(self) -> None:
        self.updates: list[dict] = []

    def async_update_entry(self, entry: Entry, *, data=None, **kwargs) -> bool:
        if data is not None:
            entry.data = dict(data)
            self.updates.append(dict(data))
        return True


def class_row(
    class_id: str,
    *,
    membership: str | None = "mem-1",
    attendance: str | None = None,
    waitlist: str | None = None,
    reserved: bool = False,
    waitlisted: bool = False,
    reservable: bool = True,
    name: str = "Noon WOD",
) -> dict:
    return {
        "item": {"itemId": class_id, "name": name},
        "schedule": {"start": START, "end": END},
        "location": {"locationName": "South"},
        "primaryInstructor": {"primaryInstructorDisplayName": "Riley"},
        "membership": {} if membership is None else {"membershipId": membership},
        "reservation": {
            "attendanceId": attendance,
            "waitlistId": waitlist,
            "isReserved": reserved,
            "isWaitlisted": waitlisted,
        },
        "availability": {"isReservable": reservable},
    }


def account_data(**overrides) -> dict:
    data = {
        "username": "ada@example.com",
        "password": "secret-pass",
        "org_id": "part-1",
        "user_id": "user-1",
        "partition_id": "part-1",
        "school_name": "South Gym",
        "timezone_name": "America/Denver",
        "access_token": "access-a",
        "refresh_token": "refresh-a",
    }
    data.update(overrides)
    return data


class Lab:
    def __init__(self) -> None:
        self.api = Api()
        self.hass: HomeAssistant | None = None
        self.entry = Entry(account_data(), {"enabled_person_ids": ["ada"]})
        self.entries = Entries()
        self.gym: Gym | None = None
        self._runner: aiohttp.web.AppRunner | None = None
        self._origins: tuple[str, str] | None = None
        self._config_dir: Path | None = None

    async def start(self) -> None:
        app = aiohttp.web.Application()
        app.router.add_route("*", "/{tail:.*}", self.api.handle)
        self._runner = aiohttp.web.AppRunner(app)
        await self._runner.setup()
        site = aiohttp.web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        base = f"http://127.0.0.1:{port}"
        self._origins = (gym_mod._AUTH_ORIGIN, gym_mod._API_ORIGIN)
        gym_mod._AUTH_ORIGIN = base
        gym_mod._API_ORIGIN = base
        root = Path("/Users/maximillianhill/GitHub/vibed_zenplanner/.pytest_tmp")
        root.mkdir(exist_ok=True)
        self._config_dir = Path(tempfile.mkdtemp(prefix="zp-hass-", dir=root))
        self.hass = HomeAssistant(str(self._config_dir))
        await self.hass.async_start()
        self.hass.config_entries = self.entries
        self.hass.data[DATA_REGISTRY] = _People()

    async def close(self) -> None:
        if self._origins is not None:
            gym_mod._AUTH_ORIGIN, gym_mod._API_ORIGIN = self._origins
        if self.hass is not None:
            await self.hass.async_stop()
        if self._runner is not None:
            await self._runner.cleanup()

    def open_gym(self) -> Gym:
        assert self.hass is not None
        self.gym = Gym(self.hass, self.entry)
        self.hass.data.setdefault(DOMAIN, {})[self.entry.entry_id] = self.gym
        return self.gym

    def call(self, service: str, **data) -> ServiceCall:
        return ServiceCall(DOMAIN, service, data)

    def person_entity(self) -> str:
        return "calendar.ada_lovelace"


def run(test):
    async def wrapper():
        lab = Lab()
        await lab.start()
        try:
            await test(lab)
        finally:
            await lab.close()

    asyncio.run(wrapper())


def test_login_statuses_and_rotated_tokens_are_what_discover_returns() -> None:
    run(_login_statuses)


async def _login_statuses(lab: Lab) -> None:
    assert lab.hass is not None
    success = await gym_mod.discover(lab.hass, "ada@example.com", "secret-pass")
    assert isinstance(success, Authenticated)
    assert success.partition_id == "part-1"
    assert success.school_name == "South Gym"
    assert success.access_token == "access-a"
    assert [person.person_id for person in success.family] == [PersonId("ada")]
    assert lab.entries.updates == []
    profile_hits = lab.api.matching("/profiles/current")
    assert profile_hits
    assert profile_hits[0].headers["partitionid"] == "part-1"

    lab.api.hits.clear()
    lab.api.profile_headers = {"Authorization": "Bearer access-b", "X-Auth-Refresh": "refresh-b"}
    lab.api.family_headers = {"Authorization": "Bearer access-c", "X-Auth-Refresh": "refresh-c"}
    rotated = await gym_mod.discover(lab.hass, "ada@example.com", "secret-pass")
    assert isinstance(rotated, Authenticated)
    assert rotated.access_token == "access-c"
    assert rotated.refresh_token == "refresh-c"
    stored = initial_entry(rotated, "ada@example.com", "secret-pass")
    assert stored["access_token"] == "access-c"
    assert stored["refresh_token"] == "refresh-c"
    assert lab.entries.updates == []

    lab.api.organizations = [
        {"userId": "u1", "organization": {"id": "g1", "name": "South"}},
        {"userId": None, "organization": {"id": "g2", "name": "Drop me"}},
        {"userId": "u3", "organization": {"id": "g3", "schoolName": "North School"}},
        {"userId": "u4", "organization": {"id": "g5"}},
        {"organization": {"id": "g6", "name": "No user"}},
    ]
    lab.api.login_status = "SUCCESS_MULTIPLE_ORGS"
    lab.api.hits.clear()
    choice = await gym_mod.discover(lab.hass, "ada@example.com", "secret-pass")
    assert isinstance(choice, ChooseGym)
    assert [(item.org_id, item.user_id, item.label) for item in choice.organizations] == [
        ("g1", "u1", "South"),
        ("g3", "u3", "North School"),
        ("g5", "u4", "g5"),
    ]
    assert lab.api.matching("/profiles/current") == []

    for status, reason in (
        ("PASSWORD_EXPIRED", "password_expired"),
        ("FIRST_TIME_USER", "first_time_user"),
        ("LOCKED", "invalid_auth"),
    ):
        lab.api.login_status = status
        failed = await gym_mod.discover(lab.hass, "ada@example.com", "secret-pass")
        assert isinstance(failed, AuthFailed)
        assert failed.reason == reason


def test_poll_keeps_the_token_pair_from_the_last_response() -> None:
    run(_poll_tokens)


async def _poll_tokens(lab: Lab) -> None:
    lab.api.profile_headers = {"Authorization": "Bearer access-b", "X-Auth-Refresh": "refresh-b"}
    lab.api.family_headers = {"Authorization": "Bearer access-c", "X-Auth-Refresh": "refresh-c"}
    gym = lab.open_gym()
    await gym.async_refresh()
    assert lab.entry.data["access_token"] == "access-c"
    assert lab.entry.data["refresh_token"] == "refresh-c"
    assert gym.data is not None
    assert gym.data.family[0].display_name == "Ada Lovelace"

    lab.api.hits.clear()
    lab.entries.updates.clear()
    lab.api.family_headers = {"Authorization": "Bearer access-d", "X-Auth-Refresh": "refresh-d"}
    lab.api.bad_body_on.add("/family-members")
    await gym.async_refresh()
    assert lab.entry.data["access_token"] == "access-d"
    assert lab.entry.data["refresh_token"] == "refresh-d"
    assert gym.last_update_success is False


def test_unauthorized_profile_retries_with_refresh_then_password() -> None:
    run(_unauthorized)


async def _unauthorized(lab: Lab) -> None:
    lab.api.authorized_profile_401s = 1
    lab.api.rotate_refresh_on_401 = "refresh-b"
    lab.api.recovery_headers = {"Authorization": "Bearer access-new", "X-Auth-Refresh": "refresh-new"}
    gym = lab.open_gym()
    await gym.async_refresh()
    profiles = lab.api.matching("/profiles/current")
    assert len(profiles) == 3
    assert profiles[0].headers["authorization"] == "Bearer access-a"
    assert "authorization" not in profiles[1].headers
    assert profiles[1].headers["x-auth-refresh"] == "refresh-b"
    assert profiles[2].headers["authorization"] == "Bearer access-new"
    assert lab.api.matching("/auth/v1/login") == []
    assert any(item["access_token"] == "" for item in lab.entries.updates)
    assert lab.entry.data["access_token"] == "access-new"
    assert gym.data is not None

    lab.api.hits.clear()
    lab.entries.updates.clear()
    lab.api.authorized_profile_401s = 5
    lab.api.refresh_profile_status = 401
    lab.api.login_token = "access-login"
    lab.api.login_refresh = "refresh-login"
    lab.api.rotate_refresh_on_401 = "refresh-b"
    await gym.async_refresh()
    assert isinstance(gym.last_exception, ConfigEntryAuthFailed)
    assert lab.entry.reauth_started is True
    profiles = lab.api.matching("/profiles/current")
    logins = lab.api.matching("/auth/v1/login")
    assert len(logins) == 1
    assert logins[0].body["password"] == "secret-pass"
    assert logins[0].body["orgId"] == "part-1"
    assert logins[0].body["userId"] == "user-1"
    assert len(profiles) == 3
    assert "authorization" not in profiles[1].headers
    assert profiles[1].headers["x-auth-refresh"] == "refresh-b"
    assert any(item["access_token"] == "" for item in lab.entries.updates)


def test_second_reserve_of_a_held_seat_does_not_post() -> None:
    run(_second_reserve)


async def _second_reserve(lab: Lab) -> None:
    gym = lab.open_gym()
    cases = (
        (class_row("c1", attendance="att-1", reserved=True), Reserved),
        (class_row("c2", waitlist="wl-1", waitlisted=True), Waitlisted),
        (class_row("c3", reserved=True, waitlist="wl-ignored"), HeldUncancellable),
        (class_row("c4", waitlisted=True), HeldUncancellable),
    )
    for row, kind in cases:
        lab.api.hits.clear()
        lab.api.reservation_rows = [row]
        lab.api.class_rows = []
        outcome = await gym.reserve(PersonId("ada"), ClassId(row["item"]["itemId"]), join_waitlist=False)
        assert isinstance(outcome, kind)
        assert lab.api.matching("/member/calendars/reserve") == []
        assert lab.api.matching("/calendars/waitlist") == []
    lab.api.reservation_rows = [class_row("c2", waitlist="wl-1", waitlisted=True)]
    lab.api.hits.clear()
    outcome = await gym.reserve(PersonId("ada"), ClassId("c2"), join_waitlist=True)
    assert isinstance(outcome, Waitlisted)
    assert lab.api.matching("/member/calendars/reserve") == []


def test_open_seat_posts_reserve_and_waitlist_only_after_spots_full() -> None:
    run(_open_seat)


async def _open_seat(lab: Lab) -> None:
    gym = lab.open_gym()
    lab.api.class_rows = [class_row("c1", membership="mem-1", reservable=False)]
    outcome = await gym.reserve(PersonId("ada"), ClassId("c1"), join_waitlist=False)
    posted = lab.api.matching("/member/calendars/reserve")
    assert len(posted) == 1
    assert posted[0].body == {
        "personId": "ada",
        "appointmentId": "c1",
        "membershipId": "mem-1",
        "ignoreLimit": False,
    }
    assert posted[0].headers["componentversionid"] == "8D052224-6C15-4986-BF65-4ED646EAE8BF"
    assert posted[0].headers["appsource"] == "MEMBER_APP"
    assert posted[0].headers["partitionid"] == "part-1"
    assert posted[0].headers["authorization"] == "Bearer access-a"
    assert "charset=utf-8" in posted[0].headers["accept"]
    assert lab.api.matching("/calendars/waitlist") == []
    assert isinstance(outcome, Unavailable)
    assert outcome.reason == "not_found"

    lab.api.hits.clear()
    lab.api.reserve_status = "RESERVATION_FAILURE_SPOTS_FULL"
    spots = await gym.reserve(PersonId("ada"), ClassId("c1"), join_waitlist=False)
    assert isinstance(spots, Unavailable)
    assert spots.reason == "spots_full"
    assert lab.api.matching("/calendars/waitlist") == []

    lab.api.hits.clear()
    lab.api.waitlist_status = "WAITLISTED"
    original = lab.api._respond

    def respond(hit: Hit):
        if hit.path.endswith("/member/calendars/reserve"):
            lab.api.reservation_rows = [class_row("c1", waitlist="wl-9", waitlisted=True)]
        return original(hit)

    lab.api._respond = respond
    held = await gym.reserve(PersonId("ada"), ClassId("c1"), join_waitlist=True)
    assert isinstance(held, Waitlisted)
    assert len(lab.api.matching("/member/calendars/reserve")) == 1
    assert len(lab.api.matching("/calendars/waitlist")) == 1
    assert lab.api.matching("/calendars/waitlist")[0].body["membershipId"] == "mem-1"

    lab.api._respond = original
    lab.api.hits.clear()
    lab.api.reservation_rows = []
    lab.api.waitlist_status = "RESERVATION_FAILURE_WAITLIST_FULL"
    full = await gym.reserve(PersonId("ada"), ClassId("c1"), join_waitlist=True)
    assert isinstance(full, Unavailable)
    assert full.reason == "waitlist_full"


def test_cancel_is_idempotent_and_maps_hold_errors() -> None:
    run(_cancel)


async def _cancel(lab: Lab) -> None:
    gym = lab.open_gym()
    entity_id = lab.person_entity()

    lab.api.class_rows = [class_row("open", reservable=True)]
    lab.api.reservation_rows = []
    call = lab.call("cancel", entity_id=[entity_id], class_id="open")
    assert await async_handle_cancel(lab.hass, call) == {"result": "already_clear"}
    assert lab.api.matching("/calendars/reserve/") == []

    lab.api.hits.clear()
    missing = lab.call("cancel", entity_id=[entity_id], class_id="missing")
    assert await async_handle_cancel(lab.hass, missing) == {"result": "already_clear"}
    assert lab.api.matching("/calendars/reserve/") == []

    lab.api.hits.clear()
    lab.api.reservation_rows = [class_row("stuck", reserved=True, waitlist="wl-1")]
    stuck = lab.call("cancel", entity_id=[entity_id], class_id="stuck")
    with pytest.raises(ServiceValidationError):
        await async_handle_cancel(lab.hass, stuck)
    assert lab.api.matching("/calendars/reserve/") == []
    with pytest.raises(UnregisterUnavailable):
        await gym.cancel(PersonId("ada"), ClassId("stuck"))

    async def cleared_after_delete(error, status: int) -> None:
        lab.api.hits.clear()
        lab.api.delete_status = status
        lab.api.delete_error = error
        lab.api.reservation_rows = [class_row("c1", attendance="att-9", reserved=True)]

        def respond(hit: Hit):
            if hit.method == "DELETE":
                lab.api.reservation_rows = []
            return Api._respond(lab.api, hit)

        lab.api._respond = respond
        result = await async_handle_cancel(
            lab.hass, lab.call("cancel", entity_id=[entity_id], class_id="c1")
        )
        assert result == {"result": "cleared"}
        deletes = [hit for hit in lab.api.hits if hit.method == "DELETE"]
        assert len(deletes) == 1
        assert deletes[0].path.endswith("/calendars/reserve/att-9")
        assert deletes[0].raw == b""
        assert "personId" not in deletes[0].path

    await cleared_after_delete(None, 404)
    await cleared_after_delete("NOT_FOUND", 200)
    await cleared_after_delete({"code": "NOT_FOUND"}, 200)

    lab.api.hits.clear()
    lab.api.delete_status = 404
    lab.api.delete_error = None
    lab.api.reservation_rows = [class_row("c1", attendance="att-9", reserved=True)]
    lab.api._respond = Api._respond.__get__(lab.api, Api)
    with pytest.raises(HomeAssistantError):
        await async_handle_cancel(lab.hass, lab.call("cancel", entity_id=[entity_id], class_id="c1"))
    assert any(hit.method == "DELETE" for hit in lab.api.hits)
    with pytest.raises(HoldRemains):
        await gym.cancel(PersonId("ada"), ClassId("c1"))
