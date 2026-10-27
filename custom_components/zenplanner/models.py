"""Schedule, seats, and the reserve and cancel decisions.

This module imports nothing from Home Assistant. Wire JSON is parsed in gym.py.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from typing import Literal, Mapping, NewType
from zoneinfo import ZoneInfo

from .const import ATTRIBUTE_CAP, OPEN_CLASS_HORIZON

PersonId = NewType("PersonId", str)
FamilyId = NewType("FamilyId", str)
PartitionId = NewType("PartitionId", str)
ClassId = NewType("ClassId", str)
AttendanceId = NewType("AttendanceId", str)
WaitlistId = NewType("WaitlistId", str)
MembershipId = NewType("MembershipId", str)
OrganizationId = NewType("OrganizationId", str)
UserId = NewType("UserId", str)

type AuthFailure = Literal["invalid_auth", "password_expired", "first_time_user"]
type EntityKind = Literal["calendar", "upcoming"]
type SeatWord = Literal[
    "open",
    "reserved",
    "waitlisted",
    "reserved_uncancellable",
    "waitlisted_uncancellable",
]
type OutcomeWord = Literal[
    "reserved",
    "waitlisted",
    "held",
    "spots_full",
    "waitlist_full",
    "not_found",
]
type AttemptFailure = Literal["spots_full", "waitlist_full", "not_found"]


class PublicClass(dict):
    """JSON object for one class. Dict so service responses stay plain JSON."""


class OutcomePayload(dict):
    """PublicClass fields plus result. Null class fields when the class is missing."""


@dataclass(frozen=True, slots=True)
class Person:
    person_id: PersonId
    family_id: FamilyId
    first_name: str
    last_name: str

    @property
    def display_name(self) -> str:
        parts = [part for part in (self.first_name.strip(), self.last_name.strip()) if part]
        return " ".join(parts) if parts else "Member"


@dataclass(frozen=True, slots=True)
class School:
    partition_id: PartitionId
    name: str
    timezone: ZoneInfo

    def now(self) -> datetime:
        return datetime.now(self.timezone)


@dataclass(frozen=True, slots=True)
class TimeWindow:
    """Aware start inclusive, end exclusive, in the school timezone."""

    start: datetime
    end: datetime

    def overlaps(self, start: datetime, end: datetime) -> bool:
        return self.start < end and start < self.end

    def contains_start(self, start: datetime) -> bool:
        return self.start <= start < self.end


@dataclass(frozen=True, slots=True)
class OpenSeat:
    reservable: bool


@dataclass(frozen=True, slots=True)
class ReservedSeat:
    attendance_id: AttendanceId


@dataclass(frozen=True, slots=True)
class WaitlistedSeat:
    waitlist_id: WaitlistId


@dataclass(frozen=True, slots=True)
class ReservedUncancellable:
    pass


@dataclass(frozen=True, slots=True)
class WaitlistedUncancellable:
    pass


type Seat = (
    OpenSeat
    | ReservedSeat
    | WaitlistedSeat
    | ReservedUncancellable
    | WaitlistedUncancellable
)


@dataclass(frozen=True, slots=True)
class ClassSession:
    class_id: ClassId
    name: str
    window: TimeWindow
    location: str
    instructor: str
    membership_id: MembershipId | None
    seat: Seat


@dataclass(frozen=True, slots=True)
class PersonAgenda:
    """One person. sessions is the only copy of their classes.

    A reservation row outside horizon stays in sessions.
    An open class exists only inside horizon.
    """

    person: Person
    horizon: TimeWindow
    sessions: Mapping[ClassId, ClassSession]

    def during_horizon(self, now: datetime) -> tuple[ClassSession, ...]:
        rows = [
            session
            for session in self.sessions.values()
            if self.horizon.contains_start(session.window.start) and session.window.end > now
        ]
        return tuple(sorted(rows, key=_by_start))

    def held_between(self, start: datetime, end: datetime) -> tuple[ClassSession, ...]:
        rows = [
            session
            for session in self.sessions.values()
            if _is_hold(session.seat) and session.window.overlaps(start, end)
        ]
        return tuple(sorted(rows, key=_by_start))

    def next_held(self, now: datetime) -> ClassSession | None:
        rows = [
            session
            for session in self.sessions.values()
            if _is_hold(session.seat) and session.window.end > now
        ]
        if not rows:
            return None
        return min(rows, key=_by_start)

    def open_in_horizon(self, now: datetime) -> tuple[ClassSession, ...]:
        return tuple(
            session
            for session in self.during_horizon(now)
            if isinstance(session.seat, OpenSeat)
        )

    def next_open(self, now: datetime) -> ClassSession | None:
        rows = self.open_in_horizon(now)
        if not rows:
            return None
        return rows[0]


@dataclass(frozen=True, slots=True)
class OpenClassView:
    state: str
    count: int
    classes: tuple[PublicClass, ...]


@dataclass(frozen=True, slots=True)
class GymState:
    school: School
    family: tuple[Person, ...]
    agendas: Mapping[PersonId, PersonAgenda]

    def agenda_for(self, person_id: PersonId) -> PersonAgenda:
        found = self.agendas.get(person_id)
        if found is not None:
            return found
        return PersonAgenda(
            person=Person(person_id, FamilyId(""), "", ""),
            horizon=horizon_for(self.school.now(), self.school.timezone),
            sessions={},
        )


@dataclass(frozen=True, slots=True)
class Reserved:
    session: ClassSession


@dataclass(frozen=True, slots=True)
class Waitlisted:
    session: ClassSession


@dataclass(frozen=True, slots=True)
class HeldUncancellable:
    session: ClassSession


@dataclass(frozen=True, slots=True)
class Unavailable:
    reason: AttemptFailure
    session: ClassSession | None


type ReserveOutcome = Reserved | Waitlisted | HeldUncancellable | Unavailable


@dataclass(frozen=True, slots=True)
class AlreadyHeld:
    outcome: Reserved | Waitlisted | HeldUncancellable


@dataclass(frozen=True, slots=True)
class PostReserve:
    """POST reserve. Waitlist only after that post says the class is full."""

    membership_id: MembershipId
    join_waitlist_if_full: bool


@dataclass(frozen=True, slots=True)
class GiveUp:
    outcome: Unavailable


type ReservePlan = AlreadyHeld | PostReserve | GiveUp


@dataclass(frozen=True, slots=True)
class DeleteAttendance:
    attendance_id: AttendanceId


@dataclass(frozen=True, slots=True)
class DeleteWaitlist:
    waitlist_id: WaitlistId


@dataclass(frozen=True, slots=True)
class AlreadyClear:
    pass


@dataclass(frozen=True, slots=True)
class CannotCancel:
    pass


type CancelPlan = DeleteAttendance | DeleteWaitlist | AlreadyClear | CannotCancel


@dataclass(frozen=True, slots=True)
class OrgChoice:
    org_id: OrganizationId
    user_id: UserId
    label: str


@dataclass(frozen=True, slots=True)
class Authenticated:
    partition_id: PartitionId
    org_id: OrganizationId
    user_id: UserId
    access_token: str
    refresh_token: str
    school_name: str
    timezone_name: str
    person: Person
    family: tuple[Person, ...]


@dataclass(frozen=True, slots=True)
class ChooseGym:
    organizations: tuple[OrgChoice, ...]


@dataclass(frozen=True, slots=True)
class AuthFailed:
    reason: AuthFailure


type DiscoverResult = Authenticated | ChooseGym | AuthFailed


@dataclass(frozen=True, slots=True)
class HeldEvent:
    uid: str
    summary: str
    start: datetime
    end: datetime
    location: str
    description: str


def merge_family(account_holder: Person, members: tuple[Person, ...]) -> tuple[Person, ...]:
    """Account holder first when members lacks that person id. Later duplicates drop."""
    known = {member.person_id for member in members}
    ordered = members if account_holder.person_id in known else (account_holder, *members)
    seen: set[PersonId] = set()
    unique: list[Person] = []
    for person in ordered:
        if person.person_id in seen:
            continue
        seen.add(person.person_id)
        unique.append(person)
    return tuple(unique)


def fold_agenda(
    person: Person,
    reserved: tuple[ClassSession, ...],
    upcoming: tuple[ClassSession, ...],
    horizon: TimeWindow,
) -> PersonAgenda:
    """Build one map. The previous agenda is not an input."""
    upcoming_by_id: dict[ClassId, ClassSession] = {}
    for session in upcoming:
        upcoming_by_id[session.class_id] = session
    sessions: dict[ClassId, ClassSession] = {}
    for session in upcoming:
        if horizon.contains_start(session.window.start):
            sessions[session.class_id] = session
    for session in reserved:
        prior = upcoming_by_id.get(session.class_id)
        if prior is None:
            sessions[session.class_id] = session
            continue
        membership_id = prior.membership_id
        if membership_id is None:
            membership_id = session.membership_id
        base = sessions.get(session.class_id, prior)
        sessions[session.class_id] = replace(
            base,
            seat=session.seat,
            membership_id=membership_id,
        )
    return PersonAgenda(person=person, horizon=horizon, sessions=sessions)


def plan_reserve(agenda: PersonAgenda, class_id: ClassId, join_waitlist: bool) -> ReservePlan:
    """Decide from the folded seat only. An open seat is the only post."""
    session = agenda.sessions.get(class_id)
    if session is None:
        return GiveUp(Unavailable("not_found", None))
    seat = session.seat
    if isinstance(seat, ReservedSeat):
        return AlreadyHeld(Reserved(session))
    if isinstance(seat, WaitlistedSeat):
        return AlreadyHeld(Waitlisted(session))
    if isinstance(seat, (ReservedUncancellable, WaitlistedUncancellable)):
        return AlreadyHeld(HeldUncancellable(session))
    if session.membership_id is None:
        return GiveUp(Unavailable("not_found", session))
    return PostReserve(session.membership_id, join_waitlist)


def plan_cancel(agenda: PersonAgenda, class_id: ClassId) -> CancelPlan:
    session = agenda.sessions.get(class_id)
    if session is None or isinstance(session.seat, OpenSeat):
        return AlreadyClear()
    seat = session.seat
    if isinstance(seat, ReservedSeat):
        return DeleteAttendance(seat.attendance_id)
    if isinstance(seat, WaitlistedSeat):
        return DeleteWaitlist(seat.waitlist_id)
    return CannotCancel()


def outcome_after_attempt(
    session: ClassSession | None,
    failure: AttemptFailure | None,
) -> ReserveOutcome:
    """The folded seat wins over the attempt status."""
    if session is not None:
        seat = session.seat
        if isinstance(seat, ReservedSeat):
            return Reserved(session)
        if isinstance(seat, WaitlistedSeat):
            return Waitlisted(session)
        if isinstance(seat, (ReservedUncancellable, WaitlistedUncancellable)):
            return HeldUncancellable(session)
    reason = failure if failure is not None else "not_found"
    if session is not None and isinstance(session.seat, OpenSeat):
        return Unavailable(reason, session)
    return Unavailable(reason, None)


def class_payload(session: ClassSession) -> PublicClass:
    """JSON for the sensor and for list_classes. Membership and attendance stay off it."""
    seat = session.seat
    return PublicClass(
        class_id=session.class_id,
        name=session.name,
        start=session.window.start.isoformat(),
        end=session.window.end.isoformat(),
        location=session.location,
        instructor=session.instructor,
        seat=_seat_word(seat),
        reservable=seat.reservable if isinstance(seat, OpenSeat) else False,
    )


def outcome_payload(outcome: ReserveOutcome) -> OutcomePayload:
    if isinstance(outcome, Reserved):
        result: OutcomeWord = "reserved"
        session: ClassSession | None = outcome.session
    elif isinstance(outcome, Waitlisted):
        result = "waitlisted"
        session = outcome.session
    elif isinstance(outcome, HeldUncancellable):
        result = "held"
        session = outcome.session
    else:
        result = outcome.reason
        session = outcome.session
    if session is None:
        return OutcomePayload(
            result=result,
            class_id=None,
            name=None,
            start=None,
            end=None,
            location=None,
            instructor=None,
            seat=None,
            reservable=None,
        )
    fields = class_payload(session)
    return OutcomePayload(result=result, **fields)


def entity_uid(kind: EntityKind, partition_id: PartitionId, person_id: PersonId) -> str:
    return f"{kind}:{partition_id}:{person_id}"


def parse_entity_uid(uid: str) -> tuple[EntityKind, PartitionId, PersonId]:
    parts = uid.split(":")
    if len(parts) != 3:
        raise ValueError("entity uid")
    kind, partition, person = parts
    if kind not in ("calendar", "upcoming") or partition == "" or person == "":
        raise ValueError("entity uid")
    parsed: EntityKind = "calendar" if kind == "calendar" else "upcoming"
    return parsed, PartitionId(partition), PersonId(person)


def require_class_id(raw: str) -> ClassId:
    if raw.strip() == "":
        raise ValueError("class id")
    return ClassId(raw.strip())


def enabled_people(
    family: tuple[Person, ...], enabled: frozenset[PersonId]
) -> tuple[Person, ...]:
    return tuple(person for person in family if person.person_id in enabled)


def horizon_for(now: datetime, timezone: ZoneInfo) -> TimeWindow:
    local = now.astimezone(timezone)
    start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return TimeWindow(start, local + OPEN_CLASS_HORIZON)


def held_description(session: ClassSession) -> str:
    if isinstance(session.seat, (WaitlistedSeat, WaitlistedUncancellable)):
        return f"Waitlisted. {session.name}"
    if isinstance(session.seat, ReservedUncancellable):
        return f"Reserved. {session.name}"
    return session.name


def held_event(
    partition_id: PartitionId, person_id: PersonId, session: ClassSession
) -> HeldEvent:
    return HeldEvent(
        uid=f"{entity_uid('calendar', partition_id, person_id)}:{session.class_id}",
        summary=session.name,
        start=session.window.start,
        end=session.window.end,
        location=session.location,
        description=held_description(session),
    )


def open_class_view(
    agenda: PersonAgenda, now: datetime, cap: int = ATTRIBUTE_CAP
) -> OpenClassView:
    rows = agenda.open_in_horizon(now)
    state = rows[0].name if rows else "none"
    return OpenClassView(
        state=state,
        count=len(rows),
        classes=tuple(class_payload(session) for session in rows[:cap]),
    )


def _is_hold(seat: Seat) -> bool:
    return not isinstance(seat, OpenSeat)


def _by_start(session: ClassSession) -> tuple[datetime, str]:
    return (session.window.start, session.class_id)


def _seat_word(seat: Seat) -> SeatWord:
    if isinstance(seat, OpenSeat):
        return "open"
    if isinstance(seat, ReservedSeat):
        return "reserved"
    if isinstance(seat, WaitlistedSeat):
        return "waitlisted"
    if isinstance(seat, ReservedUncancellable):
        return "reserved_uncancellable"
    return "waitlisted_uncancellable"
