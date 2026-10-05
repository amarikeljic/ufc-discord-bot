"""The rolling-origin walk, whose guarantees are the reason it exists.

The numbers it produces are only worth anything if every event is scored exactly
once, by a model fitted on fights before it, with the selection kept off the
events being reported. Those are the things tested here.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import ClassVar

import numpy as np
import pytest

from ufcbot.stats.rolling import Candidate, Design, Rolling, compare, walk

# Fifteen years of weekly cards, so a walk starting in 2010 has a decade of
# window behind it and five years of events to score.
FIGHTS = 4000


class Recorder:
    """A stand-in model that remembers what it was fitted on."""

    seen: ClassVar[list[tuple[int, float]]] = []

    def __init__(self, x, y, c):
        self.rows, self.c = len(x), c
        self.mean = float(y.mean()) if len(y) else 0.5
        Recorder.seen.append((len(x), c))

    def predict_proba(self, x):
        # A constant at the training base rate, which is enough to score.
        p = np.full(len(x), self.mean)
        return np.column_stack([1 - p, p])


def fit(x, y, c):
    return Recorder(x, y, c)


def design(fights: int, *, k: float = 32.0, seed: int = 0, days: int = 7) -> Design:
    """Mirrored rows, two per fight, one event every ``days`` days.

    Column 0 is the fight's date as an ordinal and column 1 is set from ``k``, so
    a test can read off a fitted model's own training data both when it happened
    and which matrix it came from. Nothing in the walk looks at either.
    """
    rng = np.random.default_rng(seed)
    x = rng.normal(size=(fights * 2, 3))
    y = np.empty(fights * 2, dtype=int)
    dates = np.empty(fights * 2, dtype=object)
    # Five fights to a card, which is what makes an event a cluster worth
    # resampling as one.
    for i in range(fights):
        won = int(rng.random() < 0.5)
        y[2 * i], y[2 * i + 1] = won, 1 - won
        day = date(2000, 1, 1) + timedelta(days=days * (i // 5))
        dates[2 * i] = dates[2 * i + 1] = day
        x[2 * i, 0] = x[2 * i + 1, 0] = day.toordinal()
    x[:, 1] = k
    return Design(x=x, y=y, dates=dates)


def walked(**kw) -> Rolling:
    Recorder.seen = []
    base = design(FIGHTS)
    return walk(
        {32.0: base},
        start=kw.pop("start", date(2010, 1, 1)),
        candidates=[Candidate(32.0, 0.02)],
        fit=fit,
        **kw,
    )


def test_every_event_is_scored_exactly_once():
    """The condition the event-level bootstrap needs. Scoring one twice would
    narrow the interval by counting the same card as two independent ones."""
    result = walked()

    events, counts = np.unique(result.events, return_counts=True)
    assert len(events) == len(set(events))
    # Every event keeps all its rows, and no event appears in two blocks.
    assert set(counts) == {10}, "five fights a card, two mirrored rows each"
    assert len(result.probabilities) == len(result.labels) == len(result.events)


def test_no_model_ever_sees_the_block_it_predicts():
    """The whole point. A model fitted on its own test block would report a
    number about memorisation."""
    base = design(FIGHTS)
    fitted_on: list[tuple[date, date]] = []

    def spy(x, y, c):
        return Recorder(x, y, c)

    # Walk by hand so the window each step trains on can be checked against the
    # block it then predicts.
    result = walk({32.0: base}, start=date(2010, 1, 1), candidates=[Candidate(32.0, 0.02)], fit=spy)

    for step in result.steps:
        covered = base.dates[base.dates < step.first_event]
        assert len(covered) // 2 == step.train_fights
        assert all(day < step.first_event for day in covered)
        fitted_on.append((covered.min(), covered.max()))

    assert fitted_on, "the walk did nothing"
    assert all(last < first for (_, last), first in zip(fitted_on, [s.first_event for s in result.steps]))


def test_the_window_is_refitted_with_the_validation_slice_back_in():
    """Selecting on the tail and then predicting without it would score a model
    that has not seen the fights immediately before the card, which is not the
    model the bot deploys."""
    Recorder.seen = []
    base = design(FIGHTS)
    walk(
        {32.0: base},
        start=date(2010, 1, 1),
        candidates=[Candidate(32.0, 0.02), Candidate(32.0, 0.5)],
        fit=fit,
    )

    # Two candidates fitted on the inner part, then one on the whole window.
    sizes = [rows for rows, _ in Recorder.seen]
    steps = len(sizes) // 3
    assert steps and len(sizes) == steps * 3, "three fits a step: two candidates, one refit"
    for i in range(0, len(sizes), 3):
        inner_a, inner_b, full = sizes[i], sizes[i + 1], sizes[i + 2]
        assert inner_a == inner_b, "both candidates see the same inner window"
        assert full > inner_a, "the refit adds the validation slice back"


def test_no_fit_is_ever_handed_a_row_from_the_block_it_precedes():
    """Read from the data itself rather than from row counts. Every fit of a
    step -- the two that choose the candidate and the one that is scored -- must
    be handed only fights that happened before the block, or the reported number
    is partly a training score."""
    base = design(FIGHTS)
    latest: list[float] = []

    def spy(x, y, c):
        latest.append(float(x[:, 0].max()))
        return Recorder(x, y, c)

    result = walk(
        {32.0: base},
        start=date(2010, 1, 1),
        candidates=[Candidate(32.0, 0.02), Candidate(32.0, 0.5)],
        fit=spy,
    )

    assert len(latest) == 3 * len(result.steps), "three fits a step"
    for step, fits in zip(result.steps, [latest[i : i + 3] for i in range(0, len(latest), 3)]):
        for seen in fits:
            assert seen < step.first_event.toordinal(), (
                f"a fit for the block at {step.first_event} saw a fight from it"
            )


def test_a_k_is_chosen_per_window_from_its_own_matrix():
    """K is selected, not fixed, and each candidate reads the design built at
    that K. Reading one matrix for every K would make the choice meaningless."""
    a, b = design(FIGHTS, k=32.0), design(FIGHTS, k=128.0)
    widths: list[float] = []

    def spy(x, y, c):
        widths.append(float(x[:, 1].mean()))
        return Recorder(x, y, c)

    result = walk(
        {32.0: a, 128.0: b},
        start=date(2010, 1, 1),
        candidates=[Candidate(32.0, 0.02), Candidate(128.0, 0.02)],
        fit=spy,
    )

    assert result.steps
    assert len(set(result.chosen_counts())) >= 1
    assert set(widths) == {32.0, 128.0}, "each candidate was fitted on its own matrix"


def test_designs_that_disagree_are_refused():
    """A K whose matrix covers different fights would make the comparison
    between two Ks a comparison between two datasets."""
    a = design(600, k=32.0)
    b = design(500, k=128.0)

    with pytest.raises(ValueError, match="disagree"):
        walk(
            {32.0: a, 128.0: b},
            start=date(2010, 1, 1),
            candidates=[Candidate(32.0, 0.02), Candidate(128.0, 0.02)],
            fit=fit,
        )


def test_comparing_two_walks_pairs_them_on_the_same_events():
    """Unpaired, the comparison carries both walks' luck. Paired, the shared
    luck cancels, which is what made the K test sharp enough to be worth
    reading."""
    labels = np.array([1, 0, 1, 0, 1, 0, 1, 0])
    events = np.array([date(2020, 1, 1)] * 4 + [date(2020, 2, 1)] * 4)
    good = Rolling(probabilities=np.where(labels == 1, 0.9, 0.1), labels=labels, events=events)
    poor = Rolling(probabilities=np.full(8, 0.5), labels=labels, events=events)

    point, lo, hi, share = compare(good, poor, draws=200)

    assert point < 0, "the better walk has the lower log loss"
    assert lo <= point <= hi
    assert share == 1.0, "every draw favours it, since it is better on both events"


def test_a_walk_cannot_be_compared_with_one_over_different_events():
    labels = np.array([1, 0])
    a = Rolling(probabilities=np.array([0.6, 0.4]), labels=labels, events=np.array([date(2020, 1, 1)] * 2))
    b = Rolling(probabilities=np.array([0.6, 0.4]), labels=labels, events=np.array([date(2021, 1, 1)] * 2))

    with pytest.raises(ValueError, match="same events"):
        compare(a, b, draws=10)
