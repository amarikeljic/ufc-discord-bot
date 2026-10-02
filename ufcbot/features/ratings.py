"""Watches the ratings boards and announces how they moved, and why.

A board is redrawn every pass, which shows where everyone stands but never what
changed. This keeps each division's board as it was last published and compares
it, so a card can be followed by "X is up to 3rd after a win" rather than a
silently edited list.

Only the divisional boards are watched. Pound for pound is not, because a server
that leaves the women's divisions out has a different pound-for-pound list from
one that does not, and there is no single set of changes to announce.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date

import discord

from ..embeds import ratings_changes_embed
from ..records import GuildSettings, RankedState
from ..stats.career import Ledger
from ..stats.rankings import (
    divisions_with_fighters,
    is_eligible,
    is_fading,
    is_womens,
    rank_division,
)
from ..storage import Storage
from .cardwatch import news_channel

log = logging.getLogger(__name__)

ENTERED = "entered"
LEFT = "left"
UP = "up"
DOWN = "down"

# Why a fighter's standing moved.
AFTER_RESULT = {"win": "a win", "loss": "a loss", "draw": "a draw", "nc": "a no contest"}
INACTIVE = "not having fought in eighteen months"
LAYOFF = "a long layoff"
PUSHED = "results around them"


@dataclass(slots=True)
class RatingChange:
    kind: str
    name: str
    was: int | None
    now: int | None
    rating: int | None
    reason: str


def _reason(current, previous: RankedState | None, ledger: Ledger | None, on: date) -> str:
    """Why this fighter moved: their own last fight, their own absence, or everyone else's."""
    fought = current is not None and current.last_fight != (previous.last_fight if previous else None)
    if fought and current.last_result:
        return AFTER_RESULT.get(current.last_result, "a fight")
    if current is None:
        # Gone from the board: either aged out, or pushed below the cut by others.
        if ledger is None or not is_eligible(ledger, on):
            return INACTIVE
        return PUSHED
    if ledger is not None and is_fading(ledger, on):
        # Still ranked, but slipping under their own rating rather than anyone
        # else's results.
        return LAYOFF
    return PUSHED


def _still_eligible(ledger: Ledger | None, on: date) -> bool:
    return ledger is not None and is_eligible(ledger, on)


def diff(
    previous: dict[str, RankedState],
    current: list,
    ledgers: dict[str, Ledger],
    *,
    on: date,
) -> list[RatingChange]:
    """What moved on one board since it was last published.

    A move is reported only when one of the two fighters who swapped had a fight
    behind it. The displayed rating fades by the day a fighter is idle, so a
    board drifts on its own: simulated over 180 days with no fights at all it
    produced 130 reported moves, 112 of them nobody passing anybody. None of it
    was news.

    Asking only whether *anybody* fought is not enough, and is worse than saying
    nothing. The UFC runs most weekends, so a pass-level gate holds a week of
    drift and then releases all of it on the first pass after a card -- where it
    reads as a consequence of that card rather than of the calendar.
    """
    changes: list[RatingChange] = []
    now_by_key = {entry.key: entry for entry in current}
    fought = {
        entry.key
        for entry in current
        if entry.key in previous and entry.last_fight != previous[entry.key].last_fight
    }
    # Who aged out rather than being pushed down the list. Their replacement is
    # news with them; anyone else arriving or leaving is the same drift again,
    # reaching the board through its ends instead of its middle.
    aged_off = {
        key
        for key, was in previous.items()
        if key not in now_by_key and not _still_eligible(ledgers.get(key), on)
    }
    # A fighter arriving was not on the board to be compared against, so "did
    # they fight" is asked of the board instead: a last fight later than anything
    # the board knew about means a card has happened since it was published.
    newest_known = max((was.last_fight for was in previous.values() if was.last_fight), default=None)

    def contradicts_the_result(entry, was) -> bool:
        """A rise after a loss, or a fall after a win.

        The fade comes off the moment a fighter fights, so someone returning
        from the fade band gets their layoff back and pays for the result out of
        it. Carrying more than half of K -- sixteen points, which is about
        fourteen months idle for a fighter two hundred above the starting rating
        -- they come back from a *loss* with a higher number than they left with,
        and the board moves them up. Poirier is carrying twenty-one points of it
        today and Dos Anjos thirty-eight.

        The board can show the reset, because it is where the rating now is. The
        sentence about it cannot, because there is no sentence about a man losing
        and climbing that is not simply wrong.
        """
        if entry.key not in fought or not entry.last_result:
            return False
        rose = entry.rank < was.rank
        return rose == (entry.last_result == "loss")

    def worth_saying(entry) -> bool:
        """Did this fighter, or anyone they actually passed, have a fight?"""
        if entry.key in fought:
            return True
        for other in current:
            if other.key == entry.key or other.key not in fought or other.key not in previous:
                continue
            before = previous[entry.key].rank - previous[other.key].rank
            after = entry.rank - other.rank
            if before * after < 0:  # they were on opposite sides of each other
                return True
        return False

    for entry in current:
        was = previous.get(entry.key)
        reason = _reason(entry, was, ledgers.get(entry.key), on)
        if was is None:
            # Arriving is only news with a fight behind it, or as the other half
            # of someone ageing off. Drifting up into fifteenth because the
            # fighter above faded is the same non-event as drifting within it.
            arrived_by_fighting = (
                newest_known is None
                or (entry.last_fight is not None and entry.last_fight > newest_known)
            )
            if arrived_by_fighting or aged_off:
                changes.append(RatingChange(ENTERED, entry.name, None, entry.rank, entry.rating, reason))
        elif entry.rank == was.rank or contradicts_the_result(entry, was) or not worth_saying(entry):
            continue
        elif entry.rank < was.rank:
            changes.append(RatingChange(UP, entry.name, was.rank, entry.rank, entry.rating, reason))
        else:
            changes.append(RatingChange(DOWN, entry.name, was.rank, entry.rank, entry.rating, reason))

    for key, was in previous.items():
        # Dropping out of the top fifteen without a fight is the same drift as
        # slipping a place inside it. Ageing out at the eighteen-month cutoff is
        # a change of state, and the one thing about an idle fighter worth saying.
        # Leaving the board cannot be tested for crossings, because the fighter
        # is no longer there to compare anyone against. Ageing out is always
        # news; being pushed below fifteenth is news when a result could have
        # done it, and nothing when the board merely got a day older.
        if key not in now_by_key and (key in aged_off or fought):
            ledger = ledgers.get(key)
            name = ledger.name if ledger else key
            changes.append(RatingChange(LEFT, name, was.rank, None, None, _reason(None, was, ledger, on)))

    # Biggest movers first, and anyone entering or leaving ahead of a shuffle.
    order = {ENTERED: 0, LEFT: 1, UP: 2, DOWN: 3}
    changes.sort(key=lambda c: (order[c.kind], c.now or c.was or 99))
    return changes


class RatingsWatch:
    """Finds board changes once, then tells every guild that wants to hear them."""

    def __init__(self, storage: Storage) -> None:
        self.storage = storage

    async def poll(self, ledgers: dict[str, Ledger], *, on: date | None = None) -> list[tuple[str, list[RatingChange]]]:
        """Compare every division's board with the last published one and record it."""
        if not ledgers:
            return []
        today = on or date.today()
        found: list[tuple[str, list[RatingChange]]] = []

        for division in divisions_with_fighters(ledgers, on=today):
            current = rank_division(ledgers, division, on=today)
            previous = await self.storage.ranking_state(division)
            await self.storage.save_ranking_state(
                division,
                [RankedState(e.key, e.rank, e.rating, e.last_fight) for e in current],
            )
            if not previous:
                # First time this board has been built; the whole board is not news.
                continue
            changes = diff(previous, current, ledgers, on=today)
            if changes:
                log.info("%s ratings moved: %d changes", division, len(changes))
                found.append((division, changes))
        return found

    async def announce(
        self,
        guild: discord.Guild,
        settings: GuildSettings,
        changes: list[tuple[str, list[RatingChange]]],
    ) -> int:
        """Post the moves to this guild's live channel, or its schedule channel."""
        if not changes or not settings.rankings_channel_id:
            # Nothing to say, or this server does not keep ratings boards at all.
            return 0
        channel = news_channel(guild, settings)
        if channel is None:
            return 0

        posted = 0
        for division, moves in changes:
            if not settings.rankings_include_women and is_womens(division):
                continue
            try:
                await channel.send(embed=ratings_changes_embed(division, moves))
                posted += 1
            except discord.HTTPException as exc:
                log.warning("Ratings update failed in #%s: %r", channel.name, exc)
                return posted
        return posted
