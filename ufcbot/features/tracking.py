"""Keeps the model honest: records picks before a card, grades them after.

A pick, with its predicted method and the sportsbook line, is written and
rewritten freely until the card starts, then frozen. Once ESPN posts results
each pick is marked right, wrong, or void (draw, no contest, cancelled bout),
along with whether the method and technique were called.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

from ..models import Event
from ..records import PredictionRecord
from ..sources.espn import UFCData
from ..stats.prediction import Prediction
from ..stats.techniques import (
    FINISHES,
    METHODS,
    method_from_espn,
    same_technique,
    technique_from_espn,
)
from ..storage import Storage

log = logging.getLogger(__name__)

# Do not look for results until the card has plausibly finished.
GRADE_AFTER = timedelta(hours=2)

# A bout with no result this long after the card was cancelled and is scored void.
VOID_AFTER = timedelta(days=3)

CONFIDENCE_BANDS = (
    ("50–60%", 0.50, 0.60),
    ("60–70%", 0.60, 0.70),
    ("70–80%", 0.70, 0.80),
    ("80%+", 0.80, 1.01),
)


@dataclass(slots=True)
class GradedEvent:
    espn_event_id: str
    name: str
    start: datetime
    records: list[PredictionRecord]

    @property
    def scored(self) -> list[PredictionRecord]:
        return [r for r in self.records if r.correct is not None]

    @property
    def correct(self) -> int:
        return sum(1 for r in self.scored if r.correct)

    @property
    def total(self) -> int:
        return len(self.scored)

    @property
    def method_total(self) -> int:
        return sum(1 for r in self.scored if r.method_correct is not None)

    @property
    def method_hits(self) -> int:
        return sum(1 for r in self.scored if r.method_correct)


@dataclass(slots=True)
class Scorecard:
    since: date | None
    correct: int = 0
    total: int = 0
    void: int = 0
    confidence_sum: float = 0.0
    method_hits: int = 0
    method_total: int = 0
    technique_hits: int = 0
    technique_total: int = 0
    market_hits: int = 0
    market_total: int = 0
    disagree_hits: int = 0
    disagree_total: int = 0
    bands: list[tuple[str, int, int]] = field(default_factory=list)
    events: list[GradedEvent] = field(default_factory=list)
    """Most recent card first."""
    streak: int = 0
    """Positive for consecutive hits, negative for consecutive misses."""

    @property
    def accuracy(self) -> float | None:
        return self.correct / self.total if self.total else None

    @property
    def avg_confidence(self) -> float | None:
        return self.confidence_sum / self.total if self.total else None


class PredictionTracker:
    def __init__(self, storage: Storage, data: UFCData) -> None:
        self.storage = storage
        self.data = data

    # -- recording -----------------------------------------------------------

    async def record(self, event: Event, picks: dict[str, Prediction]) -> int:
        """Write current picks and lines for a card that has not started. Returns rows written.

        Load odds onto the event first (``UFCData.load_odds``) to store the line.
        """
        now = datetime.now(UTC)
        if event.start <= now:
            return 0
        if not event.bouts:
            # The fight card did not load. Leaving what is on record alone beats
            # reading an empty card as every fight on it having been cancelled.
            return 0

        writing: list[PredictionRecord] = []
        for position, bout in enumerate(event.ordered_bouts()):
            pick = picks.get(bout.id)
            if pick is None or not bout.has_opponents:
                continue
            a, b = bout.fighters[0], bout.fighters[1]
            outcome = pick.pick_outcome
            writing.append(
                PredictionRecord(
                    espn_event_id=event.id,
                    bout_id=bout.id,
                    event_name=event.name,
                    event_start=event.start,
                    athlete_a=a.id,
                    name_a=a.display_name,
                    athlete_b=b.id,
                    name_b=b.display_name,
                    prob_a=pick.prob_a,
                    weight_class=bout.weight_class,
                    position=position,
                    odds_a=bout.odds.get(a.id),
                    odds_b=bout.odds.get(b.id),
                    method=outcome[0] if outcome else None,
                    technique=outcome[1] if outcome else None,
                    method_prob=outcome[2] if outcome else None,
                    detail_json=json.dumps(pick.to_dict()) if pick.has_methods else None,
                )
            )

        await self.storage.upsert_predictions(writing)

        # Drop a pick that no longer describes a fight on the card: the bout is
        # gone, or one of the two fighters has been replaced. A pick kept in
        # either case would be graded against a fight it was never made for.
        # Note that a bout whose fighters are unchanged keeps its pick even when
        # the model has nothing to say about it this time round, so a card does
        # not empty out while the model is still loading.
        stale = []
        for stored in await self.storage.predictions_for_event(event.id):
            if stored.graded_at is not None:
                continue
            if event.still_carded(stored.bout_id, {stored.athlete_a, stored.athlete_b}) is False:
                log.info("Dropping the pick for %s: it is no longer on %s", stored.matchup, event.name)
                stale.append(stored.bout_id)
        await self.storage.delete_predictions(event.id, stale)
        return len(writing)

    async def records_for(self, espn_event_id: str) -> list[PredictionRecord]:
        return await self.storage.predictions_for_event(espn_event_id)

    # -- grading -------------------------------------------------------------

    async def grade_due(self) -> list[GradedEvent]:
        """Score every pick whose card has finished. Returns cards that became fully graded."""
        now = datetime.now(UTC)
        completed: list[GradedEvent] = []

        for event_id, name, start in await self.storage.events_awaiting_grading(now - GRADE_AFTER):
            event = await self.data.get_event(event_id, ttl=120)
            if event is None:
                log.debug("Could not load %s for grading; will retry", name)
                continue

            bouts = {bout.id: bout for bout in event.bouts}
            expired = now - start > VOID_AFTER
            graded_any = False

            for record in await self.storage.predictions_for_event(event_id):
                if record.graded_at is not None:
                    continue
                if await self._grade_one(event_id, record, bouts.get(record.bout_id), expired):
                    graded_any = True

            if graded_any and event_id not in await self.storage.events_with_ungraded_predictions():
                graded = GradedEvent(
                    espn_event_id=event_id,
                    name=event.name,
                    start=event.start,
                    records=await self.storage.predictions_for_event(event_id),
                )
                completed.append(graded)
                log.info("Graded %s: %d/%d", event.name, graded.correct, graded.total)

        return completed

    async def _grade_one(self, event_id: str, record: PredictionRecord, bout, expired: bool) -> bool:
        if bout is None:
            if expired:
                await self.storage.grade_prediction(event_id, record.bout_id, None, None)
                return True
            return False

        await self.data.load_status(bout, ttl=120)
        result_method = method_from_espn(bout.result_method)
        result_technique = technique_from_espn(result_method, bout.result_description, bout.result_target)
        finished = bout.completed or bout.state == "post"
        no_contest = "contest" in (bout.result_method or "").lower()
        result = {
            "result_method": result_method or ("nc" if no_contest else None),
            "result_technique": result_technique,
            "result_round": bout.period,
            "result_time": bout.clock,
        }

        if finished and bout.winner_id:
            winner = str(bout.winner_id)
            if winner not in (record.athlete_a, record.athlete_b):
                # A late replacement fought instead; the pick was for someone else.
                await self.storage.grade_prediction(event_id, record.bout_id, winner, None, **result)
                return True
            correct = winner == record.favourite_athlete
            method_correct = None
            technique_correct = None
            if record.method and result_method in METHODS:
                method_correct = correct and record.method == result_method
                if method_correct and result_method in FINISHES and record.technique:
                    technique_correct = same_technique(record.technique, result_technique)
            await self.storage.grade_prediction(
                event_id,
                record.bout_id,
                winner,
                correct,
                method_correct=method_correct,
                technique_correct=technique_correct,
                **result,
            )
            return True

        if finished and (result_method == "draw" or no_contest):
            await self.storage.grade_prediction(event_id, record.bout_id, None, None, **result)
            return True

        if expired:
            await self.storage.grade_prediction(event_id, record.bout_id, None, None)
            return True
        return False

    # -- reporting -----------------------------------------------------------

    async def graded_events(self, since: date | None) -> list[GradedEvent]:
        """Fully graded cards from ``since`` onward, oldest first."""
        unfinished = await self.storage.events_with_ungraded_predictions()
        by_event: dict[str, GradedEvent] = {}
        for record in await self.storage.graded_predictions(since):
            if record.espn_event_id in unfinished:
                continue
            entry = by_event.get(record.espn_event_id)
            if entry is None:
                entry = by_event[record.espn_event_id] = GradedEvent(
                    espn_event_id=record.espn_event_id,
                    name=record.event_name,
                    start=record.event_start,
                    records=[],
                )
            entry.records.append(record)
        return sorted(by_event.values(), key=lambda e: e.start)



def build_scorecard(since: date | None, events: list[GradedEvent]) -> Scorecard:
    """Summarise graded cards, given oldest first."""
    card = Scorecard(since=since)
    band_hits = {label: [0, 0] for label, _, _ in CONFIDENCE_BANDS}
    streak = 0

    for event in events:
        for record in event.records:
            if record.correct is None:
                card.void += 1
                continue
            card.total += 1
            card.confidence_sum += record.confidence
            if record.correct:
                card.correct += 1
                streak = streak + 1 if streak >= 0 else 1
            else:
                streak = streak - 1 if streak <= 0 else -1

            if record.method_correct is not None:
                card.method_total += 1
                card.method_hits += int(record.method_correct)
            if record.technique_correct is not None:
                card.technique_total += 1
                card.technique_hits += int(record.technique_correct)

            market = record.market_favourite_athlete
            if market and record.winner_athlete:
                card.market_total += 1
                card.market_hits += int(market == record.winner_athlete)
                if market != record.favourite_athlete:
                    card.disagree_total += 1
                    card.disagree_hits += int(record.correct)

            for label, low, high in CONFIDENCE_BANDS:
                if low <= record.confidence < high:
                    band_hits[label][1] += 1
                    band_hits[label][0] += int(record.correct)
                    break

    card.bands = [(label, hits, total) for label, (hits, total) in band_hits.items() if total]
    card.events = list(reversed(events))
    card.streak = streak
    return card
