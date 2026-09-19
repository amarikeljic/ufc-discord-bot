"""Live coverage's polling window, and the database's own housekeeping."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

from ufcbot.features.live import LiveCoverage
from ufcbot.models import Bout, Fighter
from ufcbot.storage import Storage


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
        assert records[0].odds_source is None, "and reads through the new columns"

        async with store.db.execute("SELECT name FROM sqlite_master WHERE type='table'") as cursor:
            tables = {row[0] for row in await cursor.fetchall()}
        assert "card_bouts" in tables

        settings = await store.get_settings(1)
        assert settings.rankings_include_women is True, "the default for a server that never chose"
    finally:
        await store.close()
