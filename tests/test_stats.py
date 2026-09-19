"""Divisions, ratings, the compiled scorer's arithmetic and name matching."""

from __future__ import annotations

from array import array
from datetime import date, timedelta

import pytest

from ufcbot.stats.career import Ledger, division_name, division_weight
from ufcbot.stats.features import FEATURE_NAMES, _matchup_value, matchup_row
from ufcbot.stats.names import NameIndex
from ufcbot.stats.rankings import divisions_with_fighters, rank_division
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
    common = dict(
        on=TODAY, result="win", method_class="dec", title_fight=False,
        scheduled_rounds=3, total_seconds=900.0, own=None, opp=None,
    )
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
