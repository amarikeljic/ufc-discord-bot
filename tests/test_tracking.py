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


async def test_the_odds_source_is_kept_with_the_line(storage, soon):
    """A card can draw its lines from more than one place, and the board says
    which is which."""
    track = tracker(storage)
    fight = bout("B1", ALLEN, PICO)
    fight.odds = {ALLEN.id: -160, PICO.id: 135}
    fight.odds_provider = "DraftKings"

    await track.record(card(fight, start=soon), {"B1": prediction()})

    stored = (await track.records_for("EV1"))[0]
    assert (stored.odds_a, stored.odds_b, stored.odds_source) == (-160, 135, "DraftKings")
