"""Shared fixtures and builders.

Nothing here touches the network, Discord, or a trained model: the tests run
against a real SQLite database in a temporary directory and fight cards built by
hand, so they are fast and say the same thing every time.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from ufcbot.models import Bout, Event, Fighter
from ufcbot.records import PickemRecord
from ufcbot.stats.prediction import Prediction
from ufcbot.storage import Storage


@pytest.fixture
async def storage(tmp_path):
    """A connected, empty database that is thrown away after each test."""
    store = Storage(str(tmp_path / "test.sqlite3"))
    await store.connect()
    try:
        yield store
    finally:
        await store.close()


@pytest.fixture
def soon() -> datetime:
    """A card far enough ahead that picks are open."""
    return datetime.now(UTC) + timedelta(days=10)


def fighter(fighter_id: str, name: str) -> Fighter:
    return Fighter(id=fighter_id, display_name=name, record="20-4-0")


def bout(bout_id: str, a: Fighter, b: Fighter, *, match: int = 1, weight: str = "Lightweight") -> Bout:
    """A fight with both corners named, as a card that read cleanly would give."""
    return Bout(
        id=bout_id,
        fighters=[a, b],
        competitors=2,
        match_number=match,
        segment="Main Card",
        rounds=3,
        weight_class=weight,
    )


def unnamed_bout(bout_id: str) -> Bout:
    """A fight the card lists but whose fighters did not load."""
    return Bout(id=bout_id, fighters=[], competitors=2, segment="Main Card")


def card(*bouts: Bout, start: datetime, partial: bool = False, event_id: str = "EV1") -> Event:
    return Event(id=event_id, name="UFC 333", start=start, bouts=list(bouts), partial=partial)


def prediction(prob_a: float = 0.62) -> Prediction:
    pick = Prediction(fighter_a="A", fighter_b="B", prob_a=prob_a)
    pick.methods_a = {"ko": prob_a * 0.3, "dec_u": prob_a * 0.7}
    pick.methods_b = {"ko": (1 - prob_a) * 0.4, "dec_u": (1 - prob_a) * 0.6}
    return pick


def pickem_record(
    *,
    user_id: int,
    bout_id: str,
    athlete_id: str,
    opponent_id: str,
    locks_at: datetime,
    event_id: str = "EV1",
    odds: int = -225,
    guild_id: int = 1,
) -> PickemRecord:
    return PickemRecord(
        guild_id=guild_id,
        user_id=user_id,
        espn_event_id=event_id,
        bout_id=bout_id,
        event_name="UFC 333",
        event_start=locks_at,
        athlete_id=athlete_id,
        athlete_name=f"Fighter {athlete_id}",
        opponent_id=opponent_id,
        opponent_name=f"Fighter {opponent_id}",
        odds=odds,
        points_if_right=44,
        locks_at=locks_at,
        picked_at=datetime.now(UTC),
    )
