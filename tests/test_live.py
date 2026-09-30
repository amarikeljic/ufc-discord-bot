"""Live coverage: which fights the bot asks ESPN about while a card is on.

Fights happen one at a time, so the question the polling window answers is how
few requests a tick can get away with and still never miss a result.
"""

from __future__ import annotations

from ufcbot.features.live import LiveCoverage
from ufcbot.models import Bout, Fighter


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

