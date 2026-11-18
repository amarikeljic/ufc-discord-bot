"""Discord scheduled events: when a card's event opens and closes.

It opens with the first fight -- the early prelims, the prelims where there are
none, the main card where there is nothing else -- and ends where it always
did, unless that is before the main card is likely to be over. Checked in
America/Chicago on both sides of a daylight-saving change, and against an event
already on the calendar, which has to move too.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import discord
from conftest import bout, card, fighter

from ufcbot.features import sync as sync_module
from ufcbot.features.sync import MAIN_CARD_RUNS, EventSyncer, scheduled_times
from ufcbot.models import Event
from ufcbot.records import GuildSettings
from ufcbot.sources.espn import segment_starts

CENTRAL = ZoneInfo("America/Chicago")
DEFAULT = GuildSettings(guild_id=1)  # four hours, counted from the main card


def a_card(*, early: datetime | None = None, prelims: datetime | None = None, main: datetime | None = None) -> Event:
    """A card with fights on whichever parts are given, dated as ESPN dates them."""
    parts = [(2, early, "Early Prelims"), (1, prelims, "Prelims"), (0, main, "Main Card")]
    bouts = []
    for rank, when, name in parts:
        if when is not None:
            fight = bout(f"B{rank}", fighter(f"{rank}1", f"Al {rank}"), fighter(f"{rank}2", f"Bo {rank}"))
            fight.segment, fight.start = name, when
            bouts.append(fight)
    first = min(when for _rank, when, _name in parts if when is not None)
    event = card(*bouts, start=first, event_id="600061400")
    event.segment_starts = {rank: when for rank, when, _name in parts if when is not None}
    event.main_card_start = main
    return event


def central(when: datetime) -> str:
    return when.astimezone(CENTRAL).strftime("%a %b %d %I:%M %p %Z")


def test_a_card_with_early_prelims_opens_with_them():
    event = a_card(
        early=datetime(2026, 11, 14, 22, tzinfo=UTC),
        prelims=datetime(2026, 11, 15, 0, tzinfo=UTC),
        main=datetime(2026, 11, 15, 2, tzinfo=UTC),
    )
    start, end = scheduled_times(event, DEFAULT)

    assert central(start) == "Sat Nov 14 04:00 PM CST"
    assert end == datetime(2026, 11, 15, 2, tzinfo=UTC) + timedelta(minutes=240), "the end it always had"


def test_a_card_with_only_prelims_opens_with_them():
    event = a_card(prelims=datetime(2026, 11, 21, 15, tzinfo=UTC), main=datetime(2026, 11, 21, 18, tzinfo=UTC))
    start, _end = scheduled_times(event, DEFAULT)

    assert central(start) == "Sat Nov 21 09:00 AM CST"


def test_a_card_with_only_a_main_card_opens_with_it():
    event = a_card(main=datetime(2026, 12, 13, 2, tzinfo=UTC))
    start, end = scheduled_times(event, DEFAULT)

    assert start == datetime(2026, 12, 13, 2, tzinfo=UTC)
    assert end - start == timedelta(minutes=240)


def test_the_first_fight_not_the_cards_date_decides():
    """UFC 335 is dated for early prelims nobody is booked on yet. Its event
    opens with the prelims until there are early prelims to open with."""
    event = a_card(prelims=datetime(2026, 12, 13, 0, tzinfo=UTC), main=datetime(2026, 12, 13, 2, tzinfo=UTC))
    event.start = datetime(2026, 12, 12, 22, tzinfo=UTC)  # ESPN's date for the card

    assert scheduled_times(event, DEFAULT)[0] == datetime(2026, 12, 13, 0, tzinfo=UTC)


def test_central_time_is_right_either_side_of_the_clocks_going_back():
    """The clocks go back at 2 a.m. on November 1, 2026. A card at five Central
    on each Saturday either side is 22:00 UTC before the change and 23:00 after,
    and comes out at five both times -- as does a card that ends past the change."""
    before = a_card(early=datetime(2026, 10, 31, 22, tzinfo=UTC), main=datetime(2026, 11, 1, 2, tzinfo=UTC))
    after = a_card(early=datetime(2026, 11, 7, 23, tzinfo=UTC), main=datetime(2026, 11, 8, 3, tzinfo=UTC))

    start, end = scheduled_times(before, DEFAULT)
    assert central(start) == "Sat Oct 31 05:00 PM CDT"
    assert central(end) == "Sun Nov 01 01:00 AM CDT", "four hours of main card, still on summer time"
    late = GuildSettings(guild_id=1, duration_minutes=360)
    assert central(scheduled_times(before, late)[1]) == "Sun Nov 01 02:00 AM CST", "and past the change"
    assert central(scheduled_times(after, DEFAULT)[0]) == "Sat Nov 07 05:00 PM CST"


def test_an_end_before_the_main_card_is_likely_over_is_pushed_back():
    """Counting a short duration from the prelims would close the event with
    the main card barely under way."""
    event = a_card(prelims=datetime(2026, 11, 21, 15, tzinfo=UTC), main=datetime(2026, 11, 21, 18, tzinfo=UTC))
    short = GuildSettings(guild_id=1, duration_minutes=120, start_anchor="prelims")

    assert scheduled_times(event, short)[1] == datetime(2026, 11, 21, 18, tzinfo=UTC) + MAIN_CARD_RUNS


def test_the_parts_of_the_card_are_read_off_its_fights():
    """From ESPN's competitions, as the event document carries them."""
    competitions = [
        {"date": "2026-11-15T02:00Z", "cardSegment": {"description": "Main Card"}},
        {"date": "2026-11-15T00:00Z", "cardSegment": {"description": "Prelims"}},
        {"date": "2026-11-14T22:00Z", "cardSegment": {"description": "Early Prelims"}},
        {"date": "2026-11-14T22:00Z", "cardSegment": {"description": "Early Prelims"}},
        {"date": "2026-11-14T20:00Z", "cardSegment": {}},
    ]
    assert segment_starts(competitions) == {
        0: datetime(2026, 11, 15, 2, tzinfo=UTC),
        1: datetime(2026, 11, 15, 0, tzinfo=UTC),
        2: datetime(2026, 11, 14, 22, tzinfo=UTC),
    }


# An event already on the calendar moves too.


class ScheduledEvent:
    def __init__(self, event_id: int, start: datetime) -> None:
        self.id, self.start_time = event_id, start
        self.status = discord.EventStatus.scheduled
        self.edits: list[dict] = []

    async def edit(self, **kwargs) -> None:
        self.edits.append(kwargs)
        self.start_time = kwargs["start_time"]


class Calendar:
    """A guild's scheduled events."""

    def __init__(self, *events: ScheduledEvent) -> None:
        self.id = 1
        self.events = list(events)
        self.created: list[dict] = []
        self.me = SimpleNamespace(guild_permissions=SimpleNamespace(manage_events=True))

    async def fetch_scheduled_events(self):
        return list(self.events)

    async def create_scheduled_event(self, **kwargs):
        self.created.append(kwargs)
        made = ScheduledEvent(9000 + len(self.created), kwargs["start_time"])
        self.events.append(made)
        return made


class Cards:
    def __init__(self, *events: Event) -> None:
        self.events = list(events)

    async def upcoming_events(self, **kwargs):
        return list(self.events)

    async def get_event(self, event_id, **kwargs):
        return next((event for event in self.events if event.id == event_id), None)


async def test_an_event_already_on_the_calendar_is_moved_to_the_early_prelims(storage, monkeypatch):
    monkeypatch.setattr(sync_module, "WRITE_DELAY", 0)
    soon = datetime.now(UTC).replace(minute=0, second=0, microsecond=0) + timedelta(days=5)
    event = a_card(early=soon, prelims=soon + timedelta(hours=2), main=soon + timedelta(hours=4))
    # Created by the old rule, at the main card, and linked under its old signature.
    existing = ScheduledEvent(777, soon + timedelta(hours=4))
    await storage.save_link(1, event.id, existing.id, "signature-from-before")
    calendar = Calendar(existing)
    syncer = EventSyncer(Cards(event), posters=None, storage=storage, enable_poster_art=False)

    result = await syncer.sync_guild(calendar, DEFAULT)

    assert result.updated == 1 and calendar.created == [], "moved, not duplicated"
    assert existing.edits[0]["start_time"] == soon
    assert existing.edits[0]["end_time"] == soon + timedelta(hours=4) + timedelta(minutes=240)

    again = await syncer.sync_guild(calendar, DEFAULT)
    assert again.unchanged == 1 and len(existing.edits) == 1, "and left alone once it is right"
