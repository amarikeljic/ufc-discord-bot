"""The shapes the bot passes around: settings, picks, board state and results.

Plain records with no database in them. They are what ``storage.py`` reads and
writes, but the embeds, the buttons and every feature want the shape rather than
the store, and importing these from the database module drags the schema and a
SQLite driver along behind them.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime
from typing import NamedTuple


@dataclass(slots=True)
class GuildSettings:
    guild_id: int
    sync_enabled: bool = False
    days_ahead: int = 60
    duration_minutes: int = 240
    start_anchor: str = "main_card"
    include_contender_series: bool = False
    announce_channel_id: int | None = None
    predictions_channel_id: int | None = None
    accuracy_channel_id: int | None = None
    schedule_channel_id: int | None = None
    tracking_since: date | None = None
    live_channel_id: int | None = None
    pickem_channel_id: int | None = None
    rankings_channel_id: int | None = None
    rankings_include_women: bool = True

    @property
    def has_channels(self) -> bool:
        return any(
            (
                self.predictions_channel_id,
                self.accuracy_channel_id,
                self.schedule_channel_id,
                self.live_channel_id,
                self.pickem_channel_id,
                self.rankings_channel_id,
            )
        )


class Post(NamedTuple):
    """A message the bot maintains, with a hash of what it currently shows."""

    channel_id: int
    message_id: int
    signature: str | None = None


class RankedState(NamedTuple):
    """Where a fighter stood on a board, and what they had done by then."""

    fighter: str
    rank: int
    rating: int
    last_fight: date | None


class CardBout(NamedTuple):
    """One fight as it last stood on a card, enough to describe it once it is gone."""

    bout_id: str
    fighters: tuple[tuple[str, str], ...]
    """(athlete id, name) for each corner, in the order the card lists them."""
    weight_class: str | None = None

    @property
    def athletes(self) -> set[str]:
        return {athlete for athlete, _name in self.fighters}

    @property
    def matchup(self) -> str:
        return " vs. ".join(name for _athlete, name in self.fighters) or "TBA"

    def name_of(self, athlete_id: str) -> str:
        return next((name for athlete, name in self.fighters if athlete == athlete_id), athlete_id)


@dataclass(slots=True)
class ShortNotice:
    """A fighter who stepped into a bout after it was made, and how long before.

    Kept because no public dataset records it and the bot is already watching
    for it. Nothing reads it yet.
    """

    espn_event_id: str
    bout_id: str
    arrived: str
    departed: str | None
    opponent: str | None
    weight_class: str | None
    event_start: datetime
    noticed_at: datetime
    days_notice: float


@dataclass(slots=True)
class PickemRecord:
    """One member's pick for one bout."""

    guild_id: int
    user_id: int
    espn_event_id: str
    bout_id: str
    event_name: str
    event_start: datetime
    athlete_id: str
    athlete_name: str
    opponent_id: str
    opponent_name: str
    odds: int
    points_if_right: int
    locks_at: datetime
    picked_at: datetime
    result: str | None = None
    """"win", "loss" or "void" once graded."""
    points: int | None = None
    graded_at: datetime | None = None


@dataclass(slots=True)
class PickemStanding:
    user_id: int
    points: int
    wins: int
    losses: int
    cards: int

    @property
    def win_rate(self) -> float:
        settled = self.wins + self.losses
        return self.wins / settled if settled else 0.0


@dataclass(slots=True)
class PickemSummary:
    user_id: int
    points: int = 0
    wins: int = 0
    losses: int = 0
    voids: int = 0
    pending: int = 0
    underdog_wins: int = 0
    best_hit: int = 0
    best_hit_name: str | None = None
    rank: int | None = None
    players: int = 0

    @property
    def win_rate(self) -> float:
        settled = self.wins + self.losses
        return self.wins / settled if settled else 0.0


@dataclass(slots=True)
class PickemCard:
    espn_event_id: str
    event_name: str
    event_start: datetime
    picks: int
    wins: int
    losses: int
    pending: int
    points: int


@dataclass(slots=True)
class PredictionRecord:
    espn_event_id: str
    bout_id: str
    event_name: str
    event_start: datetime
    athlete_a: str
    name_a: str
    athlete_b: str
    name_b: str
    prob_a: float
    weight_class: str | None = None
    position: int = 0
    odds_a: int | None = None
    odds_b: int | None = None
    method: str | None = None
    """The favourite's likeliest winning method at lock."""
    technique: str | None = None
    method_prob: float | None = None
    detail_json: str | None = None
    """Full outcome distribution, so boards render the same after lock."""

    winner_athlete: str | None = None
    correct: bool | None = None
    graded_at: datetime | None = None
    result_method: str | None = None
    result_technique: str | None = None
    result_round: int | None = None
    result_time: str | None = None
    method_correct: bool | None = None
    technique_correct: bool | None = None

    @property
    def favourite_athlete(self) -> str:
        return self.athlete_a if self.prob_a >= 0.5 else self.athlete_b

    @property
    def favourite(self) -> str:
        return self.name_a if self.prob_a >= 0.5 else self.name_b

    @property
    def confidence(self) -> float:
        return max(self.prob_a, 1 - self.prob_a)

    @property
    def winner_name(self) -> str | None:
        if self.winner_athlete == self.athlete_a:
            return self.name_a
        if self.winner_athlete == self.athlete_b:
            return self.name_b
        return None

    @property
    def matchup(self) -> str:
        return f"{self.name_a} vs. {self.name_b}"

    @property
    def market_favourite_athlete(self) -> str | None:
        """Whoever the sportsbook had shorter, or None without two lines."""
        if self.odds_a is None or self.odds_b is None or self.odds_a == self.odds_b:
            return None
        return self.athlete_a if self.odds_a < self.odds_b else self.athlete_b

    @property
    def detail(self) -> dict:
        try:
            return json.loads(self.detail_json) if self.detail_json else {}
        except ValueError:
            return {}
