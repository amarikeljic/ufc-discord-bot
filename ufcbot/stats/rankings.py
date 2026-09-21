"""Rankings from the model's own ratings.

The rating is the one the model trains on: every fighter starts level, and a
result moves it by how surprising it was, so beating a contender is worth more
than beating a debutant. That makes it a measure of who has done the most
against the best, which is close to what a ranking is for.

It is not the UFC's ranking and will not agree with it. Nobody votes, a title
counts for nothing by itself, and a fighter who arrives from another promotion
starts level with everyone else however good they already are.

Two things stop a raw rating from reading as a ranking, and both are handled
here rather than in the rating itself:

* A rating is what a fighter has earned, and a fighter who is not fighting is
  not earning. Left alone, someone who retires keeps the number they walked
  away with and outranks everyone still competing for it. So a rating fades
  once a fighter has been out longer than any ordinary gap between bouts.
* Ratings a few points apart are a tie, not an order. Fighters that close
  share a rank instead of being sorted into a precision the number does not
  have.

Both are presentation. ``Ledger.elo`` is left exactly as the fights left it, so
what the model trains and predicts on does not change.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import date, timedelta

from .career import ELO_START, Ledger

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

# A year covers any ordinary gap between fights, injuries included, so nothing
# happens inside it. Past that, what a fighter holds over the starting rating
# halves for every further year out.
DECAY_GRACE = timedelta(days=365)
DECAY_HALF_LIFE = timedelta(days=365)

# A fighter who has not been seen in this long is not ranked: retired, released,
# or fighting somewhere else. Eighteen months is long enough that a comeback
# from injury is still on the board and short enough that a retirement is not.
ACTIVE_WITHIN = timedelta(days=548)

# Ratings this close are a tie, not an order: one result moves a rating by up to
# ELO_K points, so a handful between two fighters is inside the noise of a
# single fight. Across the divisional boards the median gap between neighbours
# is under four points, which is the whole reason this exists.
TIE_GAP = 5

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
    key: str = ""
    """The dataset key, which is what a board is remembered by between passes."""
    last_fight: date | None = None
    last_result: str | None = None
    tied: bool = False
    """Whether this rank is shared with another fighter, rather than held alone."""


def rating_on(ledger: Ledger, on: date) -> int:
    """What the rating is worth today, faded for however long the fighter has been out.

    Only the margin over the starting rating fades, so a fighter who never got
    above it has nothing to lose, and nobody is ever dragged below where they
    began by sitting still.
    """
    if ledger.last_fight is None:
        return round(ledger.elo)
    idle = (on - ledger.last_fight) - DECAY_GRACE
    if idle <= timedelta(0):
        return round(ledger.elo)
    kept = 0.5 ** (idle / DECAY_HALF_LIFE)
    return round(ELO_START + (ledger.elo - ELO_START) * kept)


def is_fading(ledger: Ledger, on: date) -> bool:
    """Whether this fighter's rating is being held down by a layoff."""
    return ledger.last_fight is not None and (on - ledger.last_fight) > DECAY_GRACE


def is_eligible(ledger: Ledger, on: date) -> bool:
    """Ranked at all: enough UFC fights, and seen recently enough to still be active."""
    return _eligible(ledger, on)


def _eligible(ledger: Ledger, on: date) -> bool:
    if ledger.fights < MIN_FIGHTS or ledger.last_fight is None:
        return False
    return on - ledger.last_fight <= ACTIVE_WITHIN


def _ordered(
    ledgers: dict[str, Ledger],
    division: str | None,
    *,
    on: date,
    include_women: bool,
) -> list[Ranked]:
    """Everyone eligible, best first, with ranks shared between fighters too close to separate."""
    entries = [
        (rating_on(ledger, on), key, ledger)
        for key, ledger in ledgers.items()
        if _eligible(ledger, on)
        and (division is None or ledger.division == division)
        and (include_women or not is_womens(ledger.division))
    ]
    # Name breaks the remaining tie so the same board comes back the same way
    # twice running, which is what the change watcher compares against.
    entries.sort(key=lambda entry: (-entry[0], entry[2].name))

    ranked: list[Ranked] = []
    rank, leader = 0, None
    for place, (rating, key, ledger) in enumerate(entries, 1):
        if leader is None or leader - rating > TIE_GAP:
            rank, leader = place, rating
        ranked.append(
            Ranked(
                rank=rank,
                name=ledger.name,
                rating=rating,
                record=ledger.record,
                division=ledger.division,
                key=key,
                last_fight=ledger.last_fight,
                last_result=ledger.last_result,
            )
        )

    shared = Counter(entry.rank for entry in ranked)
    for entry in ranked:
        entry.tied = shared[entry.rank] > 1
    return ranked


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
    return _ordered(ledgers, division, on=on, include_women=include_women)[:depth]


def standing(ledgers: dict[str, Ledger], key: str, *, on: date) -> Ranked | None:
    """Where one fighter sits in their own division, or None when they are not ranked.

    Unbounded depth: a fighter 30th in their division is still worth telling,
    where the boards only print the top of each.
    """
    ledger = ledgers.get(key)
    if ledger is None or ledger.division is None or not _eligible(ledger, on):
        return None
    return _find(_ordered(ledgers, ledger.division, on=on, include_women=True), key)


def pound_for_pound_rank(ledgers: dict[str, Ledger], key: str, *, on: date) -> Ranked | None:
    """Where a fighter sits across every division, or None when they are not ranked."""
    ledger = ledgers.get(key)
    if ledger is None or not _eligible(ledger, on):
        return None
    return _find(_ordered(ledgers, None, on=on, include_women=True), key)


def _find(ranked: list[Ranked], key: str) -> Ranked | None:
    return next((entry for entry in ranked if entry.key == key), None)


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
