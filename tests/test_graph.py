"""The professional fight graph, whose job is to be right about time.

Everything here is about one thing: a rating must never have seen the fight it
is about to help predict. The rest -- deduplication, missing files, unknown
fighters -- is about the graph being an optional extra that cannot take the
build down with it.
"""

from __future__ import annotations

import json
from datetime import date

from ufcbot.stats.graph import GRAPH_START, SAFE_GAP, Graph, load
from ufcbot.util import normalise


def careers(**people) -> dict:
    """``name=[(date, result, opponent), ...]`` as the cache stores it."""
    return {
        normalise(name): {
            "espn_id": name,
            "name": name,
            "fights": [{"on": on, "result": res, "opponent": opp, "event": "X"}
                       for on, res, opp in fights],
        }
        for name, fights in people.items()
    }


def test_a_fight_on_both_cards_is_counted_once():
    """Every bout between two cached fighters appears on both their records.
    Counted twice it would move the ratings twice as far."""
    graph = load_from(careers(
        Ann=[("2020-01-01", "W", "Bob")],
        Bob=[("2020-01-01", "L", "Ann")],
    ))

    assert len(graph.fights) == 1
    assert graph.fights[0] == (date(2020, 1, 1), "ann", "bob")


def test_draws_and_no_contests_are_left_out():
    """The rating reads wins and losses, the same as the one the boards use."""
    graph = load_from(careers(
        Ann=[("2020-01-01", "D", "Bob"), ("2020-06-01", "NC", "Cid"),
             ("2021-01-01", "W", "Dee")],
    ))

    assert [f[2] for f in graph.fights] == ["dee"]


def test_fights_come_out_in_date_order():
    """The walk only goes forwards, so the list has to be sorted or it would
    apply a 2024 result before a 2019 one."""
    graph = load_from(careers(
        Ann=[("2022-05-01", "W", "Bob"), ("2019-01-01", "W", "Cid"),
             ("2021-03-01", "L", "Dee")],
    ))

    assert [f[0] for f in graph.fights] == sorted(f[0] for f in graph.fights)


def test_the_walk_stops_short_of_the_cutoff():
    graph = load_from(careers(
        Ann=[("2020-01-01", "W", "Bob"), ("2020-02-01", "W", "Cid"),
             ("2020-03-01", "W", "Dee")],
    ))

    graph.advance_to(date(2020, 2, 1))
    rating, seen = graph.standing("ann")

    assert seen == 1, "only the January fight is in the past"
    assert rating > GRAPH_START


def test_a_bout_can_never_be_in_the_rating_that_predicts_it():
    """ESPN dates a card in UTC and ufcstats dates it locally, so the same fight
    can carry dates a day apart. Without the margin a bout would count as
    preceding itself, on exactly the fights being scored."""
    graph = load_from(careers(Ann=[("2020-03-02", "W", "Bob")]))

    # The replay asks for everything safely before the bout it is about to rate.
    graph.advance_to(date(2020, 3, 1) - SAFE_GAP)

    assert graph.standing("ann") == (GRAPH_START, 0), "its own result stayed out"


def test_advancing_twice_does_not_apply_a_fight_twice():
    """The replay calls this once per bout, thousands of times, with the same
    date repeatedly for one card."""
    graph = load_from(careers(Ann=[("2020-01-01", "W", "Bob")]))

    graph.advance_to(date(2021, 1, 1))
    once = graph.standing("ann")
    graph.advance_to(date(2021, 1, 1))
    graph.advance_to(date(2022, 1, 1))

    assert graph.standing("ann") == once


def test_a_fighter_nobody_has_heard_of_starts_level():
    graph = Graph(fights=[])

    assert graph.standing("who") == (GRAPH_START, 0)


def test_beating_someone_rated_higher_moves_further():
    """It is Elo, so the win over a good opponent has to be worth more. Without
    that the graph is a win count and the whole point is lost."""
    strong = load_from(careers(
        Ace=[("2019-01-01", "W", "Pat"), ("2019-02-01", "W", "Quinn"),
             ("2019-03-01", "W", "Rae"), ("2020-01-01", "L", "Underdog")],
        Flop=[("2019-01-01", "L", "Pat"), ("2019-02-01", "L", "Quinn"),
              ("2019-03-01", "L", "Rae"), ("2020-02-01", "L", "Underdog")],
    ))
    strong.advance_to(date(2021, 1, 1))

    over_good, _ = strong.standing("underdog")
    assert over_good > GRAPH_START
    assert strong.standing("ace")[0] > strong.standing("flop")[0]


# -- the file it comes from --------------------------------------------------


def test_no_cache_is_not_an_error(tmp_path):
    """The crawl cannot run on the refresh timer, so the build has to work
    without the file and simply learn nothing from it."""
    assert load(tmp_path, normalise=normalise) is None


def test_a_broken_cache_is_not_an_error(tmp_path):
    (tmp_path / "espn_careers.json").write_text("{not json", encoding="utf-8")

    assert load(tmp_path, normalise=normalise) is None


def test_a_cache_with_junk_entries_keeps_the_good_ones(tmp_path):
    """A fighter ESPN had nothing for is stored as null, and a fight can be
    missing its date or carry one that will not parse."""
    data = careers(Ann=[("2020-01-01", "W", "Bob")])
    data["gone"] = None
    data["odd"] = {"fights": [
        {"on": None, "result": "W", "opponent": "Bob"},
        {"on": "not-a-date", "result": "W", "opponent": "Bob"},
        {"on": "2020-01-01", "result": "W", "opponent": None},
    ]}
    (tmp_path / "espn_careers.json").write_text(json.dumps(data), encoding="utf-8")

    graph = load(tmp_path, normalise=normalise)

    assert graph is not None
    assert len(graph.fights) == 1


def test_a_corner_nobody_has_filled_is_not_a_fighter():
    """ESPN hands a card's unfilled corner an athlete id and a fight history like
    anybody else's. Rated, it collects a result from every card it ever appears
    on and becomes a fighter called TBA, who the bot would then pick by name to
    beat somebody real."""
    graph = load_from(careers(
        Ann=[("2020-01-01", "W", "TBA"), ("2020-06-01", "W", "Bob")],
        Cal=[("2021-01-01", "L", "Opponent TBA")],
        Dee=[("2021-06-01", "W", "TBD")],
    ))

    assert [f[1:] for f in graph.fights] == [("ann", "bob")]
    graph.advance_to(date(2030, 1, 1))
    assert "tba" not in graph.rating
    assert "opponent tba" not in graph.rating
    assert "tbd" not in graph.rating


def test_a_real_name_is_not_mistaken_for_a_placeholder():
    """On whole words, so a man is not dropped for the letters in his name."""
    from ufcbot.stats.graph import is_placeholder

    assert is_placeholder("tba")
    assert is_placeholder("opponent tba")
    assert is_placeholder("to be announced")
    assert not is_placeholder("tbasa ngoma"), "a name that merely starts with it"
    assert not is_placeholder("otba")


def test_a_placeholder_never_reaches_the_newcomers():
    """Nothing can carry one into the bot, because nothing rates one."""
    graph = load_from(careers(
        Ann=[("2020-01-01", "W", "TBA"), ("2020-02-01", "W", "TBA"),
             ("2020-03-01", "W", "TBA"), ("2020-04-01", "W", "TBA")],
    ))
    graph.advance_to(date(2030, 1, 1))

    assert graph.newcomers(set()) == {}, "four fights against a phantom is no fighter"


def test_a_graph_cannot_be_rewound():
    """One graph handed to two passes over the dataset is the way this goes
    wrong: the second pass finds it already at the end of the data, every
    advance a no-op, and every fight rated by its own result. It is worth a
    crash, because the numbers it produces look like a breakthrough -- eleven
    points of accuracy over the closing line -- rather than like a bug."""
    import pytest

    graph = load_from(careers(
        Ann=[("2020-01-01", "W", "Bob"), ("2021-01-01", "W", "Cal")],
    ))
    graph.advance_to(date(2022, 1, 1))

    with pytest.raises(ValueError, match="cannot rewind"):
        graph.advance_to(date(2020, 6, 1))


def test_the_same_cutoff_twice_is_fine():
    """The replay asks per bout, and two bouts on one card share a date."""
    graph = load_from(careers(Ann=[("2020-01-01", "W", "Bob")]))

    graph.advance_to(date(2021, 1, 1))
    graph.advance_to(date(2021, 1, 1))

    assert graph.seen["ann"] == 1, "not applied twice either"


def load_from(data: dict) -> Graph:
    from ufcbot.stats.graph import _fights_from

    return Graph(fights=_fights_from(data, normalise=normalise))
