"""Pick'em scoring, and what a changed card does to picks people have made.

Members' points ride on this, so the rule that matters most is the one about a
fight that never happened scoring nothing rather than counting as a loss.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from conftest import bout, card, fighter, pickem_record, unnamed_bout

from ufcbot.features.pickem import (
    WRONG_PICK_POINTS,
    PickemService,
    benchmarks,
    fully_priced,
    points_for,
)
from ufcbot.storage import PredictionRecord

MOICANO = fighter("20", "Renato Moicano")
ORTEGA = fighter("21", "Brian Ortega")
TSARUKYAN = fighter("22", "Arman Tsarukyan")
RUFFY = fighter("23", "Mauricio Ruffy")
REPLACEMENT = fighter("24", "Dan Hooker")


@pytest.mark.parametrize(
    ("odds", "expected"),
    [(235, 235), (110, 110), (-110, 91), (-290, 34), (-1800, 6), (100, 100), (-100, 100)],
)
def test_points_follow_the_odds(odds, expected):
    assert points_for(odds) == expected


def test_a_price_of_zero_does_not_divide_by_zero():
    """No book prices a fight at zero, but a bad feed can say so, and this runs
    over every stored pick at startup."""
    assert points_for(0) == 100


async def picks_for(storage, users, bout_id="M1", locks_at=None):
    for user, athlete, opponent in users:
        await storage.save_pickem_pick(
            pickem_record(
                user_id=user,
                bout_id=bout_id,
                athlete_id=athlete,
                opponent_id=opponent,
                locks_at=locks_at or datetime.now(UTC) - timedelta(hours=3),
            )
        )


async def results_by_user(storage, users):
    out = {}
    for user in users:
        summary = await storage.pickem_user_summary(1, user)
        out[user] = (summary.wins, summary.losses, summary.voids, summary.points)
    return out


async def test_a_pick_on_the_winner_scores(storage):
    await picks_for(storage, [(10, "22", "23")])
    await storage.grade_pickem_bout("M1", "22", loss_points=WRONG_PICK_POINTS, fighters={"22", "23"})
    assert (await results_by_user(storage, [10]))[10] == (1, 0, 0, 44)


async def test_a_pick_on_the_loser_costs(storage):
    await picks_for(storage, [(10, "22", "23")])
    await storage.grade_pickem_bout("M1", "23", loss_points=WRONG_PICK_POINTS, fighters={"22", "23"})
    assert (await results_by_user(storage, [10]))[10] == (0, 1, 0, WRONG_PICK_POINTS)


async def test_a_pick_on_a_replaced_fighter_is_void_not_a_loss(storage):
    """Moicano is picked, withdraws, and his opponent beats the replacement. The
    fight that was picked never happened."""
    await picks_for(storage, [(10, "20", "21"), (11, "21", "20")])

    await storage.grade_pickem_bout("M1", "21", loss_points=WRONG_PICK_POINTS, fighters={"21", "24"})

    scored = await results_by_user(storage, [10, 11])
    assert scored[10] == (0, 0, 1, 0), "the pick on the withdrawn fighter"
    assert scored[11] == (1, 0, 0, 44), "the pick on the fighter who fought and won"


async def test_a_bout_with_no_winner_is_void(storage):
    await picks_for(storage, [(10, "22", "23")])
    await storage.grade_pickem_bout("M1", None, loss_points=WRONG_PICK_POINTS)
    assert (await results_by_user(storage, [10]))[10] == (0, 0, 1, 0)


# -- fights that leave the card before they lock ------------------------------


def service(storage) -> PickemService:
    return PickemService(data=None, storage=storage)


async def test_a_fight_off_the_card_is_voided_before_it_locks(storage, soon):
    """Grading only ever looks at bouts that have locked, so without this a
    cancelled fight sits in everyone's picks until days after the card."""
    await picks_for(storage, [(30, "20", "21")], locks_at=soon)
    await picks_for(storage, [(31, "22", "23")], bout_id="M2", locks_at=soon)

    voided = await service(storage).retire_missing(
        card(bout("M2", TSARUKYAN, RUFFY, match=2), start=soon)
    )

    assert voided == 1
    scored = await results_by_user(storage, [30, 31])
    assert scored[30] == (0, 0, 1, 0), "the cancelled fight"
    assert (await storage.pickem_user_summary(1, 31)).pending == 1, "the other fight is untouched"


async def test_an_intact_card_voids_nothing(storage, soon):
    await picks_for(storage, [(30, "20", "21")], locks_at=soon)
    full = card(bout("M1", MOICANO, ORTEGA), bout("M2", TSARUKYAN, RUFFY, match=2), start=soon)
    assert await service(storage).retire_missing(full) == 0


async def test_a_half_loaded_card_voids_nothing(storage, soon):
    """Voiding cannot be taken back, so it waits for a card that read cleanly."""
    await picks_for(storage, [(30, "20", "21")], locks_at=soon)
    half = card(bout("M2", TSARUKYAN, RUFFY, match=2), start=soon, partial=True)
    assert await service(storage).retire_missing(half) == 0


async def test_a_bout_whose_names_did_not_load_voids_nothing(storage, soon):
    await picks_for(storage, [(30, "20", "21")], locks_at=soon)
    unresolved = card(unnamed_bout("M1"), bout("M2", TSARUKYAN, RUFFY, match=2), start=soon)
    assert await service(storage).retire_missing(unresolved) == 0


async def test_only_the_pick_on_the_replaced_fighter_is_voided(storage, soon):
    """The fighter who stayed is still fighting, so that pick stays live and can
    still be changed at the new price."""
    await picks_for(storage, [(30, "20", "21"), (31, "21", "20")], locks_at=soon)

    await service(storage).retire_missing(card(bout("M1", ORTEGA, REPLACEMENT), start=soon))

    assert (await storage.pickem_user_summary(1, 30)).voids == 1
    assert (await storage.pickem_user_summary(1, 31)).pending == 1


async def test_void_picks_are_left_out_of_the_crowd_split(storage, soon):
    """Otherwise the board shows a share of the room behind someone who is not
    in the fight."""
    await picks_for(storage, [(30, "20", "21"), (31, "21", "20")], locks_at=soon)
    await service(storage).retire_missing(card(bout("M1", ORTEGA, REPLACEMENT), start=soon))

    counts = await storage.pickem_counts(1, "EV1")
    assert counts.get("M1") == {"21": 1}


# -- opening the board ---------------------------------------------------------


def test_the_game_opens_only_once_every_fight_is_priced(soon):
    a, b, c, d = (fighter(f"F{i}", f"Fighter {i}") for i in range(4))
    one, two = bout("B1", a, b), bout("B2", c, d)
    event = card(one, two, start=soon)

    assert not fully_priced(event), "no odds at all"

    one.odds = {a.id: -150, b.id: 130}
    assert not fully_priced(event), "half the card priced is half a game"

    two.odds = {c.id: -110}
    assert not fully_priced(event), "one corner priced is not a fight anyone can pick"

    two.odds[d.id] = -110
    assert fully_priced(event)


def test_a_card_with_no_named_fights_is_not_priced(soon):
    assert not fully_priced(card(unnamed_bout("B1"), start=soon))


# -- what the model and the market scored on the same fights ---------------------


def graded_prediction(bout_id: str, *, prob_a: float, odds_a: int, odds_b: int, winner: str) -> PredictionRecord:
    return PredictionRecord(
        espn_event_id="EV1", bout_id=bout_id, event_name="UFC 333",
        event_start=datetime.now(UTC), athlete_a="A", name_a="Fighter A",
        athlete_b="B", name_b="Fighter B", prob_a=prob_a, weight_class="Lightweight",
        position=0, odds_a=odds_a, odds_b=odds_b, winner_athlete=winner,
        correct=(winner == ("A" if prob_a >= 0.5 else "B")),
    )


def test_the_model_and_the_market_are_scored_the_way_a_member_is():
    # The model likes A, the book has B shorter, and B wins.
    records = [graded_prediction("B1", prob_a=0.7, odds_a=200, odds_b=-250, winner="B")]

    model, market = benchmarks(records)

    assert (model.name, model.wins, model.losses) == ("🤖 The bot", 0, 1)
    assert model.points == WRONG_PICK_POINTS
    assert (market.wins, market.losses) == (1, 0)
    assert market.points == points_for(-250), "paid at the price it backed"


def test_a_fight_nobody_could_have_picked_is_not_scored():
    """Pick'em only opens fights with both prices, and a draw is void for
    everyone, so counting either would compare different sets of fights."""
    unpriced = PredictionRecord(
        espn_event_id="EV1", bout_id="B1", event_name="UFC 333", event_start=datetime.now(UTC),
        athlete_a="A", name_a="A", athlete_b="B", name_b="B", prob_a=0.7,
        weight_class="Lightweight", position=0, winner_athlete="A", correct=True,
    )
    drawn = graded_prediction("B2", prob_a=0.7, odds_a=-150, odds_b=130, winner="A")
    drawn.winner_athlete = None

    assert benchmarks([unpriced, drawn]) == []


def test_nothing_is_claimed_before_a_card_is_graded():
    assert benchmarks([]) == []
