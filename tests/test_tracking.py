"""What happens to the model's picks when a card changes under them.

These are the rules that decide whether a stored pick still describes a real
fight. Getting them wrong either leaves a pick for a fight nobody is having, or
throws away a whole card's picks because one request failed.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from conftest import bout, card, fighter, prediction, unnamed_bout

from ufcbot.features.tracking import PredictionTracker

ALLEN = fighter("1", "Arnold Allen")
PICO = fighter("2", "Aaron Pico")
IGE = fighter("3", "Dan Ige")
VOLK = fighter("4", "Alexander Volkanovski")
EVLOEV = fighter("5", "Movsar Evloev")


def tracker(storage) -> PredictionTracker:
    return PredictionTracker(storage, data=None)


async def bout_ids(track: PredictionTracker) -> set[str]:
    return {record.bout_id for record in await track.records_for("EV1")}


async def record_full_card(track: PredictionTracker, soon) -> None:
    """Allen vs Pico and Volkanovski vs Evloev, both picked."""
    await track.record(
        card(bout("B1", ALLEN, PICO), bout("B2", VOLK, EVLOEV, match=2), start=soon),
        {"B1": prediction(0.62), "B2": prediction(0.45)},
    )


async def test_records_a_pick_for_every_picked_bout(storage, soon):
    track = tracker(storage)
    await record_full_card(track, soon)
    assert await bout_ids(track) == {"B1", "B2"}


async def test_a_replacement_overwrites_the_pick(storage, soon):
    track = tracker(storage)
    await record_full_card(track, soon)

    await track.record(
        card(bout("B1", ALLEN, IGE), bout("B2", VOLK, EVLOEV, match=2), start=soon),
        {"B1": prediction(0.7), "B2": prediction(0.45)},
    )

    names = {r.bout_id: (r.name_a, r.name_b) for r in await track.records_for("EV1")}
    assert names["B1"] == ("Arnold Allen", "Dan Ige")


async def test_a_replacement_the_model_cannot_pick_drops_the_stale_one(storage, soon):
    """A short-notice replacement often has no UFC history, so there is no new
    pick to write. The old one still has to go: it names a fighter who is not in
    the fight."""
    track = tracker(storage)
    await record_full_card(track, soon)

    await track.record(
        card(bout("B1", ALLEN, IGE), bout("B2", VOLK, EVLOEV, match=2), start=soon),
        {"B2": prediction(0.45)},
    )

    assert await bout_ids(track) == {"B2"}


async def test_no_picks_at_all_leaves_the_board_alone(storage, soon):
    """The model can be loading, or switched off. That is not every fight being
    cancelled."""
    track = tracker(storage)
    await record_full_card(track, soon)

    await track.record(card(bout("B1", ALLEN, PICO), bout("B2", VOLK, EVLOEV, match=2), start=soon), {})

    assert await bout_ids(track) == {"B1", "B2"}


async def test_a_card_that_did_not_load_leaves_the_board_alone(storage, soon):
    track = tracker(storage)
    await record_full_card(track, soon)

    await track.record(card(start=soon), {})

    assert await bout_ids(track) == {"B1", "B2"}


async def test_a_half_loaded_card_leaves_the_board_alone(storage, soon):
    track = tracker(storage)
    await record_full_card(track, soon)

    await track.record(card(bout("B2", VOLK, EVLOEV, match=2), start=soon, partial=True), {"B2": prediction()})

    assert await bout_ids(track) == {"B1", "B2"}


async def test_a_bout_whose_names_did_not_load_keeps_its_pick(storage, soon):
    track = tracker(storage)
    await record_full_card(track, soon)

    await track.record(
        card(unnamed_bout("B1"), bout("B2", VOLK, EVLOEV, match=2), start=soon), {"B2": prediction()}
    )

    assert await bout_ids(track) == {"B1", "B2"}


async def test_a_cancelled_bout_is_dropped(storage, soon):
    track = tracker(storage)
    await record_full_card(track, soon)

    await track.record(card(bout("B2", VOLK, EVLOEV, match=2), start=soon), {"B2": prediction(0.45)})

    assert await bout_ids(track) == {"B2"}


async def test_nothing_is_recorded_once_the_card_has_started(storage):
    track = tracker(storage)
    started = datetime.now(UTC) - timedelta(hours=1)
    written = await track.record(card(bout("B1", ALLEN, PICO), start=started), {"B1": prediction()})
    assert written == 0
    assert await bout_ids(track) == set()


async def test_the_line_is_kept_with_the_pick(storage, soon):
    """Not to show on the board, which carries no odds, but so the scorecard can
    measure the model against the fighters the market favoured."""
    track = tracker(storage)
    fight = bout("B1", ALLEN, PICO)
    fight.odds = {ALLEN.id: -160, PICO.id: 135}

    await track.record(card(fight, start=soon), {"B1": prediction()})

    stored = (await track.records_for("EV1"))[0]
    assert (stored.odds_a, stored.odds_b) == (-160, 135)
    assert stored.market_favourite_athlete == ALLEN.id


# -- the method pick's blind spot ---------------------------------------------


def graded(bout_id: str, *, method_correct: bool, result_method: str):
    """One graded prediction, with how the fight actually ended."""
    from ufcbot.records import PredictionRecord

    return PredictionRecord(
        espn_event_id="E1", bout_id=bout_id, event_name="UFC 333",
        event_start=datetime(2026, 5, 1, tzinfo=UTC),
        athlete_a="A", name_a="Fighter A", athlete_b="B", name_b="Fighter B",
        prob_a=0.7, weight_class="Lightweight", position=0,
        winner_athlete="A", correct=True,
        method="dec_u", method_correct=method_correct, result_method=result_method,
    )


def test_a_split_decision_is_counted_as_a_miss_nobody_could_have_avoided():
    """Graded wrong, because "unanimous" was wrong. Counted separately, because
    the four-way pick cannot name a split: the two decisions come out of one
    stage and a split is a fifth of them, so it is never the likelier one."""
    from ufcbot.features.tracking import GradedEvent, build_scorecard

    event = GradedEvent(
        espn_event_id="E1", name="UFC 333", start=datetime(2026, 5, 1, tzinfo=UTC),
        records=[
            graded("B1", method_correct=True, result_method="dec_u"),
            graded("B2", method_correct=False, result_method="dec_s"),
            graded("B3", method_correct=False, result_method="ko"),
        ],
    )

    card = build_scorecard(None, [event])

    assert card.method_total == 3
    assert card.method_hits == 1
    assert card.method_unnameable == 1, "the split, and not the knockout it missed"
