"""Reading the sportsbook archive, where a parsing bug costs a crawl.

The first pass at this read the moneyline one level too shallow, decided the
archive held no prices at all, and the only way to find out otherwise was to
fetch the whole thing again. Hence tests on the shape rather than on the numbers.
"""

from __future__ import annotations

import pytest

from benchmark import STAKE, _american, _athlete, _implied, market, payout


def side(athlete: str, *, open_ml: str | None = None, close_ml: str | None = None) -> dict:
    """One corner of a market, nested the way ESPN nests it."""
    got: dict = {"athlete": {"$ref": f"http://sports.core.api.espn.com/v2/sports/mma/athletes/{athlete}?lang=en"}}
    for which, value in (("open", open_ml), ("close", close_ml)):
        if value is not None:
            got[which] = {"moneyLine": {"american": value, "decimal": 1.32}}
    return got


def bout(home: dict, away: dict, *, provider: str = "espn bet", on: str = "2024-06-01") -> dict:
    return {"E:1": {"event": "UFC 300", "date": on,
                    "books": [{"provider": provider, "home": home, "away": away}]}}


# -- the moneyline, two levels down ------------------------------------------


def test_the_moneyline_is_nested_under_its_own_key():
    """``side['close']['moneyLine']['american']``. One level up is a dict that
    looks like a miss, which is how this went wrong the first time."""
    assert _american(side("1", close_ml="-310"), "close") == -310
    assert _american(side("1", open_ml="+250"), "open") == 250, "the plus is dropped"


def test_a_price_that_is_not_there_is_not_a_price():
    assert _american(side("1", close_ml="-310"), "open") is None, "no opening line on this one"
    assert _american({}, "close") is None
    assert _american({"close": "nonsense"}, "close") is None
    assert _american({"close": {"moneyLine": {"american": "EVEN"}}}, "close") is None
    assert _american({"close": {"moneyLine": {"american": "n/a"}}}, "close") is None


def test_a_bare_number_is_read_too():
    """Some payloads put the current price straight on the key."""
    assert _american({"current": {"moneyLine": -150}}, "current") == -150


def test_the_fighter_comes_off_the_market_not_the_corner():
    """ESPN does not fill in home and away for MMA -- both competitors come
    back as the same corner -- so the athlete is taken from the odds payload."""
    assert _athlete(side("4916248")) == "4916248"
    assert _athlete({}) is None
    assert _athlete(None) is None


# -- probabilities -----------------------------------------------------------


def test_a_moneyline_reads_as_a_probability():
    assert _implied(-300) == pytest.approx(0.75)
    assert _implied(100) == pytest.approx(0.5)
    assert _implied(300) == pytest.approx(0.25)


def test_the_two_sides_are_normalised_so_the_margin_comes_out():
    """A book's prices sum to more than one and the surplus is its fee, not its
    opinion. Left in, the market reads as overconfident on both men at once."""
    got = market(bout(side("A", close_ml="-200"), side("B", close_ml="+150")), "close")

    prices = got["E:1"]["p"]
    assert sum(prices.values()) == pytest.approx(1.0)
    assert prices["A"] > prices["B"]
    # The raw lines are kept beside them, because the fee is what you pay.
    assert got["E:1"]["ml"] == {"A": -200, "B": 150}


def test_a_bout_missing_one_side_is_left_out():
    """Half a market cannot be normalised, and a one-sided price is not one."""
    assert market(bout(side("A", close_ml="-200"), side("B")), "close") == {}


def test_only_the_books_that_price_a_fight_before_it_starts():
    """ESPN Bet - Live Odds prices a fight in progress: it reads -2000 on a man
    three minutes from losing, which is not an estimate of who wins."""
    live = bout(side("A", close_ml="-2000"), side("B", close_ml="+600"),
                provider="espn bet - live odds")

    assert market(live, "close") == {}


# -- the money arithmetic ----------------------------------------------------


def test_the_payout_is_the_one_pickem_pays():
    """The benchmark's units are pick'em points, so the two must agree or the
    money column is measuring a game nobody is playing."""
    from ufcbot.features.pickem import points_for

    for moneyline in (-290, -200, -110, 100, 110, 150, 235, 900):
        assert payout(moneyline) == pytest.approx(points_for(moneyline), abs=0.5), moneyline


def test_a_hundred_on_an_even_money_fight_wins_a_hundred():
    assert payout(100) == STAKE
