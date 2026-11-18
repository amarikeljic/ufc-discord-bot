"""When each fight on a card is likely to start, for the pick'em board.

Nobody publishes this, because nobody knows it: ESPN dates every fight on a part
of the card with that part's start, and a fight begins when the one before it
ends. So it is worked out from the parts' air times and how a fight night runs,
measured on ESPN's play-by-play for 341 fights on 27 cards, May to November 2026:

    air time to the first bell         early prelims 14 min, prelims 14, main card 19
    last bell to the next first bell   prelims 11 min after a decision, 16 after a finish
                                       main card 18.5 after a decision, 20.5 after a finish
    a round is 5 minutes and the break between rounds 72 seconds; 39% of
    three-rounders and 31% of five-rounders go the distance, and a finish lasts
    6.1 minutes on average in a three-rounder and 13 in a five

The early prelims and prelims run to fill their window -- a part of the card
going long or short is padded or rushed to hand over to the next one on time --
so their fights are spread across it. The main card has nothing after it to
fill to, so its fights are chained on expected fight length and gap.

Once a card is under way every estimate is redone from what the play-by-play
says: the fight after one that has ended is due its gap after that fight's last
bell, a decision or a finish, and everything after chains on from there. A fight
running late is due at the next five-minute mark, never in the past, and the
fights after it move with it. Every time is shown to the nearest five minutes,
which is as exact as any of this is.

Backtested with this code on the same 27 cards, against each fight's real first
bell and with the times rounded as the board shows them:

    a day out, from the schedule        half within 5 min, 80% within 11, 90% within 18
    under way, the next fight up        half within 3 min, 80% within 5,  90% within 7
    under way, the fight after that     half within 5 min, 80% within 10, 90% within 13
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta

from ..models import Bout, Event, segment_rank

STEP = timedelta(minutes=5)
WALK_IN = {2: timedelta(minutes=14), 1: timedelta(minutes=14), 0: timedelta(minutes=19)}
GAP_AFTER = {
    (2, True): timedelta(minutes=11), (2, False): timedelta(minutes=15),
    (1, True): timedelta(minutes=11), (1, False): timedelta(minutes=16),
    (0, True): timedelta(minutes=18.5), (0, False): timedelta(minutes=20.5),
}
ROUND = timedelta(minutes=5)
BREAK = timedelta(seconds=72)
DISTANCE = {3: 0.39, 5: 0.31}
FINISH = {3: timedelta(minutes=6.1), 5: timedelta(minutes=13)}


@dataclass(frozen=True, slots=True)
class FightClock:
    """What the play-by-play says about a fight that has begun."""

    first_bell: datetime
    last_bell: datetime | None = None
    """None while it is still going."""

    def went_the_distance(self, rounds: int | None) -> bool:
        return self.last_bell is not None and self.last_bell - self.first_bell >= _full(rounds) - timedelta(seconds=30)


def _full(rounds: int | None) -> timedelta:
    """A fight that goes the distance, first bell to last."""
    rounds = rounds or 3
    return rounds * ROUND + (rounds - 1) * BREAK


def _kind(rounds: int | None) -> int:
    return 5 if (rounds or 3) >= 4 else 3


def expected_length(bout: Bout) -> timedelta:
    """First bell to last, averaged over going the distance and being finished."""
    p = DISTANCE[_kind(bout.rounds)]
    return p * _full(bout.rounds) + (1 - p) * FINISH[_kind(bout.rounds)]


def _expected_gap(rank: int, bout: Bout) -> timedelta:
    p = DISTANCE[_kind(bout.rounds)]
    return p * GAP_AFTER[(rank, True)] + (1 - p) * GAP_AFTER[(rank, False)]


def _parts(event: Event) -> list[tuple[int, datetime, list[Bout]]]:
    """(rank, air time, bouts in running order) for each part of the card, earliest first.

    Every bout on a part counts, named or not, because an empty slot is still a
    fight's worth of the night. A higher match number runs earlier: across the
    341 fights measured it never once did otherwise."""
    groups: dict[int, list[Bout]] = {}
    for bout in event.bouts:
        rank = segment_rank(bout.segment)
        if rank <= 2:
            groups.setdefault(rank, []).append(bout)
    parts = []
    for rank in sorted(groups, reverse=True):
        bouts = sorted(groups[rank], key=lambda b: -(b.match_number if b.match_number is not None else -1))
        air = event.segment_starts.get(rank) or min((b.start for b in bouts if b.start), default=None)
        if air is not None:
            parts.append((rank, air, bouts))
    return parts


def _shown(when: datetime, now: datetime) -> datetime:
    """To the nearest five minutes, and never a time already gone."""
    step = STEP.total_seconds()
    rounded = round(when.timestamp() / step) * step
    if rounded < now.timestamp():
        rounded = math.ceil(now.timestamp() / step) * step
    return datetime.fromtimestamp(rounded, tz=when.tzinfo)


def estimate_first_bells(
    event: Event, *, now: datetime, clocks: dict[str, FightClock] | None = None
) -> dict[str, datetime]:
    """Bout id -> when its first bell is likely, for every fight that has not begun.

    ``clocks`` are the fights the play-by-play says have begun. Without any, the
    estimate is the schedule alone, which is all there is before the card."""
    clocks = clocks or {}
    starts: dict[str, datetime] = {}
    parts = _parts(event)
    carry: datetime | None = None  # the earliest the next part can begin, if this one runs late

    for index, (rank, air, bouts) in enumerate(parts):
        first = air + WALK_IN[rank]
        if carry is not None:
            first = max(first, carry)
        after = parts[index + 1][1] if index + 1 < len(parts) else None
        anchor: datetime | None = None  # set once a fight on this part is known to have begun
        chain = first

        for position, bout in enumerate(bouts):
            clock = clocks.get(bout.id)
            if clock is not None:
                if clock.last_bell is not None:
                    anchor = clock.last_bell + GAP_AFTER[(rank, clock.went_the_distance(bout.rounds))]
                else:
                    anchor = max(clock.first_bell + expected_length(bout), now) + _expected_gap(rank, bout)
                chain = anchor
                continue
            if bout.completed:
                continue  # over, and nothing says when; the next fight goes on the schedule
            if anchor is not None:
                due = anchor
            elif after is not None:
                due = first + (after - first) * position / len(bouts)
            else:
                due = chain
            due = max(due, now)  # running late: due now, and the rest move with it
            starts[bout.id] = _shown(due, now)
            chain = due + expected_length(bout) + _expected_gap(rank, bout)
            if anchor is not None:
                anchor = chain
        # A part that has begun and is running long holds up the next one.
        carry = anchor
    return starts


async def fight_clocks(data, event: Event, *, now: datetime, ttl: int = 60) -> dict[str, FightClock]:
    """What the play-by-play says about the fights an estimate is chained from.

    Per part of the card already on air: its last finished fight and the fight
    after it, which is at most two requests a part, and nothing before the card."""
    clocks: dict[str, FightClock] = {}
    for _rank, air, bouts in _parts(event):
        if air > now:
            continue
        done = [bout for bout in bouts if bout.completed]
        waiting = [bout for bout in bouts if not bout.completed]
        for bout in done[-1:] + waiting[:1]:
            plays = await data.load_plays(bout, ttl=ttl)
            first = next((p["wallclock"] for p in plays if p["type"] == "Round Start" and p["period"] == 1), None)
            if first is None or first > now:
                continue
            last = next((p["wallclock"] for p in plays if p["type"] == "Fight Over"), None)
            clocks[bout.id] = FightClock(first, last if last is not None and last <= now else None)
    return clocks
