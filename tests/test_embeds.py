"""That every embed builds, stays inside Discord's limits, and says the right thing.

Discord rejects an embed over 6000 characters or with more than 25 fields, and a
board that fails to build stops updating without saying so.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from conftest import bout, card, fighter, prediction

from ufcbot.embeds import (
    card_changes_embed,
    live_knockdown_text,
    live_open_embed,
    live_pause_text,
    live_result_embed,
    live_round_embed,
    pickem_board_embed,
    pickem_picker_embed,
    picks_board_embed,
    predictions_embed,
    rankings_embed,
    scheduled_event_description,
    scheduled_event_location,
)
from ufcbot.embeds.common import EMBED_BUDGET
from ufcbot.features.cardwatch import CardChange
from ufcbot.stats.rankings import Ranked
from ufcbot.storage import PickemRecord, PredictionRecord

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
    assert "-160" not in value and "DraftKings" not in value
    assert "Arnold Allen" in value, "the pick itself is still there"


def test_a_single_pick_is_not_called_picks():
    embed = picks_board_embed("UFC 333", START, [record("B1", ALLEN, PICO)], locked=False)
    assert "1 pick" in embed.description and "1 picks" not in embed.description


def test_a_full_card_of_picks_fits_in_an_embed():
    records = [record(f"B{i}", ALLEN, PICO) for i in range(13)]
    assert within_limits(picks_board_embed("UFC 333", START, records, locked=False))


def test_the_predictions_command_carries_no_betting_line():
    fight = bout("B1", ALLEN, PICO)
    fight.odds, fight.odds_provider = {"1": -160, "2": 135}, "DraftKings"
    embed = predictions_embed(card(fight, start=START), {"B1": prediction()})

    assert "Odds" not in embed.description
    assert "-160" not in embed.fields[0].value


def test_the_pickem_board_shows_the_lines_without_naming_the_book():
    """Pick'em takes the sportsbook's price only, so there is nothing to say
    about where it came from."""
    fight = bout("B1", ALLEN, PICO)
    fight.odds, fight.odds_provider = {"1": -160, "2": 135}, "DraftKings"

    embed = pickem_board_embed(card(fight, start=START), {}, 4, now=datetime.now(UTC))

    assert "DraftKings" not in embed.description
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

    # The two plain messages have no footer to put it in, so it goes on the end.
    assert "UFC 331" in live_knockdown_text(fight, 2, "3:41", ALLEN, "UFC 331")
    assert "UFC 331" in live_pause_text(fight, 2, "2:12", "UFC 331")


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

    fields = {f.name: f.value for f in fighter_embed(None, FighterCareer(ledger, None), standing=(1, "Welterweight")).fields}

    assert "1273" in fields["Bot Rating"]
    assert "1st at Welterweight" in fields["Bot Rating"].replace("\xa0", " ")


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
