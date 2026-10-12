"""The career crawl, whose job is to have asked about a fighter before his debut.

The rest of the crawl is built out of fights that already happened, which is
the one source that cannot know about a man who has not fought in the UFC yet.
"""

from __future__ import annotations

import asyncio

import pytest

from ufcbot.models import Bout, Event, Fighter


def fighter(name: str, espn_id: str) -> Fighter:
    return Fighter(id=espn_id, display_name=name)


def event(*fighters: Fighter, event_id: str = "E1") -> Event:
    from datetime import UTC, datetime

    return Event(
        id=event_id,
        name="UFC 400",
        start=datetime(2030, 1, 1, tzinfo=UTC),
        bouts=[Bout(id="B1", fighters=list(fighters), competitors=len(fighters))],
    )


class Schedule:
    """Stands in for ESPN: a list of cards, and a count of what was asked for."""

    def __init__(self, *events: Event):
        self.events = list(events)
        self.searched: list[str] = []
        self.histories: list[str] = []

    async def upcoming_events(self, *, days, limit):
        # Summaries carry no bouts in the real thing either.
        return self.events

    async def get_event(self, event_id, **kwargs):
        for got in self.events:
            if got.id == event_id:
                return got
        return None

    async def search_fighters(self, name, *, limit=5):
        self.searched.append(name)
        return []

    async def fighter_history(self, espn_id):
        self.histories.append(espn_id)
        return []


def test_everyone_booked_is_asked_about_whether_they_have_fought_here_or_not():
    """A fighter signed from outside is missing from a crawl built on past
    fights until their debut is over, which is when a rating for them stops
    being any use."""
    from careers import _announced

    data = Schedule(event(fighter("Fresh Signing", "123"), fighter("Arnold Allen", "456")))

    booked = asyncio.run(_announced(data))

    assert booked == {
        "fresh signing": ("Fresh Signing", "123"),
        "arnold allen": ("Arnold Allen", "456"),
    }


def test_an_unfilled_corner_is_not_asked_about():
    """Fetching one is how a card's placeholder becomes a rated fighter."""
    from careers import _announced

    data = Schedule(event(fighter("Real Person", "1"), fighter("Opponent TBA", "2")))

    booked = asyncio.run(_announced(data))

    assert set(booked) == {"real person"}


def test_a_fighter_off_a_card_is_taken_by_id_rather_than_searched_for():
    """The search is where the name matching fails, and a fighter nobody has
    published before is exactly the kind of name it fails on. The card has
    already said who he is."""
    from careers import _one

    data = Schedule()
    sem = asyncio.Semaphore(1)

    key, got = asyncio.run(_one(data, "Fresh Signing", "789", sem))

    assert key == "fresh signing"
    assert got is not None and got["espn_id"] == "789"
    assert data.searched == [], "the id made a search unnecessary"
    assert data.histories == ["789"]


def test_a_fighter_with_no_id_is_still_searched_for():
    """Most of the crawl is names out of the UFC results, with no id attached."""
    from careers import _one

    data = Schedule()

    key, got = asyncio.run(_one(data, "Nobody At All", None, asyncio.Semaphore(1)))

    assert key == "nobody at all"
    assert got is None, "the search found nothing"
    assert data.searched == ["Nobody At All"]
    assert data.histories == [], "nothing to ask a history for"


@pytest.mark.parametrize("name", ["TBA", "TBD", "To Be Announced"])
def test_the_placeholders_the_cards_actually_use(name):
    from ufcbot.stats.graph import is_placeholder
    from ufcbot.util import normalise

    assert is_placeholder(normalise(name))
