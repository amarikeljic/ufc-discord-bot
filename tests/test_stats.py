"""Divisions, ratings, the compiled scorer's arithmetic and name matching."""

from __future__ import annotations

from array import array
from datetime import date, timedelta

import pytest

from ufcbot.stats.career import (
    NO_REMATCH,
    Ledger,
    Meeting,
    Rematch,
    division_name,
    division_weight,
    pair_key,
    rematch_between,
)
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


def rated(
    name: str,
    elo: float,
    *,
    fights: int = 6,
    division: str | None = "Lightweight",
    ago: int = 30,
    strength: float | None = None,
    defences: int = 0,
    titles: int = 0,
) -> Ledger:
    ledger = Ledger(name=name)
    ledger.elo = elo
    ledger.strength = elo if strength is None else strength
    ledger.fights = fights
    ledger.wins = fights
    ledger.division = division
    ledger.last_fight = TODAY - timedelta(days=ago)
    ledger.title_defences = defences
    ledger.title_wins = titles
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


# -- what has passed between these two ----------------------------------------------


def test_two_strangers_have_no_history():
    assert rematch_between({}, "a", "b", TODAY) is NO_REMATCH


def test_the_record_reads_the_same_whichever_way_the_pair_is_named():
    """It is stored once per pair, under the sorted names, so the answer cannot
    depend on which corner happens to be listed first."""
    meetings = {pair_key("zoe", "adam"): Meeting(fights=3, first_wins=2, second_wins=1, last_on=date(2025, 1, 1))}

    adam = rematch_between(meetings, "adam", "zoe", TODAY)
    zoe = rematch_between(meetings, "zoe", "adam", TODAY)

    assert (adam.wins, adam.losses) == (2, 1), "adam sorts first and won two"
    assert (zoe.wins, zoe.losses) == (1, 2)
    assert adam.meetings == zoe.meetings == 3
    assert adam.days_since == zoe.days_since


def test_swapping_a_rematch_turns_it_round():
    original = Rematch(meetings=2, wins=2, losses=0, days_since=400.0)
    other = original.swapped()

    assert (other.wins, other.losses) == (0, 2)
    assert other.meetings == 2 and other.days_since == 400.0
    assert other.swapped() == original


def test_how_long_ago_they_met_is_measured_to_the_fight_being_predicted():
    meetings = {pair_key("a", "b"): Meeting(fights=1, first_wins=1, last_on=TODAY - timedelta(days=500))}

    assert rematch_between(meetings, "a", "b", TODAY).days_since == 500.0


def test_a_pair_that_drew_has_met_without_either_winning():
    meetings = {pair_key("a", "b"): Meeting(fights=1, last_on=TODAY)}
    met = rematch_between(meetings, "a", "b", TODAY)

    assert met.meetings == 1 and met.wins == 0 and met.losses == 0


def test_the_rematch_lands_in_the_row_and_mirrors_with_it():
    """A mirrored row swaps the fighters, so it has to swap their history too,
    or the model learns that the first corner tends to have beaten the second."""
    from ufcbot.stats.features import fighter_features

    side = fighter_features(Ledger(name="_"), None, TODAY)
    met = Rematch(meetings=2, wins=2, losses=0, days_since=400.0)
    ctx = {"title_fight": False, "scheduled_rounds": 3, "weight_class": "Lightweight"}

    forward = matchup_row(side, side, **ctx, rematch=met)
    mirrored = matchup_row(side, side, **ctx, rematch=met.swapped())

    assert len(forward) == len(FEATURE_NAMES)
    tail = dict(zip(FEATURE_NAMES[-4:], forward[-4:]))
    assert tail == {"prior_meetings": 2.0, "prior_wins": 2.0, "prior_losses": 0.0, "days_since_meeting": 400.0}
    assert mirrored[-3:-1] == [0.0, 2.0], "wins and losses change hands"


def test_a_first_meeting_is_zeros_rather_than_missing():
    """Most fights are first meetings, so this is the common row: a count of
    nothing, not an absent value the model has to interpret."""
    from ufcbot.stats.features import fighter_features

    side = fighter_features(Ledger(name="_"), None, TODAY)
    row = matchup_row(side, side, title_fight=False, scheduled_rounds=3, weight_class="Lightweight")

    assert row[-4:-1] == [0.0, 0.0, 0.0]
    assert row[-1] != row[-1], "never met, so there is no time since"


# -- what the service holds on to ------------------------------------------------


def a_service(tmp_path):
    from ufcbot.stats.names import NameIndex
    from ufcbot.stats.service import StatsService

    service = StatsService(tmp_path, tmp_path)
    service.names = NameIndex(["Arnold Allen", "Aaron Pico"])
    return service


def test_the_name_cache_cannot_be_grown_without_limit(tmp_path):
    """Every name a command is given is remembered, matched or not, and the only
    thing that emptied it was a dataset refresh. A person typing is not a reason
    for the bot to keep growing between them."""
    from ufcbot.stats.service import NAME_CACHE

    service = a_service(tmp_path)
    for i in range(NAME_CACHE * 2 + 5):
        service.resolve(f"nobody {i}")

    assert len(service._resolved) <= NAME_CACHE
    assert service.resolve("Arnold Allen") is not None, "still answering after the sweep"


async def test_one_failed_spawn_does_not_condemn_the_bot_to_training_in_process(tmp_path, monkeypatch):
    """Training in the bot's own process leaves pandas and scikit-learn resident
    for good -- about 150 MB that only a restart gives back -- so a machine
    briefly out of handles must not decide it for the life of the bot."""
    from ufcbot.stats import service as service_module
    from ufcbot.stats.service import SPAWN_ATTEMPTS

    service = a_service(tmp_path)
    attempts, in_process = [], []

    def spawning(**kwargs):
        attempts.append(1)
        raise OSError("no handles")

    monkeypatch.setattr(service_module, "ProcessPoolExecutor", spawning)
    monkeypatch.setattr(
        service_module.asyncio, "to_thread",
        lambda call, *a, **k: _done(in_process.append(1)),
    )

    for _ in range(SPAWN_ATTEMPTS):
        await service._run_refresh(force_retrain=False)

    assert len(attempts) == SPAWN_ATTEMPTS, "it kept asking for a worker"
    assert len(in_process) == SPAWN_ATTEMPTS, "and fell back each time"

    # Only once it has failed this many times in a row does it stop trying.
    await service._run_refresh(force_retrain=False)
    assert len(attempts) == SPAWN_ATTEMPTS, "stopped asking after the third failure"
    assert len(in_process) == SPAWN_ATTEMPTS + 1


async def _done(value=None):
    return value


# -- the all-time boards -----------------------------------------------------------


def test_a_retired_fighter_is_off_the_current_board_and_top_of_the_all_time_one():
    """The whole difference between the two boards. A rating is what a fighter
    earned, and retiring does not unearn it -- but a list of who is best now
    should not be topped by someone who has stopped."""
    from ufcbot.stats.rankings import all_time

    retired = rated("Georges St-Pierre", 1260, fights=22, division="Welterweight", ago=3000)
    active = rated("Kamaru Usman", 1171, fights=20, division="Welterweight", ago=60)
    ledgers = {"gsp": retired, "usman": active}

    now = [e.name for e in rank_division(ledgers, "Welterweight", on=TODAY)]
    ever = [e.name for e in all_time(ledgers, "Welterweight")]

    assert now == ["Kamaru Usman"], "nine years out, so not a current ranking"
    assert ever[0] == "Georges St-Pierre"


def test_the_all_time_board_does_not_fade_a_career_for_a_layoff():
    """The current board halves what an idle fighter holds over the starting
    rating. Doing that here would rank the dead by how long they have been dead."""
    from ufcbot.stats.rankings import all_time, career_score

    led = rated("Khabib Nurmagomedov", 1207, fights=13, division="Lightweight", ago=2500)
    entry = all_time({"khabib": led}, "Lightweight")[0]

    assert entry.rating == career_score(led), "scored as a career, not faded"
    assert rating_on(led, TODAY) < 1207, "the current board does fade it"


def test_a_career_is_not_judged_on_the_rating_it_ended_with():
    """Anderson Silva gave back 120 points going 1-6 at the end. Judging a career
    on its last day is judging it on its decline, and the fitted strength reads
    the whole record rather than where it stopped."""
    from ufcbot.stats.rankings import career_score

    declined = rated("Anderson Silva", 1096, fights=25)
    declined.strength = 1216
    steady = rated("Someone Else", 1150, fights=12)
    steady.strength = 1150

    assert career_score(declined) > career_score(steady)


def test_beating_the_same_man_for_the_belt_outranks_the_longer_career():
    """Volkanovski beat Holloway three times for the featherweight title and the
    rating still had Holloway above him, because a rating adds up every result
    and a longer career adds up more of them. What it was missing is the belt."""
    from ufcbot.stats.rankings import all_time

    volk = rated("Volkanovski", 1186, fights=18, division="Featherweight",
                 strength=1186, defences=5, titles=8)
    holloway = rated("Holloway", 1206, fights=33, division="Featherweight",
                     strength=1206, defences=3, titles=5)

    assert holloway.strength > volk.strength, "the rating alone has it the wrong way round"
    assert [e.name for e in all_time({"v": volk, "h": holloway}, "Featherweight")][0] == "Volkanovski"


def test_an_all_time_board_lists_a_fighter_where_they_fought_most():
    """St-Pierre finished at middleweight after twenty-one welterweight fights.
    Asked what someone did over a career, the last fight is the wrong one."""
    from ufcbot.stats.rankings import all_time

    led = rated("Georges St-Pierre", 1260, fights=22, division="Middleweight", ago=3000)
    led.home_division = "Welterweight"


    assert [e.name for e in all_time({"gsp": led}, "Welterweight")] == ["Georges St-Pierre"]
    assert all_time({"gsp": led}, "Middleweight") == [], "not where he belongs"
    assert all_time({"gsp": led}, None)[0].division == "Welterweight", "named by it too"


def test_the_all_time_board_asks_for_more_of_a_career_than_the_current_one():
    from ufcbot.stats.rankings import ALL_TIME_MIN_FIGHTS, MIN_FIGHTS, all_time

    assert ALL_TIME_MIN_FIGHTS > MIN_FIGHTS
    brief = rated("Debutant", 1300, fights=MIN_FIGHTS, division="Lightweight", ago=30)

    assert rank_division({"d": brief}, "Lightweight", on=TODAY), "enough for the current board"
    assert all_time({"d": brief}, "Lightweight") == [], "not enough for an all-time one"


def test_an_interim_belt_is_not_the_belt():
    """Poirier and Gaethje each won interim lightweight titles between Khabib's
    defences. Counted as title changes they put someone else in the chair, and
    handed his three defences back to him as four separate reigns."""
    from ufcbot.stats.career import is_lineal_title

    assert is_lineal_title(True, "UFC Lightweight Title Bout")
    assert not is_lineal_title(True, "UFC Interim Lightweight Title Bout")


def test_a_tournament_final_is_not_a_title_defence():
    """ufcstats flags the Ultimate Fighter finals as title fights, and they
    normalise to a real division, so they land in the middle of its lineage."""
    from ufcbot.stats.career import is_lineal_title

    assert not is_lineal_title(True, "Ultimate Fighter 27 Lightweight Tournament Title Bout")
    assert not is_lineal_title(False, "UFC Lightweight Bout"), "not a title fight at all"


def test_a_career_is_scored_on_the_fitted_strength_not_the_running_rating():
    """The running rating starts everybody in the middle and takes a career to
    leave it, so every summary of it pays for length: ten more fights were worth
    nearly twice what ten points of win rate were. The fitted strength has no
    starting point, and the all-time boards rank on that."""
    from ufcbot.stats.rankings import career_score

    short_and_good = rated("Khabib", 1193, fights=13)
    short_and_good.strength = 1276
    long_and_decent = rated("Journeyman", 1212, fights=37)
    long_and_decent.strength = 1150

    assert long_and_decent.elo > short_and_good.elo, "the running rating has it this way"
    assert career_score(short_and_good) > career_score(long_and_decent)


def test_the_belt_goes_to_the_last_title_fight_won_not_the_last_lineal_one():
    """The data only ever says someone won a title. It never says a champion
    vacated, was stripped, or was elevated from interim, so the lineal chain
    alone leaves the belt wherever it was last won outright. Aspinall vs Gane
    was a no contest and Aspinall then vacated, which left the heavyweight belt
    sitting with Jon Jones from two years earlier."""
    from ufcbot.stats.career import is_lineal_title

    # The marker follows every title fight; the defence count follows only these.
    assert is_lineal_title(True, "UFC Heavyweight Title Bout")
    assert not is_lineal_title(True, "UFC Interim Heavyweight Title Bout")


def test_a_belt_held_only_as_interim_still_counts_as_having_held_one():
    """Both of Aspinall's heavyweight titles were interim, so his lineal count
    is zero, and a fighter who held a belt held a belt."""
    from ufcbot.stats.career import Ledger

    interim_only = Ledger(name="Tom Aspinall")
    interim_only.belts_held = ("Heavyweight",)

    assert interim_only.title_wins == 0, "both of them were interim"
    assert "Heavyweight" in interim_only.belts_held


def test_a_champion_who_has_aged_off_the_board_is_a_former_champion():
    """The data says who won a title fight, never who vacated one. The case it
    can catch is the champion who has since gone: Jon Jones showed as heavyweight
    champion two years after he last held the belt."""
    from ufcbot.stats.rankings import holds_belt

    led = rated("Jon Jones", 1296, fights=24, ago=700, division="Light Heavyweight")
    led.champion_of = "Light Heavyweight"
    led.belts_held = ("Light Heavyweight",)

    assert not holds_belt(led, TODAY), "eighteen months without a fight"
    assert holds_belt(led, None), "with no date to check against, the flag stands"

    still_here = rated("Islam Makhachev", 1273, fights=19, ago=60)
    still_here.champion_of = "Lightweight"
    still_here.belts_held = ("Lightweight",)
    assert holds_belt(still_here, TODAY)


def test_the_strength_fit_puts_the_better_record_above_the_longer_one():
    """The whole point of fitting instead of walking. Both beat the same man;
    one of them did it four times and lost twice, the other twice and never."""
    from ufcbot.stats.strength import fit

    bouts = [("better", "journeyman")] * 2
    bouts += [("longer", "journeyman")] * 4 + [("journeyman", "longer")] * 2

    rated = fit(bouts)

    assert rated["better"] > rated["longer"]
    assert rated["longer"] > rated["journeyman"]


def test_an_unbeaten_record_does_not_run_away_to_infinity():
    """Nothing in a perfect record contradicts "infinitely good", so the pull
    toward the middle is what makes a short unbeaten career merely very good."""
    from ufcbot.stats.strength import CENTRE, fit

    five_oh = fit([("perfect", f"opponent{i}") for i in range(5)])
    twenty_oh = fit([("perfect", f"opponent{i}") for i in range(20)])

    assert CENTRE < five_oh["perfect"] < twenty_oh["perfect"] < CENTRE + 1000
    assert all(v < CENTRE for k, v in five_oh.items() if k != "perfect")


def test_beating_a_better_fighter_is_worth_more():
    from ufcbot.stats.strength import fit

    # One man beats the fighter everybody loses to; the other beats a nobody.
    bouts = [("strong", f"victim{i}") for i in range(6)]
    bouts += [("beat_the_strong", "strong"), ("beat_a_nobody", "victim0")]

    rated = fit(bouts)

    assert rated["beat_the_strong"] > rated["beat_a_nobody"]


def test_no_fights_is_no_ratings():
    from ufcbot.stats.strength import fit

    assert fit([]) == {}
