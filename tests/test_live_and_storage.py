"""Live coverage's polling window, and the database's own housekeeping."""

from __future__ import annotations

import sqlite3
import time
from datetime import UTC, datetime, timedelta

from ufcbot.embeds.images import HEADSHOT_TTL, MatchupImages
from ufcbot.features.live import LiveCoverage
from ufcbot.models import Bout, Fighter
from ufcbot.sources.http import SWEEP_INTERVAL, Entry, HttpClient, cache_key
from ufcbot.storage import Storage
from ufcbot.util import format_duration


def pending_bout(index: int, *, completed: bool = False) -> Bout:
    return Bout(
        id=f"B{index}",
        fighters=[Fighter(id=f"{index}a", display_name=f"A{index}"), Fighter(id=f"{index}b", display_name=f"B{index}")],
        competitors=2,
        completed=completed,
    )


def watched(bouts) -> list[str]:
    return [bout.id for bout in LiveCoverage._in_play(bouts)]


def test_before_the_card_only_the_first_few_are_watched():
    """Polling all twelve every fifteen seconds asks ESPN about ten fights that
    have not begun."""
    assert watched([pending_bout(i) for i in range(12)]) == ["B0", "B1", "B2"]


def test_mid_card_the_finished_the_live_and_the_next_are_watched():
    card = [pending_bout(i, completed=True) for i in range(4)] + [pending_bout(i) for i in range(4, 12)]
    assert watched(card) == ["B0", "B1", "B2", "B3", "B4", "B5", "B6"]


def test_a_finished_card_with_nothing_posted_is_watched_in_full():
    """A restart after the card still owes every result."""
    assert len(watched([pending_bout(i, completed=True) for i in range(12)])) == 12


def test_an_empty_card_watches_nothing():
    assert watched([]) == []


# -- housekeeping --------------------------------------------------------------


async def test_old_live_coverage_rows_are_pruned(storage):
    await storage.mark_live_posted("OLD", "result")
    await storage.save_live_snapshot("OLD", 1, {"1": {"kd": 1.0}})

    removed = await storage.prune_live_data(datetime.now(UTC) + timedelta(days=1))

    assert removed == 1
    assert await storage.live_posted("OLD") == set()
    assert await storage.live_snapshot("OLD", 1) is None


async def test_recent_live_coverage_rows_are_kept(storage):
    await storage.mark_live_posted("NEW", "result")
    await storage.prune_live_data(datetime.now(UTC) - timedelta(days=1))
    assert await storage.live_posted("NEW") == {"result"}


async def test_an_older_database_is_upgraded_in_place(tmp_path):
    """Columns, indexes and tables are added to a database that has rows in it."""
    path = tmp_path / "old.sqlite3"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE predictions (
            espn_event_id TEXT NOT NULL, bout_id TEXT NOT NULL, event_name TEXT NOT NULL,
            event_start TEXT NOT NULL, athlete_a TEXT NOT NULL, name_a TEXT NOT NULL,
            athlete_b TEXT NOT NULL, name_b TEXT NOT NULL, prob_a REAL NOT NULL,
            weight_class TEXT, position INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL,
            winner_athlete TEXT, correct INTEGER, graded_at TEXT,
            PRIMARY KEY (espn_event_id, bout_id));
        INSERT INTO predictions VALUES
            ('E','B','UFC 1','2026-01-01T00:00+00:00','1','A','2','B',0.6,'LW',0,
             '2026-01-01T00:00+00:00',NULL,NULL,NULL);
        """
    )
    connection.commit()
    connection.close()

    store = Storage(str(path))
    await store.connect()
    try:
        records = await store.predictions_for_event("E")
        assert len(records) == 1 and records[0].name_a == "A", "the row survived"
        assert records[0].odds_a is None, "and reads through the columns added since"

        async with store.db.execute("SELECT name FROM sqlite_master WHERE type='table'") as cursor:
            tables = {row[0] for row in await cursor.fetchall()}
        assert "card_bouts" in tables

        settings = await store.get_settings(1)
        assert settings.rankings_include_women is True, "the default for a server that never chose"
    finally:
        await store.close()


# -- the response cache ----------------------------------------------------------


def held(client: HttpClient, key: str, *, age: float = 0.0, ttl: int = 86400, size: int = 100) -> None:
    client._cache[key] = Entry(time.monotonic() - age, ttl, {"payload": key}, size)
    client._held += size


def test_expired_responses_are_dropped_on_a_timer_not_only_when_the_cache_fills():
    """A quiet bot never reaches either cap, so nothing would evict what has gone
    stale: hundreds of parsed payloads no caller can be given again."""
    client = HttpClient()
    held(client, "stale", age=3600, ttl=10)
    held(client, "fresh")

    client._last_sweep = time.monotonic() - SWEEP_INTERVAL - 1
    client._store("new", {"a": 1}, ttl=900, size=10)

    assert "stale" not in client._cache, "past its lifetime and unreadable"
    assert set(client._cache) == {"fresh", "new"}


def test_a_sweep_that_has_just_run_is_not_run_again():
    client = HttpClient()
    held(client, "stale", age=3600, ttl=10)

    client._last_sweep = time.monotonic()
    client._store("new", {"a": 1}, ttl=900, size=10)

    assert "stale" in client._cache, "swept at most once every SWEEP_INTERVAL"


def test_a_heavy_cache_is_trimmed_even_when_it_holds_few_things():
    """Entries vary from a couple of kilobytes to well over a hundred, so a cap
    counted in entries alone says almost nothing about what is being held."""
    client = HttpClient(max_bytes=1000)
    for index in range(5):
        held(client, f"old{index}", size=300)

    client._store("new", {"a": 1}, ttl=900, size=300)

    assert client._held <= 1000
    assert "new" in client._cache, "what just arrived is what is wanted"
    assert "old0" not in client._cache, "the least recently read goes first"


def test_the_weight_of_a_replaced_entry_is_not_counted_twice():
    client = HttpClient()
    client._store("same", {"a": 1}, ttl=900, size=500)
    client._store("same", {"a": 2}, ttl=900, size=700)

    assert client._held == 700


def test_espn_link_parameters_that_change_nothing_share_one_entry():
    """ESPN sends lang and region on its $ref links but not on URLs built by
    hand, and the response is identical. Keyed separately, the biggest documents
    of all are held twice."""
    assert cache_key("https://x/events/1?lang=en&region=us") == "https://x/events/1"
    assert cache_key("https://x/events/1") == "https://x/events/1"
    # Anything that does change the response stays in the key.
    assert cache_key("https://x/plays?limit=300&lang=en") == "https://x/plays?limit=300"


# -- how long the bot has been up ------------------------------------------------


def test_an_uptime_reads_in_the_two_units_that_matter():
    assert format_duration(30) == "0m"
    assert format_duration(90 * 60) == "1h 30m"
    assert format_duration(26 * 3600 + 5 * 60) == "1d 2h", "days and hours, not minutes too"


def test_a_response_can_be_read_without_being_kept():
    """A caller that distils a document into something smaller has no use for
    the document. Fighter profiles are three kilobytes each to fill in ten
    fields, and thousands of them held raw is the long tail of the cache."""
    client = HttpClient()
    client._store("kept", {"a": 1}, ttl=900, size=3000)

    assert client._held == 3000
    assert "kept" in client._cache
    # store=False is exercised through get_json, which needs a session; what is
    # pinned here is that _store is the only thing that ever grows the cache.
    assert set(client._cache) == {"kept"}


def test_the_cache_is_held_to_its_weight_not_its_count():
    client = HttpClient(max_bytes=10_000, max_entries=1000)
    for index in range(10):
        held(client, f"doc{index}", size=2_000)

    client._store("new", {"a": 1}, ttl=900, size=2_000)

    assert client._held <= 10_000
    assert len(client._cache) < 11, "well under the entry cap, but over the weight"


# -- headshots ---------------------------------------------------------------------


def images_with(urls_to_bytes: dict[str, bytes], **kwargs) -> MatchupImages:
    """A headshot cache whose downloads are already decided."""
    images = MatchupImages(http=None, **kwargs)
    for url, data in urls_to_bytes.items():
        images._cache[url] = (time.monotonic(), data)
        images._held += len(data)
    return images


def test_headshots_a_card_is_using_are_not_dropped():
    """Every face on a card is read twice, at the walkout and at the result, so
    a cache that evicted between the two would fetch the whole card again."""
    card = {f"f{i}": b"\x89PNG" + bytes(200_000) for i in range(21)}
    images = images_with(card)

    images._expire(time.monotonic())

    assert len(images._cache) == 21, "4.2 MB of a 6 MB cap"


def test_headshots_nobody_has_looked_at_for_hours_are_dropped():
    """A card is four hours and the next one is a fortnight away. Holding the
    faces in between costs megabytes to save nothing."""
    images = images_with({"old": b"\x89PNG" + bytes(200_000)})
    images._cache["old"] = (time.monotonic() - HEADSHOT_TTL - 1, images._cache["old"][1])

    images._expire(time.monotonic())

    assert images._cache == {} and images._held == 0


def test_the_cap_still_bites_when_everything_is_fresh():
    images = images_with({f"f{i}": b"\x89PNG" + bytes(500_000) for i in range(20)})

    images._expire(time.monotonic())

    assert images._held <= 6 * 1024 * 1024
    assert len(images._cache) < 20, "the least recently read went first"
