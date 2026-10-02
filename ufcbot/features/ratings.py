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
    DECAY_GRACE,
    DEPTH,
    all_ranked,
    divisions_with_fighters,
    is_eligible,
    is_fading,
    is_womens,
)
from ..storage import Storage
from .cardwatch import news_channel

log = logging.getLogger(__name__)

ENTERED = "entered"
LEFT = "left"
UP = "up"
DOWN = "down"
RETURNED = "returned"
"""Back from a layoff long enough to have been fading.

Reported as a result and a place rather than as a direction. The fade comes off
the moment a fighter fights, so a returning fighter is handed their layoff back
and pays for the result out of it -- a loss can leave them higher than they went
in. Saying "up to 4th" of a man who just lost is wrong, and saying nothing is
worse: it would report the wins of returning fighters and not their losses,
which flatters exactly the fighters least able to carry it."""

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


def _returning(entry, was: RankedState, on: date) -> bool:
    """Back from a layoff long enough to have been fading."""
    return was.last_fight is not None and on - was.last_fight > DECAY_GRACE


def diff(
    previous: dict[str, RankedState],
    current: list,
    ledgers: dict[str, Ledger],
    *,
    on: date,
    depth: int = DEPTH,
) -> list[RatingChange]:
    """What moved on one board since it was last published.

    ``current`` is the whole ranked division, not the published top fifteen, and
    ``previous`` is the whole of it as it stood last time. Crossing the fifteenth
    line is then an ordinary crossing between the fighters at fifteen and sixteen
    rather than a case of its own, which is what entering and leaving used to
    need rules for. Only moves touching the published ``depth`` are reported, so
    a shuffle at fortieth is seen and not mentioned.

    A move is reported when the fighter, or somebody who actually passed them,
    had a fight. The displayed rating fades by the day a fighter is idle, so a
    board drifts on its own: 180 days with no fights at all produced 130 reported
    moves, 112 of them nobody passing anybody.

    Crossings are decided on the raw rating rather than the displayed one,
    because a fighter coming back is handed the fade back on top of the result
    and every place they climb inside it is the layoff ending, not the fight.
    """
    changes: list[RatingChange] = []
    now_by_key = {entry.key: entry for entry in current}
    place = {entry.key: i for i, entry in enumerate(current, 1)}
    fought = {
        entry.key
        for entry in current
        if entry.key not in previous or entry.last_fight != previous[entry.key].last_fight
    }

    def raw_before(key: str) -> int | None:
        """Where this fighter stood before the pass, with nothing faded off.

        A fighter who fought and was not on the last board -- off the bottom of
        it, or gone past the eighteen-month cutoff -- has no row to read, so the
        rating they carried into that fight stands in for one. It is the same
        question asked of a fighter who was there, and it is the only thing that
        covers a comeback from off the board.
        """
        was = previous.get(key)
        if was is not None and was.raw is not None:
            return was.raw
        if key in fought:
            ledger = ledgers.get(key)
            if ledger is not None:
                return round(ledger.elo_before_last)
        return None

    def crossed_someone_who_fought(entry) -> bool:
        """Did this fighter change places with anybody who had a fight?

        Either direction: a man passing somebody who fought and a man passed by
        somebody who fought are both moves with a result behind them. What is
        excluded is the pair who ended up on opposite sides of each other with
        neither of them fighting, which is the fade moving them.

        Going in is measured on the raw rating and coming out on the new order,
        so a returning fighter being handed his layoff back does not read as
        having passed anyone he was already above.
        """
        mine = raw_before(entry.key)
        if mine is None:
            return False
        for other in current:
            if other.key == entry.key or other.key not in fought:
                continue
            theirs = raw_before(other.key)
            if theirs is None:
                continue
            if (mine > theirs) != (place[entry.key] < place[other.key]):
                return True
        return False

    for entry in current:
        was = previous.get(entry.key)
        was_ranked = was is not None and was.rank <= depth
        now_ranked = place[entry.key] <= depth
        if not was_ranked and not now_ranked:
            continue  # a shuffle below the board is not the board moving
        if entry.key not in fought and not crossed_someone_who_fought(entry):
            continue  # nobody fought their way into this; it is the fade
        reason = _reason(entry, was, ledgers.get(entry.key), on)

        if not was_ranked:
            changes.append(RatingChange(ENTERED, entry.name, None, entry.rank, entry.rating, reason))
        elif not now_ranked:
            changes.append(RatingChange(LEFT, entry.name, was.rank, None, entry.rating, reason))
        elif entry.rank == was.rank:
            continue
        elif _returning(entry, was, on):
            # No direction word: the place is where they are and the result is
            # what happened, and the two are not joined the way "up to 4th" says.
            changes.append(RatingChange(RETURNED, entry.name, was.rank, entry.rank, entry.rating, reason))
        elif entry.rank < was.rank:
            changes.append(RatingChange(UP, entry.name, was.rank, entry.rank, entry.rating, reason))
        else:
            changes.append(RatingChange(DOWN, entry.name, was.rank, entry.rank, entry.rating, reason))

    for key, was in previous.items():
        # Gone from the ranked list altogether: eighteen months without a fight.
        # Everything else now leaves by crossing somebody, above.
        if key not in now_by_key and was.rank <= depth:
            ledger = ledgers.get(key)
            name = ledger.name if ledger else key
            changes.append(RatingChange(LEFT, name, was.rank, None, None, _reason(None, was, ledger, on)))

    # Biggest movers first, and anyone entering or leaving ahead of a shuffle.
    order = {ENTERED: 0, LEFT: 1, RETURNED: 2, UP: 3, DOWN: 4}
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
            # The whole division, not the published fifteen. Crossing the
            # fifteenth line is then an ordinary crossing rather than a case of
            # its own, and a fighter who faded off the bottom still has a
            # position to be compared against when he comes back.
            current = all_ranked(ledgers, division, on=today)
            previous = await self.storage.ranking_state(division)
            await self.storage.save_ranking_state(
                division,
                [RankedState(e.key, e.rank, e.rating, e.last_fight, e.raw) for e in current],
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
