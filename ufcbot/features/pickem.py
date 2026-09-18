"""Pick'em: members pick fight winners and score points from the odds.

The game runs on one card at a time, the next UFC card. A correct pick scores
what a 100-point bet would win at the moneyline when the pick was made, so
underdogs pay more than favourites. A wrong pick costs 100 points, favourite or
not. Draws, no contests and cancelled bouts are void. Picks open once a
sportsbook posts odds for both fighters and can be changed until that part of
the card (prelims or main card) starts.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from ..models import Bout, Event
from ..sources.espn import UFCData
from ..storage import PickemRecord, Storage
from ..util import format_odds, normalise
from .tracking import VOID_AFTER

log = logging.getLogger(__name__)

STAKE = 100
WRONG_PICK_POINTS = -STAKE
# A card counts as over this long after it starts, even if a result never posts.
CARD_LENGTH = timedelta(hours=12)

OPEN = "open"
LOCKED = "locked"
NO_ODDS = "no_odds"


def points_for(odds: int) -> int:
    """What a 100-point bet wins at an American moneyline: +235 -> 235, -290 -> 34."""
    if odds > 0:
        return round(STAKE * odds / 100)
    if odds < 0:
        return max(1, round(STAKE * 100 / -odds))
    # No sportsbook prices a fight at zero, but a bad feed can say so, and this
    # runs while a board is being drawn and again for every stored pick at startup.
    return STAKE


def card_over(event: Event, now: datetime) -> bool:
    """Every fight has a winner, or the card started long enough ago that it must be done."""
    if now - event.start > CARD_LENGTH:
        return True
    fights = event.fights
    return event.start <= now and bool(fights) and all(bout.completed for bout in fights)


def lock_time(event: Event, bout: Bout) -> datetime:
    """Picks for a bout lock when its part of the card starts."""
    return bout.start or event.start


def bout_status(event: Event, bout: Bout, now: datetime) -> str:
    if bout.completed or now >= lock_time(event, bout):
        return LOCKED
    if not bout.has_opponents or any(f.id not in bout.odds for f in bout.fighters[:2]):
        return NO_ODDS
    return OPEN


def segment_locks(event: Event) -> list[tuple[str, datetime]]:
    """(segment, lock time) for each part of the card, earliest first."""
    locks: dict[str, datetime] = {}
    for bout in event.fights:
        name = bout.segment or "Card"
        when = lock_time(event, bout)
        locks[name] = min(locks.get(name, when), when)
    return sorted(locks.items(), key=lambda kv: kv[1])


@dataclass(slots=True)
class PickOutcome:
    ok: bool
    message: str
    pick: PickemRecord | None = None


class PickemService:
    def __init__(self, data: UFCData, storage: Storage, *, clock: Callable[[], datetime] | None = None) -> None:
        self.data = data
        self.storage = storage
        self.clock = clock or (lambda: datetime.now(UTC))

    def now(self) -> datetime:
        return self.clock()

    async def load_event(self, event_id: str) -> Event | None:
        """A card with fighters and current odds, fresh enough to take picks against."""
        event = await self.data.get_event(event_id, ttl=60)
        if event is not None:
            await self.data.load_odds(event, ttl=300)
        return event

    async def current_event(self) -> Event | None:
        """The one card pick'em runs on: the soonest UFC card that isn't over.

        Contender Series cards are skipped. Once a card is over, the next card
        takes its place.
        """
        now = self.now()
        recent, upcoming = await asyncio.gather(
            self.data.recent_events(days=2, limit=3), self.data.upcoming_events(days=60, limit=6)
        )
        seen: set[str] = set()
        for summary in sorted(recent + upcoming, key=lambda event: event.start):
            if summary.id in seen or "contender series" in normalise(summary.name):
                continue
            seen.add(summary.id)
            if now - summary.start > CARD_LENGTH:
                continue
            event = await self.load_event(summary.id)
            if event is not None and not card_over(event, now):
                return event
        return None

    async def make_pick(self, guild_id: int, user_id: int, event_id: str, bout_id: str, athlete_id: str) -> PickOutcome:
        """Save or change a pick, checked against fresh odds and lock times."""
        event = await self.current_event()
        if event is None or event.id != event_id:
            where = f"only open for {event.name}" if event else "not open for any card right now"
            return PickOutcome(False, f"Pick'em is {where}.")
        bout = next((b for b in event.fights if b.id == bout_id), None)
        if bout is None:
            return PickOutcome(False, "That fight is no longer on the card.")

        status = bout_status(event, bout, self.now())
        if status == LOCKED:
            return PickOutcome(False, f"{bout.matchup} is locked.")
        if status == NO_ODDS:
            return PickOutcome(False, f"Odds aren't posted for {bout.matchup} yet.")

        picked = bout.fighter(athlete_id)
        opponent = bout.opponent(athlete_id)
        if picked is None or opponent is None:
            return PickOutcome(False, "That fighter isn't in this bout any more.")

        odds = bout.odds[picked.id]
        record = PickemRecord(
            guild_id=guild_id,
            user_id=user_id,
            espn_event_id=event.id,
            bout_id=bout.id,
            event_name=event.name,
            event_start=event.start,
            athlete_id=picked.id,
            athlete_name=picked.display_name,
            opponent_id=opponent.id,
            opponent_name=opponent.display_name,
            odds=odds,
            points_if_right=points_for(odds),
            locks_at=lock_time(event, bout),
            picked_at=self.now(),
        )
        await self.storage.save_pickem_pick(record)
        return PickOutcome(
            True,
            f"Saved: {picked.display_name} ({format_odds(odds)}) · +{record.points_if_right} pts if right",
            record,
        )

    async def retire_missing(self, event: Event) -> int:
        """Void picks on fights that have left the card, before the card is graded.

        A fight scratched days out would otherwise sit in everyone's picks as
        pending, and in the points they are playing for, until three days after
        the card had come and gone: grading only looks at bouts that have locked,
        and never hears about one that is simply no longer there.
        """
        if not event.bouts or event.partial:
            # Voiding cannot be taken back, so it waits for a card that read cleanly.
            return 0

        voided = 0
        for bout_id, _locks_at in await self.storage.pickem_ungraded_bouts(event.id):
            bout = next((b for b in event.bouts if b.id == bout_id), None)
            if bout is not None and bout.awaiting_names:
                continue  # still on the card; this bot just has not named it yet
            fighters = {f.id for f in bout.fighters[:2]} if bout is not None else None
            voided += await self.storage.void_pickem_picks(bout_id, fighters=fighters)
        if voided:
            log.info("Voided %d pick(s) on fights no longer on %s", voided, event.name)
        return voided

    async def grade_due(self) -> int:
        """Score every pick whose bout has a result. Returns the number of picks graded."""
        now = self.now()
        graded = 0

        # Fights come off a card all week, not just once it is over.
        current = await self.current_event()
        if current is not None:
            graded += await self.retire_missing(current)

        for event_id in await self.storage.pickem_events_awaiting_grading(now):
            event = await self.data.get_event(event_id, ttl=120)
            if event is None:
                continue
            bouts = {bout.id: bout for bout in event.bouts}

            for bout_id, locks_at in await self.storage.pickem_ungraded_bouts(event_id):
                expired = now - locks_at > VOID_AFTER
                bout = bouts.get(bout_id)
                winner = None
                if bout is not None and bout.completed and bout.winner_id:
                    winner = str(bout.winner_id)
                elif bout is not None and not expired:
                    await self.data.load_status(bout, ttl=120)
                    result = (bout.result_method or "").lower()
                    if not (bout.state == "post" and ("draw" in result or "contest" in result)):
                        continue  # no result yet
                elif not expired:
                    continue  # bout missing from the card; wait before voiding it
                graded += await self.storage.grade_pickem_bout(
                    bout_id,
                    winner,
                    loss_points=WRONG_PICK_POINTS,
                    # Whoever was in there at the bell. A member who picked a
                    # fighter who then withdrew is voided, not marked wrong.
                    fighters={f.id for f in bout.fighters[:2]} if bout is not None else None,
                )

        if graded:
            log.info("Graded %d pick'em picks", graded)
        return graded

    async def apply_scoring(self) -> int:
        """Bring stored picks in line with the current scoring rules. Returns rows changed."""
        changes = [
            (points_for(pick.odds), pick.guild_id, pick.user_id, pick.bout_id)
            for pick in await self.storage.pickem_open_picks()
            if pick.points_if_right != points_for(pick.odds)
        ]
        changed = await self.storage.set_pickem_points(changes)
        changed += await self.storage.set_pickem_loss_points(WRONG_PICK_POINTS)
        if changed:
            log.info("Rescored %d pick'em picks", changed)
        return changed
