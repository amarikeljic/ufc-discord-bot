"""Odds from Polymarket, used for fights no sportsbook line reaches.

ESPN carries DraftKings moneylines for numbered UFC cards, but not for Dana
White's Contender Series. Polymarket lists both. It is a prediction market
rather than a bookmaker: a price is the crowd's probability, so 0.545 means a
fighter is a 54.5% favourite, which reads as a moneyline of -120. Because the
two sides sum to 1, the price carries no bookmaker margin.

Everything here is best effort. A miss returns ``None`` and the caller shows the
fight without odds.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from urllib.parse import quote

from ..models import Bout, Fighter
from ..util import normalise, parse_api_datetime
from .http import HttpClient, HttpError

log = logging.getLogger(__name__)

GAMMA = "https://gamma-api.polymarket.com"
PROVIDER = "Polymarket"

# Fighters meet more than once; only trust a market that sits near this bout.
MATCH_WINDOW = timedelta(days=4)
# Prices this lopsided convert to absurd moneylines and usually mean a settled market.
MIN_PRICE = 0.01


def american(price: float) -> int | None:
    """A market price (0-1) as an American moneyline: 0.545 -> -120, 0.25 -> +300."""
    if price < MIN_PRICE or price > 1 - MIN_PRICE:
        return None
    if price >= 0.5:
        return round(-100 * price / (1 - price))
    return round(100 * (1 - price) / price)


def _json_list(raw) -> list:
    if isinstance(raw, list):
        return raw
    try:
        value = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return []
    return value if isinstance(value, list) else []


def _names_match(outcome: str, fighter: Fighter) -> bool:
    """Polymarket writes either "Joshua Van" or just "Van"."""
    wanted = normalise(fighter.display_name)
    got = normalise(outcome)
    if not got or not wanted:
        return False
    if got == wanted:
        return True
    tokens = wanted.split()
    return bool(tokens) and got == tokens[-1]


def _market_time(market: dict) -> datetime | None:
    """``gameStartTime`` looks like ``2026-09-15 23:00:00+00``."""
    raw = market.get("gameStartTime") or market.get("startDate")
    if not isinstance(raw, str):
        return None
    text = raw.strip().replace(" ", "T")
    if text.endswith("+00"):
        text += ":00"
    return parse_api_datetime(text)


class PolymarketOdds:
    """Looks up one fight's moneyline on Polymarket's public Gamma API. No key needed."""

    def __init__(self, http: HttpClient) -> None:
        self.http = http

    async def odds_for(self, bout: Bout, *, around: datetime, ttl: int = 300) -> dict[str, int] | None:
        """Athlete id -> moneyline, or None when this fight has no open market."""
        if not bout.has_opponents:
            return None
        a, b = bout.fighters[0], bout.fighters[1]
        for fighter in (a, b):
            odds = await self._search(fighter.display_name, a, b, around, ttl)
            if odds:
                return odds
        return None

    async def _search(
        self, query: str, a: Fighter, b: Fighter, around: datetime, ttl: int
    ) -> dict[str, int] | None:
        url = f"{GAMMA}/public-search?q={quote(query)}&limit_per_type=10"
        try:
            payload = await self.http.get_json(url, ttl=ttl)
        except HttpError as exc:
            log.debug("Polymarket search failed for %r: %r", query, exc)
            return None
        if not isinstance(payload, dict):
            return None

        for event in payload.get("events") or []:
            for market in event.get("markets") or []:
                odds = self._read_market(market, a, b, around)
                if odds:
                    log.debug("Polymarket priced %s vs %s: %s", a.display_name, b.display_name, odds)
                    return odds
        return None

    @staticmethod
    def _read_market(market: dict, a: Fighter, b: Fighter, around: datetime) -> dict[str, int] | None:
        if market.get("sportsMarketType") != "moneyline":
            return None  # the same fight also has "goes the distance" and method markets
        if market.get("closed") or not market.get("active"):
            return None

        outcomes = _json_list(market.get("outcomes"))
        prices = _json_list(market.get("outcomePrices"))
        if len(outcomes) != 2 or len(prices) != 2:
            return None

        # Both fighters must appear, one per outcome, so a market for another bout is ignored.
        sides: dict[str, int] = {}
        for index, outcome in enumerate(outcomes):
            for fighter in (a, b):
                if fighter.id not in sides and _names_match(str(outcome), fighter):
                    sides[fighter.id] = index
                    break
        if len(sides) != 2:
            return None

        when = _market_time(market)
        if when is not None and abs(when - around) > MATCH_WINDOW:
            return None  # a rematch, or the same names on an older card

        odds: dict[str, int] = {}
        for athlete_id, index in sides.items():
            try:
                line = american(float(prices[index]))
            except (TypeError, ValueError):
                return None
            if line is None:
                return None
            odds[athlete_id] = line
        return odds
