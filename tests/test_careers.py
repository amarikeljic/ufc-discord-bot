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


# -- the nightly top-up -------------------------------------------------------


def cached_file(tmp_path, data: dict):
    import json

    (tmp_path / "espn_careers.json").write_text(json.dumps(data), encoding="utf-8")
    return tmp_path


def read_cache(tmp_path) -> dict:
    import json

    return json.loads((tmp_path / "espn_careers.json").read_text(encoding="utf-8"))


def test_the_nightly_pass_fetches_a_booked_fighter_nobody_has_cached(tmp_path):
    """The one part of the crawl that cannot wait for somebody to run it: a
    fighter signed from outside the UFC is missing until his debut is over."""
    from careers import top_up_booked

    cached_file(tmp_path, {"arnold allen": {"espn_id": "456", "fights": []}})
    data = Schedule(event(fighter("Fresh Signing", "123"), fighter("Arnold Allen", "456")))

    added = asyncio.run(top_up_booked(data, tmp_path))

    assert added == 1
    assert data.histories == ["123"], "only the one nobody had"
    assert "fresh signing" in read_cache(tmp_path)
    assert read_cache(tmp_path)["arnold allen"]["espn_id"] == "456", "the cache is kept"


def test_a_warm_cache_asks_for_nothing(tmp_path):
    """Most nights. It must not rewrite the file either, because the file is
    part of what the build is fingerprinted on and a rewrite would rebuild."""
    from careers import top_up_booked

    cached_file(tmp_path, {"arnold allen": {"espn_id": "456", "fights": []}})
    before = (tmp_path / "espn_careers.json").read_bytes()
    data = Schedule(event(fighter("Arnold Allen", "456")))

    assert asyncio.run(top_up_booked(data, tmp_path)) == 0
    assert data.histories == []
    assert (tmp_path / "espn_careers.json").read_bytes() == before, "untouched"


def test_the_nightly_pass_will_not_turn_into_the_whole_crawl(tmp_path):
    """Bounded, so an empty cache or a bad schedule read cannot make the
    housekeeping pass fetch thousands of careers."""
    from careers import top_up_booked

    cached_file(tmp_path, {})
    many = [fighter(f"Someone {i}", str(i)) for i in range(30)]
    data = Schedule(event(*many))

    asyncio.run(top_up_booked(data, tmp_path, at_most=5))

    assert len(data.histories) == 5


def test_an_unreadable_cache_is_left_alone(tmp_path):
    """Rather than overwritten with whatever tonight happens to find."""
    from careers import top_up_booked

    (tmp_path / "espn_careers.json").write_text("{not json", encoding="utf-8")
    data = Schedule(event(fighter("Fresh Signing", "123")))

    assert asyncio.run(top_up_booked(data, tmp_path)) == 0
    assert (tmp_path / "espn_careers.json").read_text(encoding="utf-8") == "{not json"


# -- why one pass is not the crawl -------------------------------------------


def a_history(**fighters):
    """Stands in for build_history: UFC fighters and when they last fought."""
    from types import SimpleNamespace

    return SimpleNamespace(
        ledgers={
            key: SimpleNamespace(name=name, last_fight=None)
            for key, name in fighters.items()
        }
    )


def test_a_regional_opponent_is_invisible_until_his_rival_is_cached():
    """The opponents are read out of the careers already cached, so on an empty
    cache there are none to read. That is why the crawl goes round more than
    once: the first pass fetches the UFC and the booked cards, and only then can
    it see who those fighters came up through. Run once and stopped, it leaves a
    UFC-only rating that mostly repeats the ledger's own."""
    from careers import OPPONENT_SEEN, _wanted

    history = a_history(ann="Ann")

    cold = _wanted({}, history, refresh=False)

    assert cold == ["Ann"], "nobody to ask about but the UFC fighter"

    # Now Ann's career is in, and she came up fighting the same man repeatedly.
    cached = {
        "ann": {"fights": [
            {"on": f"20{i:02d}-01-01", "result": "W", "opponent": "Regional Guy"}
            for i in range(OPPONENT_SEEN)
        ]},
    }

    warm = _wanted(cached, history, refresh=False)

    assert "Regional Guy" in warm, "visible only on the pass after Ann was fetched"


def test_an_opponent_seen_once_is_left_in_the_long_tail():
    """Below the threshold it is 14,000 fighters seen once each."""
    from careers import _wanted

    history = a_history(ann="Ann")
    cached = {"ann": {"fights": [
        {"on": "2020-01-01", "result": "W", "opponent": "Passing Stranger"},
    ]}}

    assert "Passing Stranger" not in _wanted(cached, history, refresh=False)
