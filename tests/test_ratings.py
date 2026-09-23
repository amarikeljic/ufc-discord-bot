"""Spotting how a ratings board moved, and saying why."""

from __future__ import annotations

from datetime import date, timedelta

from ufcbot.embeds import ratings_changes_embed
from ufcbot.features.ratings import DOWN, ENTERED, INACTIVE, LEFT, PUSHED, UP, diff
from ufcbot.records import RankedState
from ufcbot.stats.career import Ledger

TODAY = date(2026, 9, 18)
RECENT = TODAY - timedelta(days=30)
OLD = TODAY - timedelta(days=1000)


def ledger(name: str, elo: float, *, last: date | None = RECENT, result: str = "win") -> Ledger:
    entry = Ledger(name=name)
    entry.elo = elo
    entry.fights = 8
    entry.division = "Lightweight"
    entry.last_fight = last
    entry.last_result = result
    return entry


def ranked(key: str, rank: int, rating: int, last: date | None = RECENT):
    """A board row in the shape the diff compares against."""
    return RankedState(key, rank, rating, last)


class Row:
    """What rank_division hands back, reduced to what the diff reads."""

    def __init__(self, key, rank, rating, last=RECENT, result="win", name=None):
        self.key, self.rank, self.rating = key, rank, rating
        self.last_fight, self.last_result, self.name = last, result, name or key.title()


def test_a_board_that_has_not_moved_reports_nothing():
    previous = {"a": ranked("a", 1, 1200), "b": ranked("b", 2, 1150)}
    current = [Row("a", 1, 1200), Row("b", 2, 1150)]
    assert diff(previous, current, {}, on=TODAY) == []


def test_climbing_after_a_win_says_so():
    previous = {"a": ranked("a", 1, 1200), "b": ranked("b", 2, 1150, last=OLD)}
    current = [Row("b", 1, 1260, last=RECENT, result="win"), Row("a", 2, 1200)]

    changes = {c.name: c for c in diff(previous, current, {}, on=TODAY)}

    assert changes["B"].kind == UP and (changes["B"].was, changes["B"].now) == (2, 1)
    assert changes["B"].reason == "a win"
    # The fighter they passed did not fight; it was not their doing.
    assert changes["A"].kind == DOWN and changes["A"].reason == PUSHED


def test_sliding_after_a_loss_says_so():
    previous = {"a": ranked("a", 1, 1200, last=OLD)}
    current = [Row("a", 1, 1100, last=RECENT, result="loss")]
    # Same rank, so nothing is announced; the reason only matters when they move.
    assert diff(previous, current, {}, on=TODAY) == []

    current = [Row("b", 1, 1300, last=OLD, result="win"), Row("a", 2, 1100, last=RECENT, result="loss")]
    changes = {c.name: c for c in diff(previous, current, {}, on=TODAY)}
    assert changes["A"].kind == DOWN and changes["A"].reason == "a loss"


def test_a_new_entrant_is_reported_with_how_they_got_there():
    previous = {"a": ranked("a", 1, 1200)}
    current = [Row("a", 1, 1200), Row("newcomer", 2, 1150, last=RECENT, result="win")]

    change = next(c for c in diff(previous, current, {}, on=TODAY) if c.kind == ENTERED)

    assert change.name == "Newcomer" and change.now == 2 and change.reason == "a win"


def test_dropping_off_through_inactivity_says_so():
    previous = {"a": ranked("a", 1, 1200), "gone": ranked("gone", 2, 1150, last=OLD)}
    current = [Row("a", 1, 1200)]
    ledgers = {"gone": ledger("Gone Fighter", 1150, last=OLD)}

    change = next(c for c in diff(previous, current, ledgers, on=TODAY) if c.kind == LEFT)

    assert change.name == "Gone Fighter" and change.was == 2
    assert change.reason == INACTIVE


def test_being_pushed_off_the_bottom_is_not_called_inactivity():
    previous = {"a": ranked("a", 1, 1200), "pushed": ranked("pushed", 2, 1150)}
    current = [Row("a", 1, 1200)]
    ledgers = {"pushed": ledger("Still Active", 1150, last=RECENT)}

    change = next(c for c in diff(previous, current, ledgers, on=TODAY) if c.kind == LEFT)

    assert change.reason == PUSHED


def test_the_embed_reads_as_what_happened():
    previous = {"a": ranked("a", 3, 1200, last=OLD), "gone": ranked("gone", 5, 1100, last=OLD)}
    current = [Row("a", 1, 1290, last=RECENT, result="win", name="Islam Makhachev")]
    ledgers = {"gone": ledger("Old Timer", 1100, last=OLD)}

    value = ratings_changes_embed(
        "Lightweight", diff(previous, current, ledgers, on=TODAY)
    ).fields[0].value.replace("\xa0", " ")

    assert "Islam Makhachev" in value and "3 → 1" in value and "after a win" in value
    assert "Old Timer" in value and "out, was **5**" in value
