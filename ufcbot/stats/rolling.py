"""Rolling-origin evaluation: every event scored once, by a model that never saw it.

A single held-out tail cannot settle the questions being asked of it. The 2024
onward holdout is 117 events, which gives a 95% interval of about +/-0.005 nats
on an event-level bootstrap, and the differences worth deciding -- a blend
weight, a K -- are a few thousandths. A paired test of K=128 against K=32 came
out at 90.5% of draws with the interval still straddling zero.

More evaluation data is the only way through, and it cannot come from making the
holdout longer, because the training set shrinks as it grows. It comes from
scoring every event in turn: train on everything before a block of cards,
predict that block, move on, refit. Training sets overlap between steps, but no
event is ever scored twice, and that is the condition an event-level bootstrap
needs. Covering 2016 onward is roughly 400 events, which about halves the
interval.

Two rules keep it honest, and both are easy to get wrong:

* Hyperparameters are chosen inside each training window, on its most recent
  slice, and the block being predicted never takes part in choosing anything.
  The validation score is optimistic, as any score used for selection is, and it
  is never reported -- only the block scores are, and selection never touches
  those.

* After selecting, the model is refitted on the *whole* window, validation slice
  included. A model that predicts the next block without having seen the fights
  immediately before it is not the model the bot deploys, and scoring one would
  understate what the bot actually does.

The validation slice is the most recent part of the window rather than a random
sample of it. That adjacency is the point: it is the same relationship the
deployed model has to the next card. A random slice would mix in older eras and
choose settings suited to fights unlike the ones coming -- which is not
hypothetical here, since the winner model's boosted half was shown to prefer
recent fights while its logistic half improved on everything.

Elo is precomputed once per candidate K rather than rebuilt inside each window.
A fighter's rating at a fight depends only on earlier fights, so one full pass
per candidate is leakage-free under any later split by date, and K becomes an
ordinary column to select over rather than a rebuild per step.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, timedelta

import numpy as np

log = logging.getLogger(__name__)

# How many events are predicted per step. The bot refits on every refresh, so
# one would be truest to it; ten is where the walk stops costing more than the
# precision it buys, and it moves the origin about every three weeks of fights.
BLOCK_EVENTS = 10

# How much of a training window's tail chooses its hyperparameters. A year,
# because a slice of a few cards ranks settings mostly by luck -- the whole
# reason this module exists is that 117 events could not separate the candidates
# being chosen between here.
VALIDATION_DAYS = 365

# Enough window to fit on at all before a block is worth scoring.
MIN_TRAIN_FIGHTS = 1000


@dataclass(frozen=True, slots=True)
class Candidate:
    """One setting to choose between. ``k`` names which design matrix to read."""

    k: float
    c: float

    def __str__(self) -> str:
        return f"K={self.k:g} C={self.c:g}"


@dataclass(frozen=True, slots=True)
class Design:
    """A design matrix built at one Elo K, with the dates to split it by."""

    x: np.ndarray
    y: np.ndarray
    dates: np.ndarray


@dataclass(frozen=True, slots=True)
class Step:
    """One turn of the walk, kept so the selection can be audited afterwards."""

    first_event: date
    train_fights: int
    block_events: int
    chosen: Candidate
    validation_loss: float
    """What the chosen candidate scored on the slice that chose it. Optimistic by
    construction, reported only to show the walk working, never as a result."""


@dataclass
class Rolling:
    """Out-of-sample predictions for every event the walk covered."""

    probabilities: np.ndarray
    labels: np.ndarray
    events: np.ndarray
    """The event each row belongs to, which is the unit the bootstrap resamples.
    Bouts on one card share conditions, so resampling bouts would narrow the
    interval by pretending they are independent."""
    steps: list[Step] = field(default_factory=list)

    @property
    def fights(self) -> int:
        # Each fight is two mirrored rows, the same as everywhere else.
        return len(self.labels) // 2

    def log_loss(self) -> float:
        return _log_loss(self.labels, self.probabilities)

    def accuracy(self) -> float:
        return float(np.mean((self.probabilities >= 0.5) == (self.labels == 1)))

    def chosen_counts(self) -> dict[str, int]:
        """How often each candidate was picked, which says whether K mattered."""
        counts: dict[str, int] = {}
        for step in self.steps:
            counts[str(step.chosen)] = counts.get(str(step.chosen), 0) + 1
        return dict(sorted(counts.items(), key=lambda kv: -kv[1]))


def compare(a: Rolling, b: Rolling, *, draws: int = 4000, seed: int = 0) -> tuple[float, float, float, float]:
    """``a`` minus ``b`` in log loss, bootstrapped by event. Negative favours ``a``.

    Paired: both are scored on the same events, so whatever luck those events
    carry is common to the two and cancels in the differences. Returns the point
    estimate, the interval, and the share of draws favouring ``a``.
    """
    if not np.array_equal(a.events, b.events) or not np.array_equal(a.labels, b.labels):
        raise ValueError("the two walks did not cover the same events")
    rng = np.random.default_rng(seed)
    events = np.unique(a.events)
    index = {event: np.flatnonzero(a.events == event) for event in events}
    gaps = np.empty(draws)
    for i in range(draws):
        drawn = rng.choice(events, size=len(events), replace=True)
        rows = np.concatenate([index[event] for event in drawn])
        gaps[i] = _log_loss(a.labels[rows], a.probabilities[rows]) - _log_loss(
            b.labels[rows], b.probabilities[rows]
        )
    lo, hi = np.percentile(gaps, [2.5, 97.5])
    return a.log_loss() - b.log_loss(), float(lo), float(hi), float((gaps < 0).mean())


def walk(
    designs: dict[float, Design],
    *,
    start: date,
    candidates: list[Candidate],
    block_events: int = BLOCK_EVENTS,
    validation_days: int = VALIDATION_DAYS,
    fit,
) -> Rolling:
    """Walk the origin forward, scoring every event from ``start`` exactly once.

    ``designs`` maps each candidate K to the matrix built at that K; every one
    must cover the same fights in the same order. ``fit`` takes ``(x, y, c)`` and
    returns something with ``predict_proba``, which is the seam the tests use and
    the only place scikit-learn is needed.
    """
    reference = designs[candidates[0].k]
    dates = reference.dates
    for design in designs.values():
        if not np.array_equal(design.dates, dates) or not np.array_equal(design.y, reference.y):
            raise ValueError("the designs disagree about which fights they hold")

    events = np.unique(dates[dates >= start])
    probabilities: list[np.ndarray] = []
    labels: list[np.ndarray] = []
    kept: list[np.ndarray] = []
    steps: list[Step] = []

    for first in range(0, len(events), block_events):
        block = events[first : first + block_events]
        test = np.isin(dates, block)
        window = dates < block[0]
        if window.sum() < MIN_TRAIN_FIGHTS:
            log.info("Skipping the block at %s: only %d rows before it", block[0], window.sum())
            continue

        # The tail of the window chooses; the block never takes part.
        cutoff = block[0] - timedelta(days=validation_days)
        slice_ = window & (dates >= cutoff)
        inner = window & (dates < cutoff)
        if not slice_.any() or inner.sum() < MIN_TRAIN_FIGHTS:
            # Too early in the data to hold a year back and still have a model.
            chosen, validation_loss = candidates[0], float("nan")
        else:
            chosen, validation_loss = _select(designs, candidates, inner, slice_, fit)

        # Refit on the whole window, validation slice included: the bot deploys a
        # model that has seen the fights immediately before the next card.
        design = designs[chosen.k]
        model = fit(design.x[window], design.y[window], chosen.c)
        probabilities.append(model.predict_proba(design.x[test])[:, 1])
        labels.append(design.y[test])
        kept.append(dates[test])
        steps.append(
            Step(
                first_event=block[0],
                train_fights=int(window.sum()) // 2,
                block_events=len(block),
                chosen=chosen,
                validation_loss=validation_loss,
            )
        )

    return Rolling(
        probabilities=np.concatenate(probabilities),
        labels=np.concatenate(labels),
        events=np.concatenate(kept),
        steps=steps,
    )


def _select(
    designs: dict[float, Design],
    candidates: list[Candidate],
    inner: np.ndarray,
    slice_: np.ndarray,
    fit,
) -> tuple[Candidate, float]:
    best, best_loss = candidates[0], float("inf")
    for candidate in candidates:
        design = designs[candidate.k]
        model = fit(design.x[inner], design.y[inner], candidate.c)
        loss = _log_loss(design.y[slice_], model.predict_proba(design.x[slice_])[:, 1])
        if loss < best_loss:
            best, best_loss = candidate, loss
    return best, best_loss


def _log_loss(y: np.ndarray, p: np.ndarray) -> float:
    """Binary log loss, clipped so a confident miss cannot return infinity."""
    p = np.clip(p, 1e-15, 1 - 1e-15)
    return float(-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p)))
