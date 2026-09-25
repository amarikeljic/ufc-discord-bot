"""That every embed builds, stays inside Discord's limits, and says the right thing.

Discord rejects an embed over 6000 characters or with more than 25 fields, and a
board that fails to build stops updating without saying so.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from conftest import bout, card, fighter, pickem_record, prediction

from ufcbot.embeds import (
    card_changes_embed,
    live_open_embed,
    live_result_embed,
    live_round_embed,
    pickem_board_embed,
    pickem_picker_embed,
    pickem_picks_embed,
    picks_board_embed,
    predictions_embed,
    rankings_embed,
    scheduled_event_description,
    scheduled_event_location,
)
from ufcbot.embeds.common import EMBED_BUDGET
from ufcbot.features.cardwatch import CardChange
from ufcbot.records import PickemRecord, PredictionRecord
from ufcbot.stats.rankings import Ranked

ALLEN = fighter("1", "Arnold Allen")
PICO = fighter("2", "Aaron Pico")
VOLK = fighter("3", "Alexander Volkanovski")
EVLOEV = fighter("4", "Movsar Evloev")
START = datetime.now(UTC) + timedelta(days=5)


def record(bout_id: str, a, b) -> PredictionRecord:
    """A pick with a line attached, which the board is expected not to show."""
    return PredictionRecord(
        espn_event_id="EV", bout_id=bout_id, event_name="UFC 333", event_start=START,
        athlete_a=a.id, name_a=a.display_name, athlete_b=b.id, name_b=b.display_name,
        prob_a=0.62, weight_class="Featherweight", position=0, odds_a=-160, odds_b=135,
        method="dec_u", method_prob=0.34,
        detail_json=json.dumps(prediction().to_dict()),
    )


def within_limits(embed) -> bool:
    return len(embed) <= EMBED_BUDGET and len(embed.fields) <= 25


def test_the_picks_board_carries_no_betting_line():
    """The picks channel is the model's opinion. Odds belong in pick'em, where
    they are what you are playing for."""
    embed = picks_board_embed("UFC 333", START, [record("B1", ALLEN, PICO)], locked=False)

    value = embed.fields[0].value.replace(" ", " ")
    assert "Odds" not in embed.description
    assert "-160" not in value and "Polymarket" not in value
    assert "Arnold Allen" in value, "the pick itself is still there"


def test_a_single_pick_is_not_called_picks():
    embed = picks_board_embed("UFC 333", START, [record("B1", ALLEN, PICO)], locked=False)
    assert "1 pick" in embed.description and "1 picks" not in embed.description


def test_a_full_card_of_picks_fits_in_an_embed():
    records = [record(f"B{i}", ALLEN, PICO) for i in range(13)]
    assert within_limits(picks_board_embed("UFC 333", START, records, locked=False))


def test_the_predictions_command_carries_no_betting_line():
    fight = bout("B1", ALLEN, PICO)
    fight.odds, fight.odds_provider = {"1": -160, "2": 135}, "Polymarket"
    embed = predictions_embed(card(fight, start=START), {"B1": prediction()})

    assert "Odds" not in embed.description
    assert "-160" not in embed.fields[0].value


def test_the_pickem_board_shows_the_lines_without_naming_the_source():
    """Every price comes from the same place, so saying so on every board is a
    line of text that never varies."""
    fight = bout("B1", ALLEN, PICO)
    fight.odds, fight.odds_provider = {"1": -160, "2": 135}, "Polymarket"

    embed = pickem_board_embed(card(fight, start=START), {}, 4, now=datetime.now(UTC))

    assert "Polymarket" not in embed.description
    assert "Odds from" not in embed.description
    assert "-160" in embed.fields[0].value, "the price itself is still shown"
    assert within_limits(embed)


def test_the_picker_counts_only_fights_still_on_the_card():
    """A pick on a fight that has come off is not a pick out of twelve, and its
    points are not there to be won."""
    event = card(bout("B1", ALLEN, PICO), bout("B2", VOLK, EVLOEV, match=2), start=START)
    picks = {
        bout_id: PickemRecord(
            guild_id=1, user_id=1, espn_event_id="EV", bout_id=bout_id, event_name="UFC 333",
            event_start=START, athlete_id="1", athlete_name="x", opponent_id="2", opponent_name="y",
            odds=-200, points_if_right=50, locks_at=START, picked_at=datetime.now(UTC),
            result="void" if bout_id == "GONE" else None,
            graded_at=datetime.now(UTC) if bout_id == "GONE" else None,
        )
        for bout_id in ("B1", "B2", "GONE")
    }

    embed = pickem_picker_embed(event, picks, page=0, pages=1, now=datetime.now(UTC))

    assert "Picked **2** of 2" in embed.description
    assert "100" in embed.description, "only the two live picks are worth points"


def test_the_predictions_embed_builds_for_a_full_card():
    bouts = [bout(f"B{i}", ALLEN, PICO, match=i) for i in range(1, 14)]
    event = card(*bouts, start=START)
    picks = {b.id: prediction() for b in bouts}
    assert within_limits(predictions_embed(event, picks))


def test_a_scheduled_event_description_is_only_the_card():
    event = card(bout("B1", ALLEN, PICO), start=START)
    event.broadcast = "Paramount+"
    event.main_card_start = START + timedelta(hours=3)

    text = scheduled_event_description(event, {})

    assert "Live on" not in text and "Prelims first" not in text
    assert "Arnold Allen vs. Aaron Pico" in text


def test_card_changes_read_as_what_happened():
    event = card(bout("B1", ALLEN, PICO), start=START)
    changes = [
        CardChange("replaced", "Allen vs Ige", "Featherweight", left="Aaron Pico", arrived="Dan Ige", opponent="Arnold Allen"),
        CardChange("removed", "Renato Moicano vs. Brian Ortega", "Lightweight"),
    ]
    # Embeds hold names together with non-breaking spaces so a narrow screen
    # wraps between facts, never inside one.
    value = card_changes_embed(event, changes).fields[0].value.replace(" ", " ")
    assert "Dan Ige** replaces Aaron Pico" in value
    assert "Off the card — Renato Moicano vs. Brian Ortega" in value


def test_a_ratings_board_builds():
    entries = [
        Ranked(rank=i, name=f"Fighter {i}", rating=1700 - i, record="12-2-0", division="Lightweight")
        for i in range(1, 16)
    ]
    embed = rankings_embed("Lightweight", entries)
    assert within_limits(embed) and "1699" in embed.fields[0].value


def test_every_live_post_names_the_card():
    fight = bout("B1", ALLEN, PICO)
    stats = {i: dict.fromkeys(("sig_l", "sig_a", "tot_l", "tot_a", "kd", "td_l", "td_a", "sub", "ctrl", "head", "body", "leg"), 10.0) for i in ("1", "2")}
    name = "UFC 331: Van vs. Pantoja 2"

    for embed in (
        live_open_embed(event_name=name, bout=fight, record=None, odds={}, careers={}),
        live_round_embed(event_name=name, bout=fight, round_number=2, stats=stats, edge=1),
        live_result_embed(event_name=name, bout=fight, winner=ALLEN, method="ko", technique="punches",
                          round_number=2, clock="3:41", totals=stats, scorecards={}, record=None, odds={}),
    ):
        assert embed.footer.text == name
        assert embed.timestamp is not None


def test_a_scheduled_event_is_located_by_city_not_arena():
    event = card(bout("B1", ALLEN, PICO), start=START)
    event.venue_name, event.venue_city, event.venue_country = "T-Mobile Arena", "Las Vegas", "USA"

    assert scheduled_event_location(event) == "Las Vegas, USA"


def test_the_fighter_card_shows_the_rating_and_where_it_places():
    from ufcbot.embeds import fighter_embed
    from ufcbot.stats.career import Ledger
    from ufcbot.stats.service import FighterCareer

    ledger = Ledger(name="Islam Makhachev")
    ledger.fights, ledger.wins, ledger.elo = 18, 17, 1273.4

    place = Ranked(rank=1, name=ledger.name, rating=1273, record="17-1-0", division="Welterweight")
    fields = {f.name: f.value for f in fighter_embed(None, FighterCareer(ledger, None), standing=place).fields}

    assert "1273" in fields["Bot Rating"]
    assert "1st at Welterweight" in fields["Bot Rating"].replace("\xa0", " ")


def test_the_fighter_card_calls_a_shared_rank_joint():
    from ufcbot.embeds import fighter_embed
    from ufcbot.stats.career import Ledger
    from ufcbot.stats.service import FighterCareer

    ledger = Ledger(name="Kamaru Usman")
    ledger.fights, ledger.wins, ledger.elo = 20, 16, 1171.0
    place = Ranked(rank=1, name=ledger.name, rating=1171, record="16-4-0", division="Middleweight", tied=True)

    fields = {f.name: f.value for f in fighter_embed(None, FighterCareer(ledger, None), standing=place).fields}

    assert "joint 1st at Middleweight" in fields["Bot Rating"].replace("\xa0", " ")


def test_the_board_marks_a_shared_rank_and_does_not_hand_out_two_of_a_medal():
    entries = [
        Ranked(rank=1, name="A", rating=1200, record="10-0-0", division="Lightweight"),
        Ranked(rank=2, name="B", rating=1171, record="9-1-0", division="Lightweight", tied=True),
        Ranked(rank=2, name="C", rating=1171, record="9-1-0", division="Lightweight", tied=True),
    ]
    text = rankings_embed("Lightweight", entries).fields[0].value

    assert "\U0001f947" in text, "the outright leader keeps the gold"
    assert text.count("\U0001f948") == 0, "a shared second is not a silver medal"
    assert text.count("=") == 2


def test_the_model_status_card_reads_without_repeating_itself():
    from datetime import date as _date

    from ufcbot.embeds import model_status_embed
    from ufcbot.stats.prediction import Evaluation
    from ufcbot.stats.scorer import Blend, Boost, CompiledModel, Linear

    empty = Blend(
        0.35,
        Boost([0.0], [], [0, 1]),
        Linear([0.0], [0.0], [1.0], [[0.0]], [0.0], [0, 1]),
        2,
    )
    model = CompiledModel(
        winner=empty, feature_names=[], trained_at=datetime(2026, 9, 18, 4, 33),
        training_fights=8754, dataset_newest=_date(2026, 9, 12),
        evaluation=Evaluation(
            holdout_from=_date(2025, 3, 1), fights=796, accuracy=0.659, log_loss=0.632,
            baseline_accuracy=0.590, method_accuracy=0.519, method_baseline=0.399,
            exact_accuracy=0.349, technique_accuracy=0.741, technique_baseline=0.736,
        ),
        importances=[("d_elo", 0.01), ("d_td_def", 0.005)],
    )

    embed = model_status_embed(
        fight_count=8911, newest_event=_date(2026, 9, 12), behind=_date(2026, 9, 15),
        model=model, last_check=datetime(2026, 9, 19, 4, 32), last_error=None,
    )
    fields = {f.name: f.value for f in embed.fields}

    assert "8,911" in embed.description and "Behind" in embed.description
    assert "65.9%" in fields["Picks the winner"] and "59.0%" in fields["Picks the winner"]
    assert "accuracy accuracy" not in embed.description, "the old wording said it twice"
    # Column names are for the model; a reader gets the English.
    decides = fields["What decides a fight"].replace(" ", " ")
    assert "takedown defence" in decides and "d_td_def" not in decides
    assert "8,754" in embed.footer.text
    assert within_limits(embed)


def test_the_status_card_says_so_when_there_is_no_model():
    from ufcbot.embeds import model_status_embed

    embed = model_status_embed(
        fight_count=0, newest_event=None, behind=None, model=None, last_check=None, last_error=None
    )
    assert "No fight data yet" in embed.description
    assert any("Not trained yet" in f.value for f in embed.fields)


# -- everyone's picks for a card -------------------------------------------------


def graded(record, result: str, points: int):
    record.result, record.points = result, points
    record.graded_at = datetime.now(UTC)
    return record


def test_a_cards_picks_are_grouped_by_fight_with_who_backed_whom(soon):
    picks = [
        pickem_record(user_id=11, bout_id="B1", athlete_id="A", opponent_id="B", locks_at=soon),
        pickem_record(user_id=22, bout_id="B1", athlete_id="A", opponent_id="B", locks_at=soon),
        pickem_record(user_id=33, bout_id="B1", athlete_id="B", opponent_id="A", locks_at=soon),
    ]
    embed = pickem_picks_embed("UFC 331", picks)

    assert "3 players" in embed.description and "3 picks" in embed.description
    assert len(embed.fields) == 1, "one fight, one field"
    value = embed.fields[0].value
    assert "<@11>" in value and "<@22>" in value and "<@33>" in value
    # Both corners are listed, and the heading reads the same way either way.
    assert embed.fields[0].name == "Fighter A vs. Fighter B"


def test_picks_still_to_lock_are_counted_but_not_shown():
    embed = pickem_picks_embed("UFC 331", [], hidden=7)

    assert not embed.fields
    assert "lock" in embed.description


def test_a_card_nobody_played_says_so():
    assert "Nobody picked" in pickem_picks_embed("UFC 331", []).description


def test_the_card_scores_only_appear_once_something_has_been_graded(soon):
    pending = [pickem_record(user_id=11, bout_id="B1", athlete_id="A", opponent_id="B", locks_at=soon)]
    assert not any(field.name == "Card scores" for field in pickem_picks_embed("UFC 331", pending).fields)

    settled = [
        graded(pickem_record(user_id=11, bout_id="B1", athlete_id="A", opponent_id="B", locks_at=soon), "win", 44),
        graded(pickem_record(user_id=22, bout_id="B1", athlete_id="B", opponent_id="A", locks_at=soon), "loss", -100),
    ]
    scores = next(field for field in pickem_picks_embed("UFC 331", settled).fields if field.name == "Card scores")
    # The winner leads, and both totals carry their sign.
    assert scores.value.index("<@11>") < scores.value.index("<@22>")
    assert "+44" in scores.value and "-100" in scores.value


def test_the_picks_board_names_the_fights_it_cannot_call():
    """A debut has no UFC history to predict from. Dropping the fight silently
    leaves a board missing a bout with no hint it was ever on the card."""
    embed = picks_board_embed(
        "UFC 333", START, [record("B1", ALLEN, PICO)], locked=False,
        unpicked=[(1, "Mehemmedeli Osmanli vs. Ilimbek Akylbek Uulu")],
    )
    text = embed.description + " ".join(f"{f.name} {f.value}" for f in embed.fields)

    assert "Picks for 1 of 2 fights" in text
    assert "Mehemmedeli Osmanli vs. Ilimbek Akylbek Uulu" in text
    assert "UFC debut" in text
    names = [f.name for f in embed.fields]
    assert names.index("Mehemmedeli Osmanli vs. Ilimbek Akylbek Uulu") == 1, "in its place on the card"


def test_a_fully_picked_card_says_nothing_about_missing_fights():
    embed = picks_board_embed("UFC 333", START, [record("B1", ALLEN, PICO)], locked=False)
    text = embed.description + " ".join(f.value for f in embed.fields)

    assert "No pick" not in text and "of 1 fights" not in text
