"""Rankings from the model's own ratings.

The rating is the one the model trains on: every fighter starts level, and a
result moves it by how surprising it was, so beating a contender is worth more
than beating a debutant. That makes it a measure of who has done the most
against the best, which is close to what a ranking is for.

It is not the UFC's ranking and will not agree with it. Nobody votes, a title
counts for nothing by itself, and a fighter who arrives from another promotion
starts level with everyone else however good they already are.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from .career import Ledger

# Divisions in the order they are usually listed, lightest first.
DIVISION_ORDER = (
    "Strawweight",
    "Flyweight",
    "Bantamweight",
    "Featherweight",
    "Lightweight",
    "Welterweight",
    "Middleweight",
    "Light Heavyweight",
    "Heavyweight",
    "Women's Strawweight",
    "Women's Flyweight",
    "Women's Bantamweight",
    "Women's Featherweight",
)

POUND_FOR_POUND = "Pound for pound"


def is_womens(division: str | None) -> bool:
    return bool(division and division.startswith("Women's"))

# A fighter who has not been seen in this long is not ranked: retired, released,
# or fighting somewhere else.
ACTIVE_WITHIN = timedelta(days=730)
# Below this many UFC fights a rating is mostly where it started.
MIN_FIGHTS = 3
# How many to list per division.
DEPTH = 15


@dataclass(slots=True)
class Ranked:
    rank: int
    name: str
    rating: int
    record: str
    division: str | None


def _eligible(ledger: Ledger, on: date) -> bool:
    if ledger.fights < MIN_FIGHTS or ledger.last_fight is None:
        return False
    return on - ledger.last_fight <= ACTIVE_WITHIN


def rank_division(
    ledgers: dict[str, Ledger],
    division: str | None,
    *,
    on: date,
    depth: int = DEPTH,
    include_women: bool = True,
) -> list[Ranked]:
    """The best-rated active fighters, in one division or across all of them.

    ``division`` of None ranks everyone, which is the pound-for-pound list, and
    is where ``include_women`` matters: a server that leaves the women's
    divisions out should not find them on that board either.
    """
    entries = [
        ledger
        for ledger in ledgers.values()
        if _eligible(ledger, on)
        and (division is None or ledger.division == division)
        and (include_women or not is_womens(ledger.division))
    ]
    entries.sort(key=lambda ledger: ledger.elo, reverse=True)
    return [
        Ranked(
            rank=index,
            name=ledger.name,
            rating=round(ledger.elo),
            record=ledger.record,
            division=ledger.division,
        )
        for index, ledger in enumerate(entries[:depth], 1)
    ]


def divisions_with_fighters(
    ledgers: dict[str, Ledger], *, on: date, include_women: bool = True
) -> list[str]:
    """Divisions that have anyone to rank, in the usual order."""
    present = {ledger.division for ledger in ledgers.values() if _eligible(ledger, on)}
    return [
        division
        for division in DIVISION_ORDER
        if division in present and (include_women or not is_womens(division))
    ]
