"""Reading prices off Polymarket, which is now the only place they come from.

A market is a probability rather than a bookmaker's line, and the same fight
carries several markets: the winner, whether it goes the distance, how it ends.
Picking the wrong one, or the right one for the wrong fight, puts a price on a
pick'em board that members stake points against, so most of what matters here is
refusing a market rather than reading one.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from conftest import fighter

from ufcbot.sources.polymarket import (
    PolymarketOdds,
    _market_time,
    _names_match,
    american,
)

VAN = fighter("1", "Joshua Van")
PANTOJA = fighter("2", "Alexandre Pantoja")
CARD_NIGHT = datetime(2026, 9, 21, 23, 0, tzinfo=UTC)


def market(**overrides) -> dict:
    """A winner market for Van vs Pantoja, as the API sends one."""
    base = {
        "sportsMarketType": "moneyline",
        "active": True,
        "closed": False,
        "outcomes": json.dumps(["Joshua Van", "Alexandre Pantoja"]),
        "outcomePrices": json.dumps(["0.545", "0.455"]),
        "gameStartTime": "2026-09-21 23:00:00+00",
    }
    return base | overrides


def read(**overrides):
    return PolymarketOdds._read_market(market(**overrides), VAN, PANTOJA, CARD_NIGHT)


# -- a probability as a moneyline ---------------------------------------------------


@pytest.mark.parametrize(
    ("price", "line"),
    [(0.545, -120), (0.5, -100), (0.25, 300), (0.8, -400), (0.2, 400), (0.9, -900)],
)
def test_a_market_price_reads_as_an_american_line(price, line):
    assert american(price) == line


def test_the_two_sides_of_a_market_price_back_to_roughly_even_money():
    """No bookmaker margin: the two prices sum to 1, so the implied
    probabilities sum to 1 too, which a real book's never do."""
    favourite, underdog = american(0.6), american(0.4)

    assert favourite == -150 and underdog == 150


@pytest.mark.parametrize("price", [0.0, 0.005, 0.995, 1.0])
def test_a_settled_market_is_not_a_price(price):
    """A market at 0.999 has been decided. As a moneyline it reads -99900, which
    would score a correct pick at a single point."""
    assert american(price) is None


# -- which market, and whose --------------------------------------------------------


def test_the_winner_market_is_read():
    assert read() == {"1": -120, "2": 120}


@pytest.mark.parametrize("kind", ["distance", "method", None, ""])
def test_the_other_markets_on_the_same_fight_are_left_alone(kind):
    """"Goes the distance" is also priced at 0.545 and would look like a winner
    price if it were taken for one."""
    assert read(sportsMarketType=kind) is None


def test_a_market_that_has_closed_or_stopped_trading_is_not_a_price():
    assert read(closed=True) is None
    assert read(active=False) is None


def test_a_market_for_a_different_fight_is_not_borrowed():
    assert read(outcomes=json.dumps(["Islam Makhachev", "Arman Tsarukyan"])) is None
    # One name matching is not enough; both corners have to be there.
    assert read(outcomes=json.dumps(["Joshua Van", "Brandon Moreno"])) is None


def test_a_rematch_priced_for_another_night_is_not_borrowed():
    """The same two names meet more than once, and an old market is still in the
    feed. Only a market sitting near this card counts."""
    assert read(gameStartTime="2026-03-01 23:00:00+00") is None
    # A few days either side is the same card being moved, not another one.
    assert read(gameStartTime="2026-09-22 23:00:00+00") is not None


def test_a_market_with_no_date_is_taken_on_the_names_alone():
    assert read(gameStartTime=None, startDate=None) == {"1": -120, "2": 120}


# -- the shapes the feed actually sends ------------------------------------------------


def test_outcomes_arrive_as_a_json_string_or_as_a_list():
    """The API sends these as strings containing JSON; some responses send real
    lists. Both have been seen."""
    assert read(outcomes=["Joshua Van", "Alexandre Pantoja"], outcomePrices=["0.545", "0.455"]) == {
        "1": -120,
        "2": 120,
    }


@pytest.mark.parametrize(
    "broken",
    [
        {"outcomes": "not json"},
        {"outcomePrices": "not json"},
        {"outcomes": json.dumps(["Only One"])},
        {"outcomePrices": json.dumps(["0.545"])},
        {"outcomePrices": json.dumps(["abc", "def"])},
    ],
)
def test_a_malformed_market_gives_no_price_rather_than_an_error(broken):
    """Pick'em opens on whether a fight has a price. An exception here would
    stop the whole card being read, where no price just leaves that fight out."""
    assert read(**broken) is None


def test_a_surname_only_outcome_still_matches_the_fighter():
    """Polymarket writes "Van" as often as "Joshua Van"."""
    assert _names_match("Van", VAN)
    assert _names_match("joshua van", VAN)
    assert not _names_match("Vanderford", VAN), "a longer name is a different fighter"
    assert not _names_match("", VAN)


def test_the_market_time_is_read_from_the_format_the_feed_uses():
    assert _market_time({"gameStartTime": "2026-09-21 23:00:00+00"}) == CARD_NIGHT
    assert _market_time({"startDate": "2026-09-21T23:00:00Z"}) == CARD_NIGHT
    assert _market_time({}) is None
    assert _market_time({"gameStartTime": 1700000000}) is None


def test_a_price_is_only_given_when_both_corners_have_one():
    """Half a price is no use: pick'em stakes points on the line, and a fight
    with one side priced cannot be scored."""
    assert read(outcomePrices=json.dumps(["0.999", "0.001"])) is None, "settled on both sides"


# -- the window a market has to sit in --------------------------------------------------


@pytest.mark.parametrize("days", [0, 1, 3.9, -3.9])
def test_a_market_near_the_card_counts(days):
    when = CARD_NIGHT + timedelta(days=days)
    assert read(gameStartTime=when.strftime("%Y-%m-%d %H:%M:%S+00")) is not None


@pytest.mark.parametrize("days", [5, -5, 200])
def test_a_market_far_from_the_card_does_not(days):
    when = CARD_NIGHT + timedelta(days=days)
    assert read(gameStartTime=when.strftime("%Y-%m-%d %H:%M:%S+00")) is None
