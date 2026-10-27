"""Callers of the schedule model. No Home Assistant."""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from custom_components.zenplanner.models import (
    AlreadyClear,
    AlreadyHeld,
    AttendanceId,
    CannotCancel,
    ClassId,
    ClassSession,
    DeleteAttendance,
    DeleteWaitlist,
    FamilyId,
    GiveUp,
    HeldUncancellable,
    MembershipId,
    OpenSeat,
    PartitionId,
    Person,
    PersonId,
    PostReserve,
    Reserved,
    ReservedSeat,
    ReservedUncancellable,
    TimeWindow,
    Unavailable,
    WaitlistId,
    Waitlisted,
    WaitlistedSeat,
    WaitlistedUncancellable,
    class_payload,
    enabled_people,
    entity_uid,
    fold_agenda,
    held_description,
    held_event,
    horizon_for,
    merge_family,
    open_class_view,
    outcome_after_attempt,
    outcome_payload,
    parse_entity_uid,
    plan_cancel,
    plan_reserve,
    require_class_id,
)

ZONE = ZoneInfo("America/Denver")
HORIZON = TimeWindow(
    datetime(2026, 10, 26, 0, tzinfo=ZONE),
    datetime(2026, 11, 9, 15, 30, tzinfo=ZONE),
)
NOW = datetime(2026, 10, 26, 15, 30, tzinfo=ZONE)
ADA = Person(PersonId("ada"), FamilyId("fam"), "Ada", "Lovelace")
PUBLIC_KEYS = {
    "class_id",
    "name",
    "start",
    "end",
    "location",
    "instructor",
    "seat",
    "reservable",
}


def at(day: int, hour: int, month: int = 10) -> datetime:
    return datetime(2026, month, day, hour, tzinfo=ZONE)


def session(
    class_id: str,
    seat: OpenSeat | ReservedSeat | WaitlistedSeat | ReservedUncancellable | WaitlistedUncancellable,
    start: datetime,
    *,
    end: datetime | None = None,
    membership_id: str | None = "membership-secret",
    name: str = "Noon WOD",
) -> ClassSession:
    return ClassSession(
        class_id=ClassId(class_id),
        name=name,
        window=TimeWindow(start, end or start + timedelta(hours=1)),
        location="South",
        instructor="Riley",
        membership_id=None if membership_id is None else MembershipId(membership_id),
        seat=seat,
    )


def listed(*rows: ClassSession):
    return fold_agenda(ADA, (), rows, HORIZON)


def test_fold_reservation_seat_wins_and_membership_prefers_upcoming() -> None:
    upcoming = session("c1", OpenSeat(True), at(27, 12), membership_id="from-upcoming")
    reserved = session(
        "c1",
        ReservedSeat(AttendanceId("att-1")),
        at(27, 12),
        membership_id="from-reservation",
    )
    stored = fold_agenda(ADA, (reserved,), (upcoming,), HORIZON).sessions[ClassId("c1")]
    assert stored.seat == ReservedSeat(AttendanceId("att-1"))
    assert stored.membership_id == MembershipId("from-upcoming")


def test_fold_uses_reservation_membership_when_upcoming_has_none() -> None:
    upcoming = session("c1", OpenSeat(True), at(27, 12), membership_id=None)
    reserved = session(
        "c1",
        ReservedSeat(AttendanceId("att-1")),
        at(27, 12),
        membership_id="from-reservation",
    )
    stored = fold_agenda(ADA, (reserved,), (upcoming,), HORIZON).sessions[ClassId("c1")]
    assert stored.membership_id == MembershipId("from-reservation")


def test_fold_keeps_reservation_outside_horizon_and_drops_open_class() -> None:
    outside = at(1, 9, month=12)
    reserved = session("held", ReservedSeat(AttendanceId("att-out")), outside)
    upcoming = session("open", OpenSeat(True), outside)
    agenda = fold_agenda(ADA, (reserved,), (upcoming,), HORIZON)
    assert ClassId("held") in agenda.sessions
    assert ClassId("open") not in agenda.sessions


def test_fold_rebuilds_from_the_lists_it_is_given() -> None:
    first = fold_agenda(
        ADA,
        (session("old", ReservedSeat(AttendanceId("att")), at(27, 12)),),
        (),
        HORIZON,
    )
    second = listed(session("new", OpenSeat(True), at(27, 13)))
    assert ClassId("old") in first.sessions
    assert set(second.sessions) == {ClassId("new")}


def test_second_reserve_of_a_held_seat_does_not_ask_for_a_post() -> None:
    agenda = fold_agenda(
        ADA,
        (
            session("reserved", ReservedSeat(AttendanceId("att-1")), at(27, 12)),
            session("wait", WaitlistedSeat(WaitlistId("wl-1")), at(27, 13)),
            session("stuck", ReservedUncancellable(), at(27, 14)),
            session("line", WaitlistedUncancellable(), at(27, 15)),
        ),
        (),
        HORIZON,
    )
    reserved_plan = plan_reserve(agenda, ClassId("reserved"), False)
    assert isinstance(reserved_plan, AlreadyHeld)
    assert isinstance(reserved_plan.outcome, Reserved)
    for flag in (False, True):
        wait_plan = plan_reserve(agenda, ClassId("wait"), flag)
        assert isinstance(wait_plan, AlreadyHeld)
        assert isinstance(wait_plan.outcome, Waitlisted)
        stuck_plan = plan_reserve(agenda, ClassId("stuck"), flag)
        assert isinstance(stuck_plan, AlreadyHeld)
        assert isinstance(stuck_plan.outcome, HeldUncancellable)
        line_plan = plan_reserve(agenda, ClassId("line"), flag)
        assert isinstance(line_plan, AlreadyHeld)
        assert isinstance(line_plan.outcome, HeldUncancellable)


def test_open_seat_is_the_only_post_and_ignores_reservable() -> None:
    agenda = listed(session("full", OpenSeat(False), at(27, 12), membership_id="mem-1"))
    plan = plan_reserve(agenda, ClassId("full"), True)
    assert isinstance(plan, PostReserve)
    assert plan.membership_id == MembershipId("mem-1")
    assert plan.join_waitlist_if_full is True
    quiet = plan_reserve(agenda, ClassId("full"), False)
    assert isinstance(quiet, PostReserve)
    assert quiet.join_waitlist_if_full is False


def test_open_seat_without_membership_and_missing_class_give_up() -> None:
    bare = session("bare", OpenSeat(True), at(27, 12), membership_id=None)
    agenda = listed(bare)
    missing = plan_reserve(agenda, ClassId("missing"), False)
    assert isinstance(missing, GiveUp)
    assert missing.outcome == Unavailable("not_found", None)
    bare_plan = plan_reserve(agenda, ClassId("bare"), True)
    assert isinstance(bare_plan, GiveUp)
    assert bare_plan.outcome.reason == "not_found"
    assert bare_plan.outcome.session == bare


def test_second_cancel_of_open_or_missing_class_is_already_clear() -> None:
    agenda = listed(session("open", OpenSeat(True), at(27, 12)))
    assert isinstance(plan_cancel(agenda, ClassId("open")), AlreadyClear)
    assert isinstance(plan_cancel(agenda, ClassId("missing")), AlreadyClear)


def test_cancel_deletes_the_id_the_seat_carries() -> None:
    agenda = fold_agenda(
        ADA,
        (
            session("c1", ReservedSeat(AttendanceId("att-9")), at(27, 12)),
            session("c2", WaitlistedSeat(WaitlistId("wl-9")), at(27, 13)),
        ),
        (),
        HORIZON,
    )
    reserved_plan = plan_cancel(agenda, ClassId("c1"))
    wait_plan = plan_cancel(agenda, ClassId("c2"))
    assert reserved_plan == DeleteAttendance(AttendanceId("att-9"))
    assert wait_plan == DeleteWaitlist(WaitlistId("wl-9"))


def test_uncancellable_hold_cannot_cancel() -> None:
    agenda = fold_agenda(
        ADA,
        (
            session("c1", ReservedUncancellable(), at(27, 12)),
            session("c2", WaitlistedUncancellable(), at(27, 13)),
        ),
        (),
        HORIZON,
    )
    assert isinstance(plan_cancel(agenda, ClassId("c1")), CannotCancel)
    assert isinstance(plan_cancel(agenda, ClassId("c2")), CannotCancel)


def test_outcome_follows_the_folded_seat() -> None:
    reserved = session("c1", ReservedSeat(AttendanceId("att")), at(27, 12))
    waitlisted = session("c2", WaitlistedSeat(WaitlistId("wl")), at(27, 13))
    stuck = session("c3", ReservedUncancellable(), at(27, 14))
    opened = session("c4", OpenSeat(True), at(27, 15))
    assert isinstance(outcome_after_attempt(reserved, "spots_full"), Reserved)
    assert isinstance(outcome_after_attempt(waitlisted, "not_found"), Waitlisted)
    assert isinstance(outcome_after_attempt(stuck, None), HeldUncancellable)
    assert outcome_after_attempt(opened, "spots_full") == Unavailable("spots_full", opened)
    assert outcome_after_attempt(opened, None) == Unavailable("not_found", opened)
    assert outcome_after_attempt(None, None) == Unavailable("not_found", None)
    assert outcome_after_attempt(None, "waitlist_full") == Unavailable("waitlist_full", None)


def test_public_payloads_omit_membership_attendance_and_waitlist_ids() -> None:
    row = session("c1", WaitlistedSeat(WaitlistId("waitlist-secret")), at(27, 12))
    reserved = session("c2", ReservedSeat(AttendanceId("attendance-secret")), at(27, 13))
    payloads = (
        class_payload(row),
        class_payload(reserved),
        outcome_payload(Reserved(reserved)),
        outcome_payload(Unavailable("not_found", None)),
    )
    for payload in payloads:
        assert "membership-secret" not in payload.values()
        assert "attendance-secret" not in payload.values()
        assert "waitlist-secret" not in payload.values()
    assert set(class_payload(row)) == PUBLIC_KEYS
    assert class_payload(row)["seat"] == "waitlisted"
    assert class_payload(reserved)["seat"] == "reserved"
    stuck = class_payload(session("c3", ReservedUncancellable(), at(27, 14)))
    assert stuck["seat"] == "reserved_uncancellable"
    assert stuck["reservable"] is False
    opened = class_payload(session("c4", OpenSeat(True), at(27, 15), membership_id=None))
    assert opened["seat"] == "open"
    assert opened["reservable"] is True
    hidden = outcome_payload(Unavailable("not_found", None))
    assert hidden["result"] == "not_found"
    assert hidden["class_id"] is None
    held = outcome_payload(
        HeldUncancellable(session("c5", WaitlistedUncancellable(), at(27, 16)))
    )
    assert held["result"] == "held"
    assert held["seat"] == "waitlisted_uncancellable"


def test_horizon_views_separate_holds_from_open_classes() -> None:
    early = session("early", ReservedSeat(AttendanceId("a")), at(27, 8), name="Early")
    later = session("later", OpenSeat(True), at(27, 18), name="Later")
    wait = session("wait", WaitlistedSeat(WaitlistId("w")), at(28, 9), name="Wait")
    stuck = session("stuck", ReservedUncancellable(), at(1, 9, month=12), name="Stuck")
    ended = session("ended", OpenSeat(True), at(26, 8), end=at(26, 9), name="Ended")
    agenda = fold_agenda(ADA, (early, wait, stuck), (early, later, wait, ended), HORIZON)
    assert [item.name for item in agenda.during_horizon(NOW)] == ["Early", "Later", "Wait"]
    assert [item.name for item in agenda.open_in_horizon(NOW)] == ["Later"]
    assert agenda.next_open(NOW).name == "Later"
    assert agenda.next_held(NOW).name == "Early"
    assert [item.name for item in agenda.held_between(at(27, 0), at(28, 0))] == ["Early"]
    outside = agenda.held_between(at(1, 0, month=12), at(2, 0, month=12))
    assert [item.name for item in outside] == ["Stuck"]
    assert agenda.next_held(datetime(2026, 12, 1, 8, tzinfo=ZONE)).name == "Stuck"


def test_same_start_orders_by_class_id() -> None:
    agenda = listed(
        session("b", OpenSeat(True), at(27, 12), name="B"),
        session("a", OpenSeat(True), at(27, 12), name="A"),
    )
    assert [item.class_id for item in agenda.during_horizon(NOW)] == [
        ClassId("a"),
        ClassId("b"),
    ]


def test_open_class_view_caps_the_list_and_counts_every_row() -> None:
    rows = tuple(
        session(
            f"c{index:02d}",
            OpenSeat(True),
            at(27, 12) + timedelta(minutes=index),
            name=f"Class {index}",
            membership_id=None,
        )
        for index in range(12)
    )
    view = open_class_view(listed(*rows), NOW)
    assert view.state == "Class 0"
    assert view.count == 12
    assert len(view.classes) == 10
    assert view.classes[0]["name"] == "Class 0"
    empty = open_class_view(listed(), NOW)
    assert empty.state == "none"
    assert empty.count == 0
    assert empty.classes == ()


def test_held_description_and_event_uid() -> None:
    reserved = session("c1", ReservedSeat(AttendanceId("att")), at(27, 12), name="Noon WOD")
    waitlisted = session("c2", WaitlistedSeat(WaitlistId("wl")), at(27, 13), name="Night")
    reserved_stuck = session("c3", ReservedUncancellable(), at(27, 14), name="Stuck")
    wait_stuck = session("c4", WaitlistedUncancellable(), at(27, 15), name="Line")
    assert held_description(reserved) == "Noon WOD"
    assert held_description(waitlisted).startswith("Waitlisted.")
    assert held_description(wait_stuck).startswith("Waitlisted.")
    assert held_description(reserved_stuck).startswith("Reserved.")
    event = held_event(PartitionId("gym"), PersonId("ada"), reserved)
    assert event.summary == "Noon WOD"
    assert event.description == "Noon WOD"
    assert event.uid == "calendar:gym:ada:c1"
    assert event.location == "South"


def test_horizon_starts_at_local_midnight_and_ends_fourteen_days_after_now() -> None:
    window = horizon_for(NOW, ZONE)
    assert window.start == datetime(2026, 10, 26, 0, tzinfo=ZONE)
    assert window.end == NOW + timedelta(days=14)
    assert window.contains_start(datetime(2026, 10, 26, 6, tzinfo=ZONE))
    assert not window.contains_start(window.end)


def test_merge_family_and_enabled_people_follow_family_order() -> None:
    byron = Person(PersonId("byron"), FamilyId("fam"), "Byron", "")
    duplicate = Person(PersonId("ada"), FamilyId("fam"), "Ada", "Other")
    merged = merge_family(ADA, (byron, duplicate, byron))
    assert [person.person_id for person in merged] == [PersonId("byron"), PersonId("ada")]
    assert merged[1].last_name == "Other"
    holder_first = merge_family(ADA, (byron,))
    assert [person.person_id for person in holder_first] == [PersonId("ada"), PersonId("byron")]
    enabled = enabled_people(
        holder_first, frozenset({PersonId("byron"), PersonId("missing")})
    )
    assert enabled == (byron,)


def test_display_name_and_ids() -> None:
    assert ADA.display_name == "Ada Lovelace"
    assert Person(PersonId("x"), FamilyId("f"), "  ", "").display_name == "Member"
    assert entity_uid("upcoming", PartitionId("gym"), PersonId("ada")) == "upcoming:gym:ada"
    kind, partition, person = parse_entity_uid("calendar:gym:ada")
    assert kind == "calendar"
    assert partition == PartitionId("gym")
    assert person == PersonId("ada")
    with pytest.raises(ValueError):
        parse_entity_uid("calendar:gym")
    with pytest.raises(ValueError):
        parse_entity_uid("other:gym:ada")
    with pytest.raises(ValueError):
        require_class_id("   ")
    assert require_class_id("  c1  ") == ClassId("c1")
