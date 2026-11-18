"""A fight's price, once known, is not lost to a market that stops answering.

During a card pick'em used to show "odds not posted" for every fight: the
market was asked afresh on each redraw, a fight's market closes once it is
settled, and a search that fails under load comes back empty -- and each of
those read as no price at all, with nothing kept to fall back on. The market is
faked; the database that remembers is real.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from conftest import bout, card, fighter

from ufcbot.embeds import pickem_board_embed
from ufcbot.embeds.pickem import LAST_KNOWN, NOT_POSTED
from ufcbot.models import ODDS_KNOWN, ODDS_LAST_KNOWN, ODDS_NOT_POSTED
from ufcbot.sources.espn import UFCData

ALLEN = fighter("1", "Arnold Allen")
PICO = fighter("2", "Aaron Pico")
EVLOEV = fighter("3", "Movsar Evloev")
PRE_FIGHT = {ALLEN.id: -150, PICO.id: 130}


class Market:
    """Polymarket, answering with whatever the test says: prices, nothing, or an error."""

    def __init__(self) -> None:
        self.prices: dict[str, int] | None = None
        self.fails = False
        self.asked = 0

    async def odds_for(self, fight, *, around, ttl=300):
        self.asked += 1
        if self.fails:
            raise RuntimeError("timed out")
        return dict(self.prices) if self.prices else None


def a_fight(*, starts_in: timedelta, b=PICO):
    """A bout as a fresh read of the card builds it: no price on it yet."""
    fight = bout("B1", ALLEN, b)
    fight.start = datetime.now(UTC) + starts_in
    return fight


async def priced(storage, market, *, starts_in=timedelta(days=2), b=PICO):
    fight = a_fight(starts_in=starts_in, b=b)
    await UFCData(None, odds=market, odds_memory=storage).load_bout_odds(fight)
    return fight


async def test_a_fetch_that_comes_back_empty_keeps_the_price(storage):
    market = Market()
    market.prices = PRE_FIGHT
    first = await priced(storage, market)
    assert (first.odds, first.odds_state) == (PRE_FIGHT, ODDS_KNOWN)

    market.prices = None  # the search found nothing this time
    later = await priced(storage, market)

    assert later.odds == PRE_FIGHT, "still shown"
    assert later.odds_state == ODDS_LAST_KNOWN, "and said to be the last known, not the current one"


async def test_a_fetch_that_fails_keeps_the_price(storage):
    market = Market()
    market.prices = PRE_FIGHT
    await priced(storage, market)

    market.fails = True
    later = await priced(storage, market)

    assert (later.odds, later.odds_state) == (PRE_FIGHT, ODDS_LAST_KNOWN)


async def test_a_fresh_price_still_replaces_the_old_one_before_the_fight(storage):
    market = Market()
    market.prices = PRE_FIGHT
    await priced(storage, market)

    market.prices = {ALLEN.id: -200, PICO.id: 170}  # the line moved during the week
    later = await priced(storage, market)

    assert (later.odds, later.odds_state) == ({ALLEN.id: -200, PICO.id: 170}, ODDS_KNOWN)


async def test_once_the_fight_locks_the_closing_line_stays(storage):
    """For the rest of the event, whatever the market does: closes, settles at
    1 and 0, or trades on inside the fight."""
    market = Market()
    market.prices = PRE_FIGHT
    await priced(storage, market, starts_in=timedelta(minutes=10))

    market.prices = {ALLEN.id: -2500, PICO.id: 1400}  # a knockdown in round one
    asked = market.asked
    during = await priced(storage, market, starts_in=-timedelta(minutes=20))

    assert (during.odds, during.odds_state) == (PRE_FIGHT, ODDS_KNOWN)
    assert market.asked == asked, "a locked fight does not ask the market at all"


async def test_the_price_outlives_a_restart(storage):
    market = Market()
    market.prices = PRE_FIGHT
    await priced(storage, market)

    market.prices = None
    after = await priced(storage, Market())  # a new process, the same database

    assert (after.odds, after.odds_state) == (PRE_FIGHT, ODDS_LAST_KNOWN)


async def test_a_replacement_does_not_inherit_the_old_fights_price(storage):
    market = Market()
    market.prices = PRE_FIGHT
    await priced(storage, market)

    market.prices = None
    replaced = await priced(storage, market, b=EVLOEV)  # Pico is out

    assert (replaced.odds, replaced.odds_state) == ({}, ODDS_NOT_POSTED)


async def test_the_board_tells_the_three_states_apart():
    soon = datetime.now(UTC) + timedelta(days=2)
    pairs = [(fighter(f"{i}1", f"Al {i}"), fighter(f"{i}2", f"Bo {i}")) for i in (1, 2, 3)]
    known, stale, unpriced = (bout(f"B{i}", a, b, match=i) for i, (a, b) in enumerate(pairs, 1))
    for fight, state in ((known, ODDS_KNOWN), (stale, ODDS_LAST_KNOWN)):
        fight.odds = {fight.fighters[0].id: -150, fight.fighters[1].id: 130}
        fight.odds_state = state
    embed = pickem_board_embed(card(known, stale, unpriced, start=soon), {}, 0, now=datetime.now(UTC))
    shown = {field.name: field.value for field in embed.fields}

    assert "-150" in shown[known.matchup] and LAST_KNOWN not in shown[known.matchup]
    assert "-150" in shown[stale.matchup] and LAST_KNOWN in shown[stale.matchup]
    assert NOT_POSTED in shown[unpriced.matchup] and "-150" not in shown[unpriced.matchup]
