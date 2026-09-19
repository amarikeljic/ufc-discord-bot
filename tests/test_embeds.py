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


def record(bout_id: str, a, b, source: str | None) -> PredictionRecord:
    return PredictionRecord(
        espn_event_id="EV", bout_id=bout_id, event_name="UFC 333", event_start=START,
        athlete_a=a.id, name_a=a.display_name, athlete_b=b.id, name_b=b.display_name,
        prob_a=0.62, weight_class="Featherweight", position=0, odds_a=-160, odds_b=135,
        odds_source=source, method="dec_u", method_prob=0.34,
        detail_json=json.dumps(prediction().to_dict()),
    )


def within_limits(embed) -> bool:
    return len(embed) <= EMBED_BUDGET and len(embed.fields) <= 25


def test_one_odds_source_is_named_once_at_the_top():
    embed = picks_board_embed("UFC 333", START, [record("B1", ALLEN, PICO, "DraftKings")], locked=False)
    assert "Odds from DraftKings" in embed.description
    assert "DraftKings" not in embed.fields[0].value, "not repeated per fight"


def test_mixed_odds_sources_are_named_per_fight():
    """A card drawing on two places cannot say so once: you could not tell which
    fight came from where."""
    records = [record("B1", ALLEN, PICO, "DraftKings"), record("B2", VOLK, EVLOEV, "Polymarket")]
    embed = picks_board_embed("UFC 333", START, records, locked=False)

    assert "marked per fight" in embed.description
    assert "DraftKings" in embed.fields[0].value
    assert "Polymarket" in embed.fields[1].value


def test_a_single_pick_is_not_called_picks():
    embed = picks_board_embed("UFC 333", START, [record("B1", ALLEN, PICO, None)], locked=False)
    assert "1 pick" in embed.description and "1 picks" not in embed.description


def test_a_full_card_of_picks_fits_in_an_embed():
    records = [record(f"B{i}", ALLEN, PICO, "DraftKings") for i in range(13)]
    assert within_limits(picks_board_embed("UFC 333", START, records, locked=False))


def test_the_pickem_board_marks_each_fights_source_when_they_differ():
    first, second = bout("B1", ALLEN, PICO), bout("B2", VOLK, EVLOEV, match=2)
    first.odds, first.odds_provider = {"1": -160, "2": 135}, "DraftKings"
    second.odds, second.odds_provider = {"3": -120, "4": 100}, "Polymarket"
    event = card(first, second, start=START)

    embed = pickem_board_embed(event, {}, 4, now=datetime.now(UTC))

    assert "marked per fight" in embed.description
    assert "DraftKings" in embed.fields[0].value
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
