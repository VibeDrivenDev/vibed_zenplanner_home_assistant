"""One gym. This module owns HTTP, tokens, and the folded schedule."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, Literal
from urllib.parse import quote, urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import DOMAIN, SCAN_INTERVAL
from .models import (
    AlreadyClear,
    AlreadyHeld,
    AttemptFailure,
    AttendanceId,
    Authenticated,
    AuthFailed,
    AuthFailure,
    CannotCancel,
    ChooseGym,
    ClassId,
    ClassSession,
    DeleteAttendance,
    DeleteWaitlist,
    DiscoverResult,
    FamilyId,
    GiveUp,
    GymState,
    MembershipId,
    OpenSeat,
    OrgChoice,
    OrganizationId,
    PartitionId,
    Person,
    PersonAgenda,
    PersonId,
    ReserveOutcome,
    ReservedSeat,
    ReservedUncancellable,
    School,
    TimeWindow,
    UserId,
    WaitlistId,
    WaitlistedSeat,
    WaitlistedUncancellable,
    enabled_people,
    fold_agenda,
    horizon_for,
    merge_family,
    outcome_after_attempt,
    plan_cancel,
    plan_reserve,
)

_LOGGER = logging.getLogger(__name__)


def _log_auth(event: str, **details: str) -> None:
    """Record an auth failure. Details are labels such as a status or present/missing."""
    extra = " ".join(f"{key}={value}" for key, value in details.items())
    if extra:
        _LOGGER.warning("ZenPlanner auth %s %s", event, extra)
        return
    _LOGGER.warning("ZenPlanner auth %s", event)


def _status_label(status: object) -> str:
    if isinstance(status, str):
        return status[:80]
    if status is None:
        return "missing"
    return type(status).__name__


def _presence(value: object) -> str:
    return "present" if value else "missing"

_AUTH_ORIGIN = "https://memberappv220.zenplanner.com"
_API_ORIGIN = "https://memberappapiv220.zenplanner.com"
_COMPONENT_VERSION = "8D052224-6C15-4986-BF65-4ED646EAE8BF"
_TIMEOUT = aiohttp.ClientTimeout(total=30)

_Mode = Literal["api", "login", "refresh", "delete"]


class CannotConnect(Exception):
    """The host did not answer."""


class WrongGym(Exception):
    """Reauth returned a different partition than this config entry."""


class UnregisterUnavailable(Exception):
    """The hold has no id Home Assistant can delete."""


class HoldRemains(Exception):
    """The server still reports the hold after a delete."""


class ApiError(Exception):
    """The elements body was not a usable payload."""


@dataclass(frozen=True, slots=True)
class Account:
    username: str
    password: str
    org_id: OrganizationId
    user_id: UserId
    partition_id: PartitionId
    school_name: str
    timezone_name: str
    access_token: str
    refresh_token: str


@dataclass
class _Pair:
    account: Account


_Commit = Callable[[Account], Awaitable[None]]


def _zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (KeyError, ValueError, ZoneInfoNotFoundError):
        return ZoneInfo("US/Mountain")


def _timezone_name(raw: object) -> str:
    text = str(raw or "")
    try:
        ZoneInfo(text)
    except (KeyError, ValueError, ZoneInfoNotFoundError):
        return "US/Mountain"
    return text


def _account_data(account: Account) -> dict[str, str]:
    return {
        "username": account.username,
        "password": account.password,
        "org_id": account.org_id,
        "user_id": account.user_id,
        "partition_id": account.partition_id,
        "school_name": account.school_name,
        "timezone_name": account.timezone_name,
        "access_token": account.access_token,
        "refresh_token": account.refresh_token,
    }


def account_from_entry(entry: ConfigEntry) -> Account:
    """Parse entry data. An invalid timezone becomes US/Mountain. Missing tokens raise."""
    data = entry.data
    refresh = data.get("refresh_token")
    if "access_token" not in data or not refresh:
        raise ValueError("missing tokens")
    return Account(
        username=str(data.get("username") or ""),
        password=str(data.get("password") or ""),
        org_id=OrganizationId(str(data.get("org_id") or "")),
        user_id=UserId(str(data.get("user_id") or "")),
        partition_id=PartitionId(str(data.get("partition_id") or "")),
        school_name=str(data.get("school_name") or ""),
        timezone_name=_timezone_name(data.get("timezone_name")),
        access_token=str(data.get("access_token") or ""),
        refresh_token=str(refresh),
    )


def enabled_ids(entry: ConfigEntry) -> frozenset[PersonId]:
    raw = entry.options.get("enabled_person_ids", [])
    if not isinstance(raw, list):
        return frozenset()
    return frozenset(PersonId(item) for item in raw if isinstance(item, str) and item)


def initial_entry(auth: Authenticated, username: str, password: str) -> dict[str, object]:
    """The only dict shape async_create_entry may store."""
    return _account_data(
        Account(
            username=username,
            password=password,
            org_id=auth.org_id,
            user_id=auth.user_id,
            partition_id=auth.partition_id,
            school_name=auth.school_name,
            timezone_name=auth.timezone_name,
            access_token=auth.access_token,
            refresh_token=auth.refresh_token,
        )
    )


def initial_options(enabled: frozenset[PersonId]) -> dict[str, list[str]]:
    """enabled_person_ids, sorted, so the options blob stays stable."""
    return {"enabled_person_ids": sorted(enabled)}


def _login_body(username: str, password: str, org_id: str, user_id: str | None) -> dict[str, object]:
    return {
        "orgId": org_id,
        "username": username,
        "password": password,
        "userId": user_id or None,
        "requiredRoles": ["PORTAL"],
        "requiredType": None,
        "temporaryAuth": False,
        "authCheckOnly": False,
    }


def _auth_url() -> str:
    return f"{_AUTH_ORIGIN}/auth/v1/login"


def _api_url(path: str) -> str:
    return f"{_API_ORIGIN}/elements/api-v2/{path}"


def _bearer(value: str) -> str:
    text = value.strip()
    if text.lower().startswith("bearer "):
        return text[7:].strip()
    return text


class _Api:
    """HTTP for one token pair. Constructed by discover and by Gym."""

    def __init__(self, hass: HomeAssistant, pair: _Pair, commit: _Commit | None) -> None:
        self._pair = pair
        self._commit = commit
        self._session = async_get_clientsession(hass)

    @property
    def account(self) -> Account:
        return self._pair.account

    async def _save(self, account: Account) -> None:
        if account == self._pair.account:
            return
        if self._commit is not None:
            await self._commit(account)
            return
        self._pair.account = account

    def _headers(self, mode: _Mode) -> dict[str, str]:
        account = self._pair.account
        headers = {
            "Accept": "application/json; charset=utf-8",
            "Content-Type": "application/json",
            "ComponentVersionId": _COMPONENT_VERSION,
            "appSource": "MEMBER_APP",
        }
        if mode == "login":
            return headers
        if account.partition_id:
            headers["PartitionId"] = account.partition_id
        if mode == "refresh":
            headers["X-Auth-Refresh"] = account.refresh_token
            return headers
        if account.access_token:
            headers["Authorization"] = f"Bearer {account.access_token}"
        if account.refresh_token:
            headers["X-Auth-Refresh"] = account.refresh_token
        return headers

    async def _take_headers(self, response: aiohttp.ClientResponse) -> tuple[bool, bool]:
        """Store rotated tokens before the body is read.

        A crash while parsing then restarts with the pair the server already issued.
        """
        account = self._pair.account
        access = account.access_token
        refresh = account.refresh_token
        saw_access = False
        saw_refresh = False
        header_access = response.headers.get("authorization")
        header_refresh = response.headers.get("X-Auth-Refresh")
        if header_access:
            saw_access = True
            access = _bearer(header_access)
        if header_refresh:
            saw_refresh = True
            refresh = header_refresh
        await self._save(replace(account, access_token=access, refresh_token=refresh))
        return saw_access, saw_refresh

    async def _exchange(
        self,
        method: str,
        url: str,
        json_body: dict[str, object] | None,
        mode: _Mode,
    ) -> tuple[int, bytes, bool, bool]:
        header_mode: _Mode = "login" if mode == "login" else "refresh" if mode == "refresh" else "api"
        try:
            async with self._session.request(
                method,
                url,
                headers=self._headers(header_mode),
                json=json_body,
                timeout=_TIMEOUT,
            ) as response:
                saw_access, saw_refresh = await self._take_headers(response)
                if response.status == 401 or (response.status == 404 and mode == "delete"):
                    return response.status, b"", saw_access, saw_refresh
                return response.status, await response.read(), saw_access, saw_refresh
        except aiohttp.ClientError as err:
            raise CannotConnect("ZenPlanner did not answer") from err
        except TimeoutError as err:
            raise CannotConnect("ZenPlanner did not answer") from err

    async def _request(
        self,
        method: str,
        url: str,
        *,
        json_body: dict[str, object] | None = None,
        mode: _Mode = "api",
        allow_reauth: bool = True,
    ) -> Any:
        status, raw, saw_access, saw_refresh = await self._exchange(method, url, json_body, mode)
        if status == 401 and allow_reauth and mode in ("api", "delete"):
            await self._recover()
            return await self._request(
                method,
                url,
                json_body=json_body,
                mode=mode,
                allow_reauth=False,
            )
        if status == 401:
            _log_auth("http_401", method=method, path=urlparse(url).path, mode=mode)
            raise ConfigEntryAuthFailed("ZenPlanner denied the token")
        if status == 404 and mode == "delete":
            return None
        if status >= 500:
            raise CannotConnect("ZenPlanner did not answer")
        parsed = _parse_json(raw)
        if mode == "login":
            await self._apply_login_body(parsed, saw_access, saw_refresh)
            if status >= 400:
                raise CannotConnect("ZenPlanner did not answer")
            return parsed
        if status >= 400:
            raise ApiError(f"HTTP {status}")
        if mode == "delete" and _error_code(_error_field(parsed)) == "NOT_FOUND":
            return None
        _raise_elements(parsed)
        return parsed

    async def _apply_login_body(self, parsed: object, saw_access: bool, saw_refresh: bool) -> None:
        if not isinstance(parsed, dict):
            return
        account = self._pair.account
        access = account.access_token
        refresh = account.refresh_token
        token = parsed.get("token")
        rotated = parsed.get("refreshToken")
        if not saw_access and isinstance(token, str):
            access = token
        if not saw_refresh and isinstance(rotated, str):
            refresh = rotated
        await self._save(replace(account, access_token=access, refresh_token=refresh))

    async def _recover(self) -> None:
        await self._save(replace(self._pair.account, access_token=""))
        try:
            await self._request(
                "GET",
                _api_url("profiles/current"),
                mode="refresh",
                allow_reauth=False,
            )
        except (ConfigEntryAuthFailed, CannotConnect, ApiError):
            await self._password_login()

    async def _password_login(self) -> None:
        account = self._pair.account
        body = await self._request(
            "POST",
            _auth_url(),
            json_body=_login_body(
                account.username,
                account.password,
                account.org_id,
                account.user_id or None,
            ),
            mode="login",
            allow_reauth=False,
        )
        status = body.get("status") if isinstance(body, dict) else None
        if status != "SUCCESS":
            _log_auth("password_login", status=_status_label(status))
            raise ConfigEntryAuthFailed("ZenPlanner denied the password login")

    async def login(self, org_id: str, user_id: str | None) -> Any:
        account = self._pair.account
        return await self._request(
            "POST",
            _auth_url(),
            json_body=_login_body(account.username, account.password, org_id, user_id),
            mode="login",
            allow_reauth=False,
        )

    async def adopt_org(self, choice: OrgChoice) -> None:
        await self._save(
            replace(
                self._pair.account,
                org_id=choice.org_id,
                user_id=choice.user_id,
                partition_id=PartitionId(str(choice.org_id)),
            )
        )

    async def profile(self) -> tuple[str, str, Person]:
        return _parse_profile(await self._request("GET", _api_url("profiles/current")))

    async def family_members(self, family_id: FamilyId) -> tuple[Person, ...]:
        body = await self._request(
            "GET",
            _api_url(f"families/{quote(family_id, safe='')}/family-members"),
        )
        return _parse_family(body, family_id)

    async def classes(
        self, person_id: PersonId, window: TimeWindow, zone: ZoneInfo
    ) -> tuple[ClassSession, ...]:
        body = await self._request(
            "POST",
            _api_url("calendars/classes"),
            json_body={
                "personId": person_id,
                "beginDate": window.start.isoformat(),
                "endDate": window.end.isoformat(),
            },
        )
        return _sessions(_class_rows(body), zone)

    async def reservations(self, person_id: PersonId, zone: ZoneInfo) -> tuple[ClassSession, ...]:
        body = await self._request(
            "POST",
            _api_url("calendars/reservations"),
            json_body={"personId": person_id},
        )
        return _sessions(_list_rows(body), zone)

    async def post_reserve(self, body: dict[str, object]) -> AttemptFailure | None:
        parsed = await self._request("POST", _api_url("member/calendars/reserve"), json_body=body)
        return _reservation_failure(parsed)

    async def post_waitlist(self, body: dict[str, object]) -> AttemptFailure | None:
        parsed = await self._request("POST", _api_url("calendars/waitlist"), json_body=body)
        return _reservation_failure(parsed)

    async def delete_attendance(self, attendance_id: AttendanceId) -> None:
        await self._request(
            "DELETE",
            _api_url(f"calendars/reserve/{quote(attendance_id, safe='')}"),
            mode="delete",
        )

    async def delete_waitlist(self, waitlist_id: WaitlistId) -> None:
        await self._request(
            "DELETE",
            _api_url(f"calendars/waitlist/{quote(waitlist_id, safe='')}"),
            mode="delete",
        )


def _blank_account(username: str, password: str) -> Account:
    return Account(
        username=username,
        password=password,
        org_id=OrganizationId(""),
        user_id=UserId(""),
        partition_id=PartitionId(""),
        school_name="",
        timezone_name="US/Mountain",
        access_token="",
        refresh_token="",
    )


async def discover(
    hass: HomeAssistant,
    username: str,
    password: str,
    selected: OrgChoice | None = None,
) -> DiscoverResult:
    """Login, then load the profile and the family when the gym is known.

    Does not write a config entry. The returned tokens are the pair after those calls.
    """
    api = _Api(hass, _Pair(_blank_account(username, password)), None)
    org_id = "" if selected is None else selected.org_id
    user_id = None if selected is None else selected.user_id
    try:
        body = await api.login(org_id, user_id)
    except CannotConnect:
        raise
    except ConfigEntryAuthFailed:
        _log_auth("login_rejected")
        return AuthFailed("invalid_auth")
    except ApiError as err:
        raise CannotConnect("ZenPlanner did not answer") from err
    if not isinstance(body, dict):
        raise CannotConnect("ZenPlanner did not answer")
    status = body.get("status")
    if status == "SUCCESS_MULTIPLE_ORGS":
        choices = _org_choices(body)
        if not choices:
            _log_auth("multiple_orgs_unreadable")
        return ChooseGym(choices)
    if status != "SUCCESS":
        _log_auth("login_status", status=_status_label(status))
        if status == "PASSWORD_EXPIRED":
            return AuthFailed("password_expired")
        if status == "FIRST_TIME_USER":
            return AuthFailed("first_time_user")
        return AuthFailed("invalid_auth")
    choice = _first_org(body)
    if choice is None or not api.account.access_token or not api.account.refresh_token:
        _log_auth(
            "login_account_unusable",
            gym=_presence(choice),
            access_token=_presence(api.account.access_token),
            refresh_token=_presence(api.account.refresh_token),
        )
        return AuthFailed("invalid_auth")
    try:
        await api.adopt_org(choice)
        school_name, timezone_name, person = await api.profile()
        members = await api.family_members(person.family_id)
    except ConfigEntryAuthFailed:
        _log_auth("profile_or_family_rejected")
        return AuthFailed("invalid_auth")
    except CannotConnect:
        raise
    except ApiError as err:
        raise CannotConnect("ZenPlanner did not answer") from err
    account = api.account
    if not account.access_token or not account.refresh_token:
        _log_auth(
            "tokens_missing_after_profile",
            access_token=_presence(account.access_token),
            refresh_token=_presence(account.refresh_token),
        )
        return AuthFailed("invalid_auth")
    return Authenticated(
        partition_id=PartitionId(str(choice.org_id)),
        org_id=choice.org_id,
        user_id=choice.user_id,
        access_token=account.access_token,
        refresh_token=account.refresh_token,
        school_name=school_name or choice.label or "ZenPlanner",
        timezone_name=_timezone_name(timezone_name),
        person=person,
        family=merge_family(person, members),
    )


class Gym(DataUpdateCoordinator[GymState]):
    """One gym, one token pair, one schedule, one lock."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        account = account_from_entry(entry)
        self.entry = entry
        self._pair = _Pair(account)
        self._lock = asyncio.Lock()
        self._people_ids = enabled_ids(entry)
        self._api = _Api(hass, self._pair, self._commit)
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=account.school_name or "ZenPlanner",
            update_interval=SCAN_INTERVAL,
        )

    async def _commit(self, account: Account) -> None:
        self.hass.config_entries.async_update_entry(self.entry, data=_account_data(account))
        self._pair.account = account

    @property
    def partition_id(self) -> PartitionId:
        return self._pair.account.partition_id

    @property
    def people_ids(self) -> frozenset[PersonId]:
        return self._people_ids

    def family(self) -> tuple[Person, ...]:
        if self.data is None:
            return ()
        return self.data.family

    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self.partition_id)},
            name=self._pair.account.school_name,
        )

    def _school(self) -> School:
        account = self._pair.account
        return School(account.partition_id, account.school_name, _zone(account.timezone_name))

    async def _async_update_data(self) -> GymState:
        async with self._lock:
            try:
                return await self._load_state()
            except ConfigEntryAuthFailed:
                raise
            except CannotConnect as err:
                raise UpdateFailed("ZenPlanner did not answer") from err
            except ApiError as err:
                raise UpdateFailed(str(err)) from err

    async def _load_state(self) -> GymState:
        school_name, timezone_name, person = await self._api.profile()
        members = await self._api.family_members(person.family_id)
        family = merge_family(person, members)
        account = self._pair.account
        school = School(
            account.partition_id,
            school_name or account.school_name,
            _zone(timezone_name or account.timezone_name),
        )
        horizon = horizon_for(school.now(), school.timezone)
        agendas: dict[PersonId, PersonAgenda] = {}
        for member in enabled_people(family, enabled_ids(self.entry)):
            reserved = await self._api.reservations(member.person_id, school.timezone)
            upcoming = await self._api.classes(member.person_id, horizon, school.timezone)
            agendas[member.person_id] = fold_agenda(member, reserved, upcoming, horizon)
        return GymState(school, family, agendas)

    async def _reload_person(self, person_id: PersonId) -> GymState:
        current = self.data
        school = current.school if current is not None else self._school()
        family = current.family if current is not None else ()
        person = next((item for item in family if item.person_id == person_id), None)
        if person is None:
            person = Person(person_id, FamilyId(""), "", "")
        horizon = horizon_for(school.now(), school.timezone)
        reserved = await self._api.reservations(person_id, school.timezone)
        upcoming = await self._api.classes(person_id, horizon, school.timezone)
        agenda = fold_agenda(person, reserved, upcoming, horizon)
        agendas = dict(current.agendas) if current is not None else {}
        agendas[person_id] = agenda
        return GymState(school, family, agendas)

    async def reserve(
        self,
        person_id: PersonId,
        class_id: ClassId,
        *,
        join_waitlist: bool,
    ) -> ReserveOutcome:
        async with self._lock:
            state = await self._reload_person(person_id)
            plan = plan_reserve(state.agenda_for(person_id), class_id, join_waitlist)
            if isinstance(plan, (AlreadyHeld, GiveUp)):
                self.async_set_updated_data(state)
                return plan.outcome
            body = {
                "personId": person_id,
                "appointmentId": class_id,
                "membershipId": plan.membership_id,
                "ignoreLimit": False,
            }
            failure = await self._api.post_reserve(body)
            if failure == "spots_full" and plan.join_waitlist_if_full:
                failure = await self._api.post_waitlist(body)
            state = await self._reload_person(person_id)
            self.async_set_updated_data(state)
            stored = state.agenda_for(person_id).sessions.get(class_id)
            return outcome_after_attempt(stored, failure)

    async def cancel(self, person_id: PersonId, class_id: ClassId) -> Literal["cleared", "already_clear"]:
        async with self._lock:
            state = await self._reload_person(person_id)
            plan = plan_cancel(state.agenda_for(person_id), class_id)
            self.async_set_updated_data(state)
            if isinstance(plan, AlreadyClear):
                return "already_clear"
            if isinstance(plan, CannotCancel):
                raise UnregisterUnavailable("Home Assistant cannot cancel this seat")
            if isinstance(plan, DeleteAttendance):
                await self._api.delete_attendance(plan.attendance_id)
            else:
                await self._api.delete_waitlist(plan.waitlist_id)
            state = await self._reload_person(person_id)
            self.async_set_updated_data(state)
            stored = state.agenda_for(person_id).sessions.get(class_id)
            if stored is not None and not isinstance(stored.seat, OpenSeat):
                raise HoldRemains("ZenPlanner still shows this seat")
            return "cleared"

    async def reauthenticate(self, password: str) -> AuthFailure | None:
        async with self._lock:
            account = self._pair.account
            result = await discover(
                self.hass,
                account.username,
                password,
                OrgChoice(account.org_id, account.user_id, account.school_name),
            )
            if isinstance(result, ChooseGym):
                raise WrongGym("ZenPlanner asked for a different gym")
            if isinstance(result, AuthFailed):
                return result.reason
            if result.partition_id != account.partition_id:
                raise WrongGym("ZenPlanner returned a different gym")
            await self._commit(
                Account(
                    username=account.username,
                    password=password,
                    org_id=result.org_id,
                    user_id=result.user_id,
                    partition_id=result.partition_id,
                    school_name=result.school_name,
                    timezone_name=result.timezone_name,
                    access_token=result.access_token,
                    refresh_token=result.refresh_token,
                )
            )
            self.async_set_updated_data(await self._load_state())
            return None


def _parse_json(raw: bytes) -> Any:
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError as err:
        raise ApiError("invalid json") from err


def _error_field(parsed: object) -> object:
    if isinstance(parsed, dict):
        return parsed.get("error")
    return None


def _error_code(error: object) -> str | None:
    if error is None or error is False or error == "" or error == 0:
        return None
    if isinstance(error, str):
        return error
    if isinstance(error, dict):
        for key in ("code", "errorCode", "status", "name", "message"):
            value = error.get(key)
            if isinstance(value, str) and value:
                return value
        nested = error.get("errors")
        if isinstance(nested, list):
            for item in nested:
                code = _error_code(item)
                if code:
                    return code
    return str(error)


def _raise_elements(parsed: object) -> None:
    if not isinstance(parsed, dict) or "error" not in parsed:
        return
    code = _error_code(parsed.get("error"))
    if code is None:
        return
    raise ApiError(code)


def _class_rows(body: object) -> list[object]:
    if not isinstance(body, dict):
        raise ApiError("classes")
    payload = body.get("payload")
    if isinstance(payload, dict) and isinstance(payload.get("classes"), list):
        return payload["classes"]
    if isinstance(body.get("payloadArray"), list):
        return body["payloadArray"]
    if isinstance(payload, list):
        return payload
    if isinstance(body.get("classes"), list):
        return body["classes"]
    raise ApiError("classes")


def _list_rows(body: object) -> list[object]:
    if not isinstance(body, dict):
        raise ApiError("list")
    if isinstance(body.get("payloadArray"), list):
        return body["payloadArray"]
    payload = body.get("payload")
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict) and isinstance(payload.get("classes"), list):
        return payload["classes"]
    raise ApiError("list")


def _parse_profile(body: object) -> tuple[str, str, Person]:
    if not isinstance(body, dict):
        raise ApiError("profile")
    payload = body.get("payload")
    if not isinstance(payload, dict):
        raise ApiError("profile")
    partition = payload.get("partition") if isinstance(payload.get("partition"), dict) else {}
    person_row = payload.get("person") if isinstance(payload.get("person"), dict) else {}
    person_id = person_row.get("personId")
    if not person_id:
        raise ApiError("profile")
    person = Person(
        PersonId(str(person_id)),
        FamilyId(str(person_row.get("familyId") or "")),
        str(person_row.get("firstName") or ""),
        str(person_row.get("lastName") or ""),
    )
    return (
        str(partition.get("schoolName") or ""),
        str(partition.get("timezone") or "US/Mountain"),
        person,
    )


def _parse_family(body: object, fallback: FamilyId) -> tuple[Person, ...]:
    people: list[Person] = []
    for row in _list_rows(body):
        if not isinstance(row, dict) or not row.get("personId"):
            continue
        people.append(
            Person(
                PersonId(str(row["personId"])),
                FamilyId(str(row.get("familyId") or fallback)),
                str(row.get("firstName") or ""),
                str(row.get("lastName") or ""),
            )
        )
    return tuple(people)


def _parse_time(value: object, zone: ZoneInfo) -> datetime | None:
    if not isinstance(value, str) or value == "":
        return None
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=zone)
    return parsed.astimezone(zone)


def _sessions(rows: list[object], zone: ZoneInfo) -> tuple[ClassSession, ...]:
    parsed: list[ClassSession] = []
    for row in rows:
        session = _session(row, zone)
        if session is not None:
            parsed.append(session)
    return tuple(parsed)


def _session(row: object, zone: ZoneInfo) -> ClassSession | None:
    if not isinstance(row, dict):
        return None
    item = row.get("item") if isinstance(row.get("item"), dict) else {}
    schedule = row.get("schedule") if isinstance(row.get("schedule"), dict) else {}
    class_id = item.get("itemId")
    start = _parse_time(schedule.get("start"), zone)
    end = _parse_time(schedule.get("end"), zone)
    if not class_id or start is None or end is None:
        return None
    location = row.get("location") if isinstance(row.get("location"), dict) else {}
    instructor = row.get("primaryInstructor") if isinstance(row.get("primaryInstructor"), dict) else {}
    membership = row.get("membership") if isinstance(row.get("membership"), dict) else {}
    reservation = row.get("reservation") if isinstance(row.get("reservation"), dict) else {}
    availability = row.get("availability") if isinstance(row.get("availability"), dict) else {}
    membership_raw = membership.get("membershipId")
    return ClassSession(
        class_id=ClassId(str(class_id)),
        name=str(item.get("name") or ""),
        window=TimeWindow(start, end),
        location=str(location.get("locationName") or ""),
        instructor=str(instructor.get("primaryInstructorDisplayName") or ""),
        membership_id=None if not membership_raw else MembershipId(str(membership_raw)),
        seat=_seat(reservation, availability),
    )


def _seat(reservation: dict[str, Any], availability: dict[str, Any]):
    attendance = _id(reservation.get("attendanceId"))
    waitlist = _id(reservation.get("waitlistId"))
    is_reserved = bool(reservation.get("isReserved"))
    is_waitlisted = bool(reservation.get("isWaitlisted"))
    if attendance:
        return ReservedSeat(AttendanceId(attendance))
    # A reserved flag with no attendance id has no cancel URL, so it beats a waitlist id.
    if is_reserved:
        return ReservedUncancellable()
    if waitlist:
        return WaitlistedSeat(WaitlistId(waitlist))
    if is_waitlisted:
        return WaitlistedUncancellable()
    return OpenSeat(bool(availability.get("isReservable")))


def _id(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if text == "":
        return None
    return text


def _reservation_failure(body: object) -> AttemptFailure | None:
    status = None
    if isinstance(body, dict):
        payload = body.get("payload")
        if isinstance(payload, dict) and isinstance(payload.get("reservationStatus"), str):
            status = payload["reservationStatus"]
        elif isinstance(body.get("reservationStatus"), str):
            status = body["reservationStatus"]
    if status in ("RESERVED", "WAITLISTED"):
        return None
    if status == "RESERVATION_FAILURE_SPOTS_FULL":
        return "spots_full"
    if status == "RESERVATION_FAILURE_WAITLIST_FULL":
        return "waitlist_full"
    return "not_found"


def _org_choices(body: dict[str, Any]) -> tuple[OrgChoice, ...]:
    users = body.get("organizationUsers")
    if not isinstance(users, list):
        return ()
    choices = [choice for row in users if (choice := _one_org(row)) is not None]
    return tuple(choices)


def _first_org(body: dict[str, Any]) -> OrgChoice | None:
    users = body.get("organizationUsers")
    if not isinstance(users, list) or not users:
        return None
    return _one_org(users[0])


def _one_org(row: object) -> OrgChoice | None:
    if not isinstance(row, dict):
        return None
    org = row.get("organization")
    if not isinstance(org, dict):
        return None
    org_id = org.get("id")
    user_id = row.get("userId")
    if not org_id or not user_id:
        return None
    label = org.get("name") or org.get("schoolName") or str(org_id)
    return OrgChoice(OrganizationId(str(org_id)), UserId(str(user_id)), str(label))
