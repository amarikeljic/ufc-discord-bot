"""Divisions, ratings, the compiled scorer's arithmetic and name matching."""

from __future__ import annotations

from array import array
from datetime import date, timedelta

import pytest

from ufcbot.stats.career import Ledger, division_name, division_weight
from ufcbot.stats.features import FEATURE_NAMES, _matchup_value, matchup_row
from ufcbot.stats.names import NameIndex
from ufcbot.stats.rankings import (
    divisions_with_fighters,
    is_fading,
    pound_for_pound_rank,
    rank_division,
    rating_on,
    standing,
)
from ufcbot.stats.scorer import Blend, Boost, Linear, Tree

TODAY = date(2026, 9, 18)


@pytest.mark.parametrize(
    ("text", "name", "weight"),
    [
        ("Lightweight Bout", "Lightweight", 155.0),
        ("Light Heavyweight Title Bout", "Light Heavyweight", 205.0),
        ("Women's Flyweight Bout", "Women's Flyweight", 125.0),
        ("Heavyweight", "Heavyweight", 265.0),
    ],
)
def test_divisions_are_read_however_they_are_worded(text, name, weight):
    assert division_name(text) == name
    assert division_weight(text) == weight


def test_light_heavyweight_is_not_read_as_heavyweight():
    assert division_name("Light Heavyweight Bout") != "Heavyweight"


@pytest.mark.parametrize("text", ["Catch Weight Bout", "Open Weight Bout", "", None])
def test_a_fight_at_no_division_has_none(text):
    assert division_name(text) is None
    assert division_weight(text) != division_weight(text), "NaN"


# -- ratings ------------------------------------------------------------------


def rated(name: str, elo: float, *, fights: int = 6, division: str | None = "Lightweight", ago: int = 30) -> Ledger:
    ledger = Ledger(name=name)
    ledger.elo = elo
    ledger.fights = fights
    ledger.wins = fights
    ledger.division = division
    ledger.last_fight = TODAY - timedelta(days=ago)
    return ledger


def test_a_win_over_a_better_fighter_moves_the_rating_further():
    underdog, favourite = Ledger(name="under"), Ledger(name="fav")
    underdog.elo = 1400.0
    favourite.elo = 1700.0

    underdog.record_fight(
        on=TODAY, result="win", method_class="dec", title_fight=False, scheduled_rounds=3,
        total_seconds=900.0, own=None, opp=None, opponent_elo=1700.0,
    )
    easy = Ledger(name="easy")
    easy.elo = 1400.0
    easy.record_fight(
        on=TODAY, result="win", method_class="dec", title_fight=False, scheduled_rounds=3,
        total_seconds=900.0, own=None, opp=None, opponent_elo=1200.0,
    )

    assert underdog.elo > easy.elo


def test_a_no_contest_does_not_move_the_rating():
    ledger = Ledger(name="x")
    before = ledger.elo
    ledger.record_fight(
        on=TODAY, result="nc", method_class="other", title_fight=False, scheduled_rounds=3,
        total_seconds=300.0, own=None, opp=None, opponent_elo=1600.0,
    )
    assert ledger.elo == before


def test_the_division_follows_the_most_recent_fight_at_a_limit():
    ledger = Ledger(name="x")
    common = {
        "on": TODAY, "result": "win", "method_class": "dec", "title_fight": False,
        "scheduled_rounds": 3, "total_seconds": 900.0, "own": None, "opp": None,
    }
    ledger.record_fight(weight_class="Lightweight Bout", **common)
    ledger.record_fight(weight_class="Welterweight Bout", **common)
    assert ledger.division == "Welterweight"

    ledger.record_fight(weight_class="Catch Weight Bout", **common)
    assert ledger.division == "Welterweight", "a catchweight says nothing about where they belong"


def test_rankings_are_ordered_by_rating():
    ledgers = {"a": rated("A", 1700), "b": rated("B", 1500), "c": rated("C", 1600)}
    assert [e.name for e in rank_division(ledgers, "Lightweight", on=TODAY)] == ["A", "C", "B"]


def test_the_inactive_and_the_barely_tested_are_not_ranked():
    ledgers = {
        "a": rated("Active", 1700),
        "b": rated("Retired", 1900, ago=1200),
        "c": rated("Newcomer", 1800, fights=2),
    }
    assert [e.name for e in rank_division(ledgers, "Lightweight", on=TODAY)] == ["Active"]


def test_the_womens_divisions_can_be_left_out():
    ledgers = {
        "a": rated("Man", 1600),
        "b": rated("Woman", 1900, division="Women's Flyweight"),
    }
    assert [e.name for e in rank_division(ledgers, None, on=TODAY)] == ["Woman", "Man"]
    assert [e.name for e in rank_division(ledgers, None, on=TODAY, include_women=False)] == ["Man"]
    assert divisions_with_fighters(ledgers, on=TODAY, include_women=False) == ["Lightweight"]


# -- the compiled scorer -------------------------------------------------------


def stump(threshold: float, left_value: float, right_value: float, *, feature: int = 0) -> Tree:
    """One split: left when the feature is at or below the threshold."""
    return Tree(
        feature=array("i", [feature, 0, 0]),
        threshold=array("d", [threshold, 0.0, 0.0]),
        left=array("I", [1, 0, 0]),
        right=array("I", [2, 0, 0]),
        value=array("d", [0.0, left_value, right_value]),
        leaf=bytes([0, 1, 1]),
        missing_left=bytes([1, 0, 0]),
    )


def test_a_tree_walks_to_the_leaf_the_value_belongs_to():
    tree = stump(0.5, -1.0, 1.0)
    assert tree.leaf_value([0.0]) == -1.0
    assert tree.leaf_value([0.9]) == 1.0


def test_a_missing_value_takes_the_branch_training_chose():
    tree = stump(0.5, -1.0, 1.0)
    assert tree.leaf_value([float("nan")]) == -1.0


def test_the_blend_averages_the_two_models_the_way_they_were_fitted():
    boost = Boost(baseline=[0.0], stages=[[stump(0.5, -10.0, 10.0)]], classes=[0, 1])
    linear = Linear(
        medians=[0.0], means=[0.0], scales=[1.0], coefficients=[[0.0]], intercepts=[0.0], classes=[0, 1]
    )
    blend = Blend(boost_weight=0.5, boost=boost, linear=linear, width=2)

    # Boosting is all but certain, the linear half is a coin flip, so the blend
    # sits halfway between the two.
    from ufcbot.stats.scorer import _sigmoid

    expected = 0.5 * _sigmoid(10.0) + 0.5 * 0.5
    assert blend.probabilities([1.0])[1] == pytest.approx(expected, abs=1e-9)
    assert 0.74 < blend.probabilities([1.0])[1] < 0.75
    assert sum(blend.probabilities([1.0])) == pytest.approx(1.0)


# -- matchup features ----------------------------------------------------------


def test_a_matchup_value_is_missing_when_either_side_is():
    assert _matchup_value("scaled", float("nan"), 0.5) != _matchup_value("scaled", float("nan"), 0.5)


def test_strikes_expected_falls_as_the_opponents_defence_rises():
    weak = _matchup_value("scaled", 5.0, 0.4)
    strong = _matchup_value("scaled", 5.0, 0.7)
    assert weak > strong


def test_a_row_has_one_value_per_feature_name():
    empty = Ledger(name="_")
    from ufcbot.stats.features import fighter_features

    side = fighter_features(empty, None, TODAY)
    row = matchup_row(side, side, title_fight=False, scheduled_rounds=3, weight_class="Lightweight")
    assert len(row) == len(FEATURE_NAMES)


# -- name matching -------------------------------------------------------------


def test_a_name_is_found_from_its_start_or_its_surname():
    index = NameIndex(["Alexander Volkanovski", "Jon Jones", "Alexa Grasso"])
    assert index.suggest("volk") == ["Alexander Volkanovski"]
    assert index.suggest("jones") == ["Jon Jones"]


def test_a_misspelling_still_finds_the_fighter():
    index = NameIndex(["Islam Makhachev", "Jon Jones"])
    assert index.suggest("makchacev") == ["Islam Makhachev"]


def test_nothing_is_suggested_for_nonsense():
    assert NameIndex(["Jon Jones"]).suggest("xyzq") == []


# -- where a fighter stands ----------------------------------------------------


def test_a_fighters_place_in_their_division_and_overall():
    ledgers = {
        "champ": rated("Champ", 1300),
        "second": rated("Second", 1250),
        "small": rated("Small", 1280, division="Flyweight"),
    }

    place = standing(ledgers, "second", on=TODAY)
    assert (place.rank, place.division) == (2, "Lightweight")
    # Across every division, the flyweight sits between the two lightweights.
    assert pound_for_pound_rank(ledgers, "second", on=TODAY).rank == 3
    assert pound_for_pound_rank(ledgers, "small", on=TODAY).rank == 2


def test_someone_ranked_deeper_than_a_board_prints_still_gets_a_number():
    """The boards stop at fifteen; a profile should not say nothing about the
    sixteenth-best fighter in a division."""
    ledgers = {f"f{i}": rated(f"Fighter {i}", 1400 - 20 * i) for i in range(40)}

    place = standing(ledgers, "f30", on=TODAY)
    assert (place.rank, place.division) == (31, "Lightweight")
    assert pound_for_pound_rank(ledgers, "f30", on=TODAY).rank == 31


def test_an_unranked_fighter_has_no_place():
    ledgers = {
        "retired": rated("Retired", 1400, ago=1200),
        "rookie": rated("Rookie", 1300, fights=1),
        "nodivision": rated("Catchweight Only", 1350, division=None),
    }
    for key in ledgers:
        assert standing(ledgers, key, on=TODAY) is None, key
    assert pound_for_pound_rank(ledgers, "retired", on=TODAY) is None
    assert pound_for_pound_rank(ledgers, "rookie", on=TODAY) is None


def test_a_fighter_nobody_has_heard_of_has_no_place():
    assert standing({}, "who", on=TODAY) is None
    assert pound_for_pound_rank({}, "who", on=TODAY) is None


# -- a rating fades while a fighter is not defending it --------------------------


def test_a_rating_holds_through_an_ordinary_gap_between_fights():
    """Fighters go most of a year between bouts all the time; that is not a layoff."""
    for days in (30, 200, 364):
        assert rating_on(rated("X", 1300, ago=days), TODAY) == 1300


def test_a_long_layoff_fades_the_margin_the_fighter_built():
    year_out = rating_on(rated("X", 1300, ago=365 + 365), TODAY)
    half_out = rating_on(rated("X", 1300, ago=365 + 182), TODAY)

    # A year past the grace period halves what they hold over the start.
    assert year_out == 1150
    assert 1150 < half_out < 1300
    assert is_fading(rated("X", 1300, ago=400), TODAY)
    assert not is_fading(rated("X", 1300, ago=300), TODAY)


def test_fading_never_drags_a_fighter_below_where_they_started():
    """Only the margin fades, so sitting still cannot make someone worse than a debutant."""
    assert rating_on(rated("X", 900, ago=365 + 365), TODAY) == 950
    assert rating_on(rated("X", 1000, ago=365 + 365), TODAY) == 1000


def test_a_retired_fighter_stops_outranking_the_division_he_left():
    """The case this was built for: a great fighter long gone, still sitting above
    the man who actually holds the division now.

    Two rules share the work. Fading closes the gap while he is away; the cut
    takes him off once he has been away too long to count as a fighter at all.
    """
    champ = rated("Current Champ", 1170, ago=99)

    def board(ago: int) -> list[str]:
        ledgers = {"gone": rated("Retired Great", 1296, ago=ago), "champ": champ}
        return [entry.name for entry in rank_division(ledgers, "Lightweight", on=TODAY)]

    assert board(100) == ["Retired Great", "Current Champ"], "recently active: he is simply better"
    assert rating_on(rated("Retired Great", 1296, ago=500), TODAY) < 1296, "away a year and more: fading"
    assert board(600) == ["Current Champ"], "away too long to rank at all"


# -- ratings too close to separate share a rank ----------------------------------


def test_fighters_within_a_handful_of_points_share_a_rank():
    ledgers = {
        "a": rated("Clear", 1250),
        "b": rated("Close", 1188),
        "c": rated("Closer", 1186),
    }
    board = rank_division(ledgers, "Lightweight", on=TODAY)

    assert [entry.rank for entry in board] == [1, 2, 2]
    assert [entry.tied for entry in board] == [False, True, True]


def test_a_tie_does_not_chain_across_the_whole_board():
    """Each is within TIE_GAP of the one above, but the ends are far apart, so
    they are not all one rank."""
    ledgers = {str(i): rated(f"F{i}", 1200 - 4 * i) for i in range(6)}
    ranks = [entry.rank for entry in rank_division(ledgers, "Lightweight", on=TODAY)]

    assert ranks == [1, 1, 3, 3, 5, 5]


def test_a_board_comes_back_the_same_way_twice():
    """The change watcher compares one board against the last, so an arbitrary
    order among equals would announce moves that never happened."""
    ledgers = {"a": rated("Zoe", 1200), "b": rated("Adam", 1200)}
    once = [entry.name for entry in rank_division(ledgers, "Lightweight", on=TODAY)]
    again = [entry.name for entry in rank_division(dict(reversed(ledgers.items())), "Lightweight", on=TODAY)]

    assert once == again == ["Adam", "Zoe"]
