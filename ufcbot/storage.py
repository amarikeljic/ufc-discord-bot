"""SQLite persistence: guild settings, event links, the prediction ledger, channel posts and live state."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from typing import NamedTuple

import aiosqlite

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS guild_config (
    guild_id                 INTEGER PRIMARY KEY,
    sync_enabled             INTEGER NOT NULL DEFAULT 0,
    days_ahead               INTEGER NOT NULL DEFAULT 60,
    duration_minutes         INTEGER NOT NULL DEFAULT 240,
    start_anchor             TEXT    NOT NULL DEFAULT 'main_card',
    include_contender_series INTEGER NOT NULL DEFAULT 0,
    announce_channel_id      INTEGER,
    updated_at               TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS synced_events (
    guild_id         INTEGER NOT NULL,
    espn_event_id    TEXT    NOT NULL,
    discord_event_id INTEGER NOT NULL,
    signature        TEXT    NOT NULL,
    updated_at       TEXT    NOT NULL,
    PRIMARY KEY (guild_id, espn_event_id)
);

CREATE INDEX IF NOT EXISTS idx_synced_events_guild ON synced_events (guild_id);

-- The model's pick for one bout. Rows are overwritten until the card starts,
-- then frozen, so the final row is the pick that was on record beforehand.
CREATE TABLE IF NOT EXISTS predictions (
    espn_event_id     TEXT    NOT NULL,
    bout_id           TEXT    NOT NULL,
    event_name        TEXT    NOT NULL,
    event_start       TEXT    NOT NULL,
    athlete_a         TEXT    NOT NULL,
    name_a            TEXT    NOT NULL,
    athlete_b         TEXT    NOT NULL,
    name_b            TEXT    NOT NULL,
    prob_a            REAL    NOT NULL,
    weight_class      TEXT,
    position          INTEGER NOT NULL DEFAULT 0,
    updated_at        TEXT    NOT NULL,
    winner_athlete    TEXT,
    correct           INTEGER,
    graded_at         TEXT,
    PRIMARY KEY (espn_event_id, bout_id)
);

CREATE INDEX IF NOT EXISTS idx_predictions_event ON predictions (espn_event_id);

-- Live coverage looks a bout's pick up by bout id on every tick of a card.
CREATE INDEX IF NOT EXISTS idx_predictions_bout ON predictions (bout_id);

-- Grading asks for what is still unscored, which is a handful of rows in a table
-- that only grows. A partial index keeps that lookup the size of the answer.
CREATE INDEX IF NOT EXISTS idx_predictions_ungraded
    ON predictions (event_start) WHERE graded_at IS NULL;

-- The fights last seen on each upcoming card, so a change to one can be spotted.
CREATE TABLE IF NOT EXISTS card_bouts (
    espn_event_id TEXT NOT NULL,
    bout_id       TEXT NOT NULL,
    fighters_json TEXT NOT NULL,
    weight_class  TEXT,
    seen_at       TEXT NOT NULL,
    PRIMARY KEY (espn_event_id, bout_id)
);

-- Each division's ratings board as last published, so a change to it can be
-- described rather than just redrawn.
CREATE TABLE IF NOT EXISTS ranking_state (
    division   TEXT    NOT NULL,
    fighter    TEXT    NOT NULL,
    rank       INTEGER NOT NULL,
    rating     INTEGER NOT NULL,
    last_fight TEXT,
    PRIMARY KEY (division, fighter)
);

-- Messages the bot maintains in configured channels, so they can be edited.
CREATE TABLE IF NOT EXISTS notices_sent (
    kind     TEXT NOT NULL,
    subject  TEXT NOT NULL,
    sent_at  TEXT NOT NULL,
    PRIMARY KEY (kind, subject)
);

CREATE TABLE IF NOT EXISTS short_notice (
    espn_event_id TEXT NOT NULL,
    bout_id       TEXT NOT NULL,
    arrived       TEXT NOT NULL,
    departed      TEXT,
    opponent      TEXT,
    weight_class  TEXT,
    event_start   TEXT NOT NULL,
    noticed_at    TEXT NOT NULL,
    days_notice   REAL NOT NULL,
    PRIMARY KEY (espn_event_id, bout_id, arrived)
);

CREATE TABLE IF NOT EXISTS channel_posts (
    guild_id   INTEGER NOT NULL,
    kind       TEXT    NOT NULL,
    key        TEXT    NOT NULL,
    channel_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    updated_at TEXT    NOT NULL,
    PRIMARY KEY (guild_id, kind, key)
);

-- Cards whose recap has already been posted to a guild's accuracy channel.
CREATE TABLE IF NOT EXISTS recaps_posted (
    guild_id      INTEGER NOT NULL,
    espn_event_id TEXT    NOT NULL,
    posted_at     TEXT    NOT NULL,
    PRIMARY KEY (guild_id, espn_event_id)
);

-- Live coverage: which updates have gone out for a bout, so restarts never repeat them.
CREATE TABLE IF NOT EXISTS live_posts (
    bout_id   TEXT NOT NULL,
    key       TEXT NOT NULL,
    posted_at TEXT NOT NULL,
    PRIMARY KEY (bout_id, key)
);

-- Cumulative fight stats captured at the end of each round, for per-round deltas.
CREATE TABLE IF NOT EXISTS live_snapshots (
    bout_id    TEXT    NOT NULL,
    round      INTEGER NOT NULL,
    stats_json TEXT    NOT NULL,
    PRIMARY KEY (bout_id, round)
);

-- Pick'em: one pick per member per bout. Winning points are fixed from the odds when the pick was made.
CREATE TABLE IF NOT EXISTS pickem_picks (
    guild_id        INTEGER NOT NULL,
    user_id         INTEGER NOT NULL,
    espn_event_id   TEXT    NOT NULL,
    bout_id         TEXT    NOT NULL,
    event_name      TEXT    NOT NULL,
    event_start     TEXT    NOT NULL,
    athlete_id      TEXT    NOT NULL,
    athlete_name    TEXT    NOT NULL,
    opponent_id     TEXT    NOT NULL,
    opponent_name   TEXT    NOT NULL,
    odds            INTEGER NOT NULL,
    points_if_right INTEGER NOT NULL,
    locks_at        TEXT    NOT NULL,
    picked_at       TEXT    NOT NULL,
    result          TEXT,
    points          INTEGER,
    graded_at       TEXT,
    PRIMARY KEY (guild_id, user_id, bout_id)
);

CREATE INDEX IF NOT EXISTS idx_pickem_event ON pickem_picks (guild_id, espn_event_id);
CREATE INDEX IF NOT EXISTS idx_pickem_bout ON pickem_picks (bout_id);
CREATE INDEX IF NOT EXISTS idx_pickem_ungraded
    ON pickem_picks (locks_at) WHERE graded_at IS NULL;

-- Pick'em no longer posts results after each card.
DROP TABLE IF EXISTS pickem_results_posted;
"""

# Columns added after the first release; applied to existing databases on connect.
MIGRATIONS = {
    "channel_posts": (("signature", "TEXT"),),
    "guild_config": (
        ("predictions_channel_id", "INTEGER"),
        ("accuracy_channel_id", "INTEGER"),
        ("schedule_channel_id", "INTEGER"),
        ("tracking_since", "TEXT"),
        ("live_channel_id", "INTEGER"),
        ("pickem_channel_id", "INTEGER"),
        ("rankings_channel_id", "INTEGER"),
        ("rankings_include_women", "INTEGER NOT NULL DEFAULT 1"),
    ),
    "predictions": (
        ("odds_a", "INTEGER"),
        ("odds_b", "INTEGER"),
        ("method", "TEXT"),
        ("technique", "TEXT"),
        ("method_prob", "REAL"),
        ("detail_json", "TEXT"),
        ("result_method", "TEXT"),
        ("result_technique", "TEXT"),
        ("result_round", "INTEGER"),
        ("result_time", "TEXT"),
        ("method_correct", "INTEGER"),
        ("technique_correct", "INTEGER"),
    ),
}


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


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _parse_date(value: str | None) -> date | None:
    return date.fromisoformat(value) if value else None


def _bool_or_none(value) -> bool | None:
    return None if value is None else bool(value)


class Storage:
    def __init__(self, path: str) -> None:
        self._path = path
        self._db: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self._db = await aiosqlite.connect(self._path)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA foreign_keys=ON")
        await self._db.executescript(SCHEMA)
        await self._migrate()
        await self._db.commit()
        log.info("Database ready at %s", self._path)

    async def _migrate(self) -> None:
        for table, columns in MIGRATIONS.items():
            async with self.db.execute(f"PRAGMA table_info({table})") as cursor:
                existing = {row["name"] for row in await cursor.fetchall()}
            for column, kind in columns:
                if column not in existing:
                    await self.db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")
                    log.info("Added %s.%s", table, column)

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    @property
    def db(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("Storage.connect() has not been called")
        return self._db

    # -- guild settings -----------------------------------------------------

    async def get_settings(self, guild_id: int, defaults: GuildSettings | None = None) -> GuildSettings:
        """Settings for a guild, falling back to ``defaults`` when it has no row yet."""
        async with self.db.execute(
            "SELECT * FROM guild_config WHERE guild_id = ?", (guild_id,)
        ) as cursor:
            row = await cursor.fetchone()

        if row is None:
            base = defaults or GuildSettings(guild_id=guild_id)
            return replace(base, guild_id=guild_id)
        return _settings_from_row(row)

    async def save_settings(self, settings: GuildSettings) -> None:
        await self.db.execute(
            """
            INSERT INTO guild_config (
                guild_id, sync_enabled, days_ahead, duration_minutes,
                start_anchor, include_contender_series, announce_channel_id,
                predictions_channel_id, accuracy_channel_id, schedule_channel_id,
                tracking_since, live_channel_id, pickem_channel_id, rankings_channel_id,
                rankings_include_women, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(guild_id) DO UPDATE SET
                sync_enabled             = excluded.sync_enabled,
                days_ahead               = excluded.days_ahead,
                duration_minutes         = excluded.duration_minutes,
                start_anchor             = excluded.start_anchor,
                include_contender_series = excluded.include_contender_series,
                announce_channel_id      = excluded.announce_channel_id,
                predictions_channel_id   = excluded.predictions_channel_id,
                accuracy_channel_id      = excluded.accuracy_channel_id,
                schedule_channel_id      = excluded.schedule_channel_id,
                tracking_since           = excluded.tracking_since,
                live_channel_id          = excluded.live_channel_id,
                pickem_channel_id        = excluded.pickem_channel_id,
                rankings_channel_id      = excluded.rankings_channel_id,
                rankings_include_women   = excluded.rankings_include_women,
                updated_at               = excluded.updated_at
            """,
            (
                settings.guild_id,
                int(settings.sync_enabled),
                settings.days_ahead,
                settings.duration_minutes,
                settings.start_anchor,
                int(settings.include_contender_series),
                settings.announce_channel_id,
                settings.predictions_channel_id,
                settings.accuracy_channel_id,
                settings.schedule_channel_id,
                settings.tracking_since.isoformat() if settings.tracking_since else None,
                settings.live_channel_id,
                settings.pickem_channel_id,
                settings.rankings_channel_id,
                int(settings.rankings_include_women),
                datetime.now(UTC).isoformat(),
            ),
        )
        await self.db.commit()

    async def _guilds_where(self, clause: str) -> list[GuildSettings]:
        async with self.db.execute(f"SELECT * FROM guild_config WHERE {clause}") as cursor:
            rows = await cursor.fetchall()
        return [_settings_from_row(row) for row in rows]

    async def guilds_with_sync_enabled(self) -> list[GuildSettings]:
        return await self._guilds_where("sync_enabled = 1")

    async def guilds_with_channels(self) -> list[GuildSettings]:
        return await self._guilds_where(
            "predictions_channel_id IS NOT NULL OR accuracy_channel_id IS NOT NULL "
            "OR schedule_channel_id IS NOT NULL OR pickem_channel_id IS NOT NULL "
            "OR rankings_channel_id IS NOT NULL"
        )

    async def guilds_with_live(self) -> list[GuildSettings]:
        return await self._guilds_where("live_channel_id IS NOT NULL")

    # -- synced event links -------------------------------------------------

    async def get_links(self, guild_id: int) -> dict[str, tuple[int, str]]:
        """Map of UFC event id -> (Discord scheduled event id, content signature)."""
        async with self.db.execute(
            "SELECT espn_event_id, discord_event_id, signature FROM synced_events WHERE guild_id = ?",
            (guild_id,),
        ) as cursor:
            rows = await cursor.fetchall()
        return {r["espn_event_id"]: (r["discord_event_id"], r["signature"]) for r in rows}

    async def save_link(
        self, guild_id: int, espn_event_id: str, discord_event_id: int, signature: str
    ) -> None:
        await self.db.execute(
            """
            INSERT INTO synced_events (
                guild_id, espn_event_id, discord_event_id, signature, updated_at
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(guild_id, espn_event_id) DO UPDATE SET
                discord_event_id = excluded.discord_event_id,
                signature        = excluded.signature,
                updated_at       = excluded.updated_at
            """,
            (
                guild_id,
                espn_event_id,
                discord_event_id,
                signature,
                datetime.now(UTC).isoformat(),
            ),
        )
        await self.db.commit()

    async def delete_link(self, guild_id: int, espn_event_id: str) -> None:
        await self.db.execute(
            "DELETE FROM synced_events WHERE guild_id = ? AND espn_event_id = ?",
            (guild_id, espn_event_id),
        )
        await self.db.commit()

    # -- prediction ledger ----------------------------------------------------

    async def upsert_predictions(self, records: list[PredictionRecord]) -> None:
        """Write a card's picks in one go. Never touches a row already graded.

        A card is a dozen fights and this runs for every upcoming card on every
        pass, so they go in one transaction rather than a dozen: committing each
        row separately means a dozen flushes to disk for one board refresh.
        """
        if not records:
            return
        now = datetime.now(UTC).isoformat()
        await self.db.executemany(
            """
            INSERT INTO predictions (
                espn_event_id, bout_id, event_name, event_start,
                athlete_a, name_a, athlete_b, name_b, prob_a, weight_class, position,
                odds_a, odds_b, method, technique, method_prob, detail_json, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(espn_event_id, bout_id) DO UPDATE SET
                event_name   = excluded.event_name,
                event_start  = excluded.event_start,
                athlete_a    = excluded.athlete_a,
                name_a       = excluded.name_a,
                athlete_b    = excluded.athlete_b,
                name_b       = excluded.name_b,
                prob_a       = excluded.prob_a,
                weight_class = excluded.weight_class,
                position     = excluded.position,
                -- A line that disappears for a moment keeps its last known value.
                odds_a       = COALESCE(excluded.odds_a, predictions.odds_a),
                odds_b       = COALESCE(excluded.odds_b, predictions.odds_b),
                method       = excluded.method,
                technique    = excluded.technique,
                method_prob  = excluded.method_prob,
                detail_json  = excluded.detail_json,
                updated_at   = excluded.updated_at
            WHERE predictions.graded_at IS NULL
            """,
            [
                (
                    record.espn_event_id,
                    record.bout_id,
                    record.event_name,
                    record.event_start.isoformat(),
                    record.athlete_a,
                    record.name_a,
                    record.athlete_b,
                    record.name_b,
                    record.prob_a,
                    record.weight_class,
                    record.position,
                    record.odds_a,
                    record.odds_b,
                    record.method,
                    record.technique,
                    record.method_prob,
                    record.detail_json,
                    now,
                )
                for record in records
            ],
        )
        await self.db.commit()

    async def delete_predictions(self, espn_event_id: str, bout_ids: list[str]) -> None:
        """Drop picks for bouts that are no longer the fight they described."""
        if not bout_ids:
            return
        await self.db.executemany(
            "DELETE FROM predictions WHERE espn_event_id = ? AND bout_id = ? AND graded_at IS NULL",
            [(espn_event_id, bout_id) for bout_id in bout_ids],
        )
        await self.db.commit()

    async def predictions_for_event(self, espn_event_id: str) -> list[PredictionRecord]:
        async with self.db.execute(
            "SELECT * FROM predictions WHERE espn_event_id = ? ORDER BY position, rowid",
            (espn_event_id,),
        ) as cursor:
            rows = await cursor.fetchall()
        return [_prediction_from_row(row) for row in rows]

    async def prediction_for_bout(self, bout_id: str) -> PredictionRecord | None:
        async with self.db.execute("SELECT * FROM predictions WHERE bout_id = ?", (bout_id,)) as cursor:
            row = await cursor.fetchone()
        return _prediction_from_row(row) if row else None

    async def events_awaiting_grading(self, before: datetime) -> list[tuple[str, str, datetime]]:
        """(event id, name, start) for cards that started before ``before`` and have ungraded picks."""
        async with self.db.execute(
            """
            SELECT espn_event_id, MIN(event_name) AS event_name, MIN(event_start) AS event_start
            FROM predictions
            WHERE graded_at IS NULL AND event_start < ?
            GROUP BY espn_event_id
            ORDER BY event_start
            """,
            (before.isoformat(),),
        ) as cursor:
            rows = await cursor.fetchall()
        return [(r["espn_event_id"], r["event_name"], _parse_dt(r["event_start"])) for r in rows]

    async def grade_prediction(
        self,
        espn_event_id: str,
        bout_id: str,
        winner_athlete: str | None,
        correct: bool | None,
        *,
        result_method: str | None = None,
        result_technique: str | None = None,
        result_round: int | None = None,
        result_time: str | None = None,
        method_correct: bool | None = None,
        technique_correct: bool | None = None,
    ) -> None:
        await self.db.execute(
            """
            UPDATE predictions
            SET winner_athlete = ?, correct = ?, graded_at = ?,
                result_method = ?, result_technique = ?, result_round = ?, result_time = ?,
                method_correct = ?, technique_correct = ?
            WHERE espn_event_id = ? AND bout_id = ?
            """,
            (
                winner_athlete,
                None if correct is None else int(correct),
                datetime.now(UTC).isoformat(),
                result_method,
                result_technique,
                result_round,
                result_time,
                None if method_correct is None else int(method_correct),
                None if technique_correct is None else int(technique_correct),
                espn_event_id,
                bout_id,
            ),
        )
        await self.db.commit()

    async def graded_predictions(self, since: date | None = None) -> list[PredictionRecord]:
        """Every graded pick from ``since`` onward, oldest card first."""
        query = "SELECT * FROM predictions WHERE graded_at IS NOT NULL"
        params: tuple = ()
        if since is not None:
            query += " AND event_start >= ?"
            params = (datetime(since.year, since.month, since.day, tzinfo=UTC).isoformat(),)
        query += " ORDER BY event_start, espn_event_id, position, rowid"
        async with self.db.execute(query, params) as cursor:
            rows = await cursor.fetchall()
        return [_prediction_from_row(row) for row in rows]

    async def events_with_ungraded_predictions(self) -> set[str]:
        """Cards with at least one pick still waiting for a result."""
        async with self.db.execute(
            "SELECT DISTINCT espn_event_id FROM predictions WHERE graded_at IS NULL"
        ) as cursor:
            return {row["espn_event_id"] for row in await cursor.fetchall()}

    # -- the ratings boards as last published --------------------------------------

    async def ranking_state(self, division: str) -> dict[str, RankedState]:
        """Who was on this board last time, by fighter key."""
        async with self.db.execute(
            "SELECT fighter, rank, rating, last_fight FROM ranking_state WHERE division = ?",
            (division,),
        ) as cursor:
            rows = await cursor.fetchall()
        return {
            row["fighter"]: RankedState(
                row["fighter"], row["rank"], row["rating"], _parse_date(row["last_fight"])
            )
            for row in rows
        }

    async def save_ranking_state(self, division: str, entries: list[RankedState]) -> None:
        await self.db.execute("DELETE FROM ranking_state WHERE division = ?", (division,))
        await self.db.executemany(
            """
            INSERT INTO ranking_state (division, fighter, rank, rating, last_fight)
            VALUES (?, ?, ?, ?, ?)
            """,
            [
                (division, e.fighter, e.rank, e.rating, e.last_fight.isoformat() if e.last_fight else None)
                for e in entries
            ],
        )
        await self.db.commit()

    # -- the card as last seen ----------------------------------------------------

    async def card_bouts(self, espn_event_id: str) -> dict[str, CardBout]:
        """The fights last recorded for a card, by bout id. Empty when never seen."""
        async with self.db.execute(
            "SELECT bout_id, fighters_json, weight_class FROM card_bouts WHERE espn_event_id = ?",
            (espn_event_id,),
        ) as cursor:
            rows = await cursor.fetchall()
        bouts: dict[str, CardBout] = {}
        for row in rows:
            try:
                fighters = tuple((str(a), str(n)) for a, n in json.loads(row["fighters_json"]))
            except (TypeError, ValueError):
                continue
            bouts[row["bout_id"]] = CardBout(row["bout_id"], fighters, row["weight_class"])
        return bouts

    async def save_card_bouts(self, espn_event_id: str, bouts: list[CardBout]) -> None:
        """Replace what is on record for a card with what is on it now."""
        now = datetime.now(UTC).isoformat()
        await self.db.execute("DELETE FROM card_bouts WHERE espn_event_id = ?", (espn_event_id,))
        await self.db.executemany(
            """
            INSERT INTO card_bouts (espn_event_id, bout_id, fighters_json, weight_class, seen_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            [
                (espn_event_id, bout.bout_id, json.dumps([list(f) for f in bout.fighters]), bout.weight_class, now)
                for bout in bouts
            ],
        )
        await self.db.commit()

    async def prune_card_bouts(self, before: datetime) -> int:
        """Forget cards nothing has refreshed since ``before``; they have happened."""
        cursor = await self.db.execute("DELETE FROM card_bouts WHERE seen_at < ?", (before.isoformat(),))
        await self.db.commit()
        return cursor.rowcount

    # -- channel posts ------------------------------------------------------------

    async def get_post(self, guild_id: int, kind: str, key: str) -> Post | None:
        async with self.db.execute(
            "SELECT channel_id, message_id, signature FROM channel_posts WHERE guild_id = ? AND kind = ? AND key = ?",
            (guild_id, kind, key),
        ) as cursor:
            row = await cursor.fetchone()
        return Post(row["channel_id"], row["message_id"], row["signature"]) if row else None

    async def save_post(
        self,
        guild_id: int,
        kind: str,
        key: str,
        channel_id: int,
        message_id: int,
        signature: str | None = None,
    ) -> None:
        await self.db.execute(
            """
            INSERT INTO channel_posts (guild_id, kind, key, channel_id, message_id, signature, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(guild_id, kind, key) DO UPDATE SET
                channel_id = excluded.channel_id,
                message_id = excluded.message_id,
                signature  = excluded.signature,
                updated_at = excluded.updated_at
            """,
            (guild_id, kind, key, channel_id, message_id, signature, datetime.now(UTC).isoformat()),
        )
        await self.db.commit()

    async def delete_post(self, guild_id: int, kind: str, key: str) -> None:
        await self.db.execute(
            "DELETE FROM channel_posts WHERE guild_id = ? AND kind = ? AND key = ?",
            (guild_id, kind, key),
        )
        await self.db.commit()

    async def posts_of_kind(self, guild_id: int, kind: str) -> dict[str, Post]:
        async with self.db.execute(
            "SELECT key, channel_id, message_id, signature FROM channel_posts WHERE guild_id = ? AND kind = ?",
            (guild_id, kind),
        ) as cursor:
            rows = await cursor.fetchall()
        return {r["key"]: Post(r["channel_id"], r["message_id"], r["signature"]) for r in rows}

    async def recap_posted(self, guild_id: int, espn_event_id: str) -> bool:
        async with self.db.execute(
            "SELECT 1 FROM recaps_posted WHERE guild_id = ? AND espn_event_id = ?",
            (guild_id, espn_event_id),
        ) as cursor:
            return await cursor.fetchone() is not None

    async def mark_recap_posted(self, guild_id: int, espn_event_id: str) -> None:
        await self.db.execute(
            "INSERT OR IGNORE INTO recaps_posted (guild_id, espn_event_id, posted_at) VALUES (?, ?, ?)",
            (guild_id, espn_event_id, datetime.now(UTC).isoformat()),
        )
        await self.db.commit()

    # -- live coverage ------------------------------------------------------------

    async def live_posted_many(self, bout_ids: list[str]) -> dict[str, set[str]]:
        """Which updates have gone out for each of these bouts, in one query."""
        done: dict[str, set[str]] = {bout_id: set() for bout_id in bout_ids}
        if not bout_ids:
            return done
        placeholders = ",".join("?" * len(bout_ids))
        async with self.db.execute(
            f"SELECT bout_id, key FROM live_posts WHERE bout_id IN ({placeholders})", tuple(bout_ids)
        ) as cursor:
            for row in await cursor.fetchall():
                done[row["bout_id"]].add(row["key"])
        return done

    async def live_posted(self, bout_id: str) -> set[str]:
        async with self.db.execute("SELECT key FROM live_posts WHERE bout_id = ?", (bout_id,)) as cursor:
            return {row["key"] for row in await cursor.fetchall()}

    async def mark_live_posted(self, bout_id: str, key: str) -> None:
        await self.db.execute(
            "INSERT OR IGNORE INTO live_posts (bout_id, key, posted_at) VALUES (?, ?, ?)",
            (bout_id, key, datetime.now(UTC).isoformat()),
        )
        await self.db.commit()

    async def save_live_snapshot(self, bout_id: str, round_number: int, stats: dict) -> None:
        await self.db.execute(
            """
            INSERT INTO live_snapshots (bout_id, round, stats_json) VALUES (?, ?, ?)
            ON CONFLICT(bout_id, round) DO UPDATE SET stats_json = excluded.stats_json
            """,
            (bout_id, round_number, json.dumps(stats)),
        )
        await self.db.commit()

    async def live_snapshot(self, bout_id: str, round_number: int) -> dict | None:
        async with self.db.execute(
            "SELECT stats_json FROM live_snapshots WHERE bout_id = ? AND round = ?",
            (bout_id, round_number),
        ) as cursor:
            row = await cursor.fetchone()
        return json.loads(row["stats_json"]) if row else None

    # -- pick'em ------------------------------------------------------------------

    async def save_pickem_pick(self, pick: PickemRecord) -> None:
        """Create or change a member's pick. A graded pick never changes."""
        await self.db.execute(
            """
            INSERT INTO pickem_picks (
                guild_id, user_id, espn_event_id, bout_id, event_name, event_start,
                athlete_id, athlete_name, opponent_id, opponent_name, odds, points_if_right,
                locks_at, picked_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(guild_id, user_id, bout_id) DO UPDATE SET
                event_name      = excluded.event_name,
                event_start     = excluded.event_start,
                athlete_id      = excluded.athlete_id,
                athlete_name    = excluded.athlete_name,
                opponent_id     = excluded.opponent_id,
                opponent_name   = excluded.opponent_name,
                odds            = excluded.odds,
                points_if_right = excluded.points_if_right,
                locks_at        = excluded.locks_at,
                picked_at       = excluded.picked_at
            WHERE pickem_picks.graded_at IS NULL
            """,
            (
                pick.guild_id,
                pick.user_id,
                pick.espn_event_id,
                pick.bout_id,
                pick.event_name,
                pick.event_start.isoformat(),
                pick.athlete_id,
                pick.athlete_name,
                pick.opponent_id,
                pick.opponent_name,
                pick.odds,
                pick.points_if_right,
                pick.locks_at.isoformat(),
                pick.picked_at.isoformat(),
            ),
        )
        await self.db.commit()

    async def pickem_user_picks(self, guild_id: int, user_id: int, espn_event_id: str) -> list[PickemRecord]:
        async with self.db.execute(
            """
            SELECT * FROM pickem_picks
            WHERE guild_id = ? AND user_id = ? AND espn_event_id = ?
            ORDER BY locks_at DESC, rowid
            """,
            (guild_id, user_id, espn_event_id),
        ) as cursor:
            rows = await cursor.fetchall()
        return [_pickem_from_row(row) for row in rows]

    async def record_short_notice(self, rows: list[ShortNotice]) -> None:
        """Remember replacements. A fighter already recorded for a bout is left alone,
        so the first sighting is the one kept and re-reading a card changes nothing."""
        await self.db.executemany(
            """
            INSERT OR IGNORE INTO short_notice
                (espn_event_id, bout_id, arrived, departed, opponent, weight_class,
                 event_start, noticed_at, days_notice)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    row.espn_event_id, row.bout_id, row.arrived, row.departed, row.opponent,
                    row.weight_class, row.event_start.isoformat(), row.noticed_at.isoformat(),
                    row.days_notice,
                )
                for row in rows
            ],
        )
        await self.db.commit()

    async def short_notice_for(self, espn_event_id: str) -> list[ShortNotice]:
        """Replacements recorded on one card, least notice first."""
        async with self.db.execute(
            "SELECT * FROM short_notice WHERE espn_event_id = ? ORDER BY days_notice, bout_id",
            (espn_event_id,),
        ) as cursor:
            rows = await cursor.fetchall()
        return [
            ShortNotice(
                espn_event_id=row["espn_event_id"],
                bout_id=row["bout_id"],
                arrived=row["arrived"],
                departed=row["departed"],
                opponent=row["opponent"],
                weight_class=row["weight_class"],
                event_start=datetime.fromisoformat(row["event_start"]),
                noticed_at=datetime.fromisoformat(row["noticed_at"]),
                days_notice=row["days_notice"],
            )
            for row in rows
        ]

    async def notice_due(self, kind: str, subject: str) -> bool:
        """Whether this is the first time the bot has had to say this.

        Records it as said, so a warning that repeats every pass is posted once.
        """
        async with self.db.execute(
            "SELECT 1 FROM notices_sent WHERE kind = ? AND subject = ?", (kind, subject)
        ) as cursor:
            if await cursor.fetchone() is not None:
                return False
        await self.db.execute(
            "INSERT INTO notices_sent (kind, subject, sent_at) VALUES (?, ?, ?)",
            (kind, subject, datetime.now(UTC).isoformat()),
        )
        await self.db.commit()
        return True

    async def pickem_card_picks(self, guild_id: int, espn_event_id: str) -> list[PickemRecord]:
        """Every member's picks for one card, in the order the fights are fought."""
        async with self.db.execute(
            """
            SELECT * FROM pickem_picks
            WHERE guild_id = ? AND espn_event_id = ?
            ORDER BY locks_at DESC, bout_id, user_id
            """,
            (guild_id, espn_event_id),
        ) as cursor:
            rows = await cursor.fetchall()
        return [_pickem_from_row(row) for row in rows]

    async def pickem_counts(self, guild_id: int, espn_event_id: str) -> dict[str, dict[str, int]]:
        """Bout id -> fighter id -> number of members who picked them.

        Void picks are left out. They are picks on a fighter who was replaced, and
        counting them would show a share of the room behind someone who is not in
        the fight.
        """
        async with self.db.execute(
            """
            SELECT bout_id, athlete_id, COUNT(*) AS n FROM pickem_picks
            WHERE guild_id = ? AND espn_event_id = ? AND (result IS NULL OR result != 'void')
            GROUP BY bout_id, athlete_id
            """,
            (guild_id, espn_event_id),
        ) as cursor:
            rows = await cursor.fetchall()
        counts: dict[str, dict[str, int]] = {}
        for row in rows:
            counts.setdefault(row["bout_id"], {})[row["athlete_id"]] = row["n"]
        return counts

    async def pickem_player_count(self, guild_id: int, espn_event_id: str) -> int:
        async with self.db.execute(
            "SELECT COUNT(DISTINCT user_id) AS n FROM pickem_picks WHERE guild_id = ? AND espn_event_id = ?",
            (guild_id, espn_event_id),
        ) as cursor:
            row = await cursor.fetchone()
        return row["n"]

    async def pickem_events_awaiting_grading(self, now: datetime) -> list[str]:
        """Cards with ungraded picks on bouts that have already locked."""
        async with self.db.execute(
            "SELECT DISTINCT espn_event_id FROM pickem_picks WHERE graded_at IS NULL AND locks_at < ?",
            (now.isoformat(),),
        ) as cursor:
            rows = await cursor.fetchall()
        return [row["espn_event_id"] for row in rows]

    async def pickem_ungraded_bouts(self, espn_event_id: str) -> list[tuple[str, datetime]]:
        async with self.db.execute(
            """
            SELECT bout_id, MIN(locks_at) AS locks_at FROM pickem_picks
            WHERE espn_event_id = ? AND graded_at IS NULL
            GROUP BY bout_id
            """,
            (espn_event_id,),
        ) as cursor:
            rows = await cursor.fetchall()
        return [(row["bout_id"], _parse_dt(row["locks_at"])) for row in rows]

    async def void_pickem_picks(self, bout_id: str, *, fighters: set[str] | None = None) -> int:
        """Void ungraded picks on a bout, so they score nothing and stop pending.

        With ``fighters``, only picks on someone no longer in the bout are voided,
        which is a replacement. Without it every pick on the bout is, which is a
        fight coming off the card.
        """
        clause = ""
        params: list = [datetime.now(UTC).isoformat(), bout_id]
        if fighters:
            clause = f" AND athlete_id NOT IN ({','.join('?' * len(fighters))})"
            params += sorted(fighters)
        cursor = await self.db.execute(
            "UPDATE pickem_picks SET result = 'void', points = 0, graded_at = ? "
            f"WHERE bout_id = ? AND graded_at IS NULL{clause}",
            params,
        )
        await self.db.commit()
        return cursor.rowcount

    async def grade_pickem_bout(
        self,
        bout_id: str,
        winner_athlete: str | None,
        *,
        loss_points: int,
        fighters: set[str] | None = None,
    ) -> int:
        """Score every pick on a bout. No winner, or a winner nobody picked against, is void.

        ``fighters`` is who actually fought. A pick on someone who was replaced
        before the bell is void too: that fight never happened, so it can be
        neither right nor wrong.
        """
        graded = await self.void_pickem_picks(bout_id, fighters=fighters) if fighters else 0

        cursor = await self.db.execute(
            """
            UPDATE pickem_picks SET
                result = CASE
                    WHEN :winner IS NULL THEN 'void'
                    WHEN athlete_id = :winner THEN 'win'
                    WHEN opponent_id = :winner THEN 'loss'
                    ELSE 'void'
                END,
                points = CASE
                    WHEN :winner IS NULL THEN 0
                    WHEN athlete_id = :winner THEN points_if_right
                    WHEN opponent_id = :winner THEN :loss
                    ELSE 0
                END,
                graded_at = :now
            WHERE bout_id = :bout AND graded_at IS NULL
            """,
            {"winner": winner_athlete, "loss": loss_points, "bout": bout_id, "now": datetime.now(UTC).isoformat()},
        )
        await self.db.commit()
        return graded + cursor.rowcount

    async def pickem_leaderboard(
        self, guild_id: int, espn_event_id: str | None = None
    ) -> list[PickemStanding]:
        """Every member with a settled pick, highest points first.

        With an event, only that card counts, which is the same standings read
        over one night instead of all of them.
        """
        async with self.db.execute(
            """
            SELECT user_id,
                   COALESCE(SUM(points), 0) AS points,
                   SUM(CASE WHEN result = 'win' THEN 1 ELSE 0 END) AS wins,
                   SUM(CASE WHEN result = 'loss' THEN 1 ELSE 0 END) AS losses,
                   COUNT(DISTINCT espn_event_id) AS cards
            FROM pickem_picks
            WHERE guild_id = ? AND result IN ('win', 'loss')
              AND (? IS NULL OR espn_event_id = ?)
            GROUP BY user_id
            ORDER BY points DESC, wins DESC, losses ASC, user_id
            """,
            (guild_id, espn_event_id, espn_event_id),
        ) as cursor:
            rows = await cursor.fetchall()
        return [
            PickemStanding(
                user_id=row["user_id"], points=row["points"], wins=row["wins"], losses=row["losses"], cards=row["cards"]
            )
            for row in rows
        ]

    async def pickem_user_summary(self, guild_id: int, user_id: int) -> PickemSummary:
        async with self.db.execute(
            """
            SELECT COALESCE(SUM(points), 0) AS points,
                   COALESCE(SUM(CASE WHEN result = 'win' THEN 1 ELSE 0 END), 0) AS wins,
                   COALESCE(SUM(CASE WHEN result = 'loss' THEN 1 ELSE 0 END), 0) AS losses,
                   COALESCE(SUM(CASE WHEN result = 'void' THEN 1 ELSE 0 END), 0) AS voids,
                   COALESCE(SUM(CASE WHEN graded_at IS NULL THEN 1 ELSE 0 END), 0) AS pending,
                   COALESCE(SUM(CASE WHEN result = 'win' AND odds > 0 THEN 1 ELSE 0 END), 0) AS underdog_wins
            FROM pickem_picks WHERE guild_id = ? AND user_id = ?
            """,
            (guild_id, user_id),
        ) as cursor:
            row = await cursor.fetchone()
        summary = PickemSummary(
            user_id=user_id,
            points=row["points"],
            wins=row["wins"],
            losses=row["losses"],
            voids=row["voids"],
            pending=row["pending"],
            underdog_wins=row["underdog_wins"],
        )

        async with self.db.execute(
            """
            SELECT points, athlete_name FROM pickem_picks
            WHERE guild_id = ? AND user_id = ? AND result = 'win'
            ORDER BY points DESC LIMIT 1
            """,
            (guild_id, user_id),
        ) as cursor:
            best = await cursor.fetchone()
        if best:
            summary.best_hit, summary.best_hit_name = best["points"], best["athlete_name"]

        standings = await self.pickem_leaderboard(guild_id)
        summary.players = len(standings)
        summary.rank = next((i for i, s in enumerate(standings, 1) if s.user_id == user_id), None)
        return summary

    async def pickem_user_cards(self, guild_id: int, user_id: int, limit: int = 10) -> list[PickemCard]:
        async with self.db.execute(
            """
            SELECT espn_event_id,
                   MIN(event_name) AS event_name,
                   MIN(event_start) AS event_start,
                   COUNT(*) AS picks,
                   COALESCE(SUM(CASE WHEN result = 'win' THEN 1 ELSE 0 END), 0) AS wins,
                   COALESCE(SUM(CASE WHEN result = 'loss' THEN 1 ELSE 0 END), 0) AS losses,
                   COALESCE(SUM(CASE WHEN graded_at IS NULL THEN 1 ELSE 0 END), 0) AS pending,
                   COALESCE(SUM(points), 0) AS points
            FROM pickem_picks WHERE guild_id = ? AND user_id = ?
            GROUP BY espn_event_id
            ORDER BY event_start DESC
            LIMIT ?
            """,
            (guild_id, user_id, limit),
        ) as cursor:
            rows = await cursor.fetchall()
        return [
            PickemCard(
                espn_event_id=row["espn_event_id"],
                event_name=row["event_name"],
                event_start=_parse_dt(row["event_start"]),
                picks=row["picks"],
                wins=row["wins"],
                losses=row["losses"],
                pending=row["pending"],
                points=row["points"],
            )
            for row in rows
        ]

    async def pickem_event_names(self, guild_id: int, limit: int = 25) -> list[str]:
        """Names of cards this guild has played, most recent first, for autocomplete."""
        async with self.db.execute(
            """
            SELECT event_name, MAX(event_start) AS latest FROM pickem_picks
            WHERE guild_id = ? GROUP BY event_name ORDER BY latest DESC LIMIT ?
            """,
            (guild_id, limit),
        ) as cursor:
            rows = await cursor.fetchall()
        return [row["event_name"] for row in rows]

    async def pickem_last_scored_card(self, guild_id: int) -> tuple[str, str, datetime] | None:
        """The most recent card anyone has a settled pick on: (id, name, start).

        While a card is being fought this is that card, from the moment its first
        fight is graded. Between cards it is the one that just finished.
        """
        async with self.db.execute(
            """
            SELECT espn_event_id, event_name, MAX(event_start) AS event_start
            FROM pickem_picks
            WHERE guild_id = ? AND result IN ('win', 'loss')
            GROUP BY espn_event_id
            ORDER BY event_start DESC
            LIMIT 1
            """,
            (guild_id,),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None
        return row["espn_event_id"], row["event_name"], datetime.fromisoformat(row["event_start"])

    async def pickem_unscored_count(self, guild_id: int, espn_event_id: str) -> int:
        async with self.db.execute(
            "SELECT COUNT(*) AS n FROM pickem_picks WHERE guild_id = ? AND espn_event_id = ? AND graded_at IS NULL",
            (guild_id, espn_event_id),
        ) as cursor:
            row = await cursor.fetchone()
        return row["n"]

    async def pickem_open_picks(self) -> list[PickemRecord]:
        async with self.db.execute("SELECT * FROM pickem_picks WHERE graded_at IS NULL") as cursor:
            rows = await cursor.fetchall()
        return [_pickem_from_row(row) for row in rows]

    async def set_pickem_points(self, changes: list[tuple[int, int, int, str]]) -> int:
        """Apply (points_if_right, guild id, user id, bout id) to unsettled picks."""
        if not changes:
            return 0
        await self.db.executemany(
            """
            UPDATE pickem_picks SET points_if_right = ?
            WHERE guild_id = ? AND user_id = ? AND bout_id = ? AND graded_at IS NULL
            """,
            changes,
        )
        await self.db.commit()
        return len(changes)

    async def set_pickem_loss_points(self, points: int) -> int:
        """Make every settled wrong pick score ``points``."""
        cursor = await self.db.execute(
            "UPDATE pickem_picks SET points = ? WHERE result = 'loss' AND points IS NOT ?", (points, points)
        )
        await self.db.commit()
        return cursor.rowcount

    # -- housekeeping -------------------------------------------------------------

    async def prune_live_data(self, before: datetime) -> int:
        """Forget live coverage bookkeeping for cards that are long over.

        Both tables exist to stop a restart mid-card repeating posts, so they are
        of no use once the card is history. Snapshots go first: they are found
        through the posts that are about to be deleted.
        """
        cutoff = before.isoformat()
        await self.db.execute(
            """
            DELETE FROM live_snapshots WHERE bout_id IN (
                SELECT bout_id FROM live_posts WHERE posted_at < ?
            )
            """,
            (cutoff,),
        )
        cursor = await self.db.execute("DELETE FROM live_posts WHERE posted_at < ?", (cutoff,))
        await self.db.commit()
        return cursor.rowcount

    async def delete_guild(self, guild_id: int) -> None:
        for table in (
            "synced_events",
            "channel_posts",
            "recaps_posted",
            "pickem_picks",
            "guild_config",
        ):
            await self.db.execute(f"DELETE FROM {table} WHERE guild_id = ?", (guild_id,))
        await self.db.commit()


def _settings_from_row(row: aiosqlite.Row) -> GuildSettings:
    return GuildSettings(
        guild_id=row["guild_id"],
        sync_enabled=bool(row["sync_enabled"]),
        days_ahead=row["days_ahead"],
        duration_minutes=row["duration_minutes"],
        start_anchor=row["start_anchor"],
        include_contender_series=bool(row["include_contender_series"]),
        announce_channel_id=row["announce_channel_id"],
        predictions_channel_id=row["predictions_channel_id"],
        accuracy_channel_id=row["accuracy_channel_id"],
        schedule_channel_id=row["schedule_channel_id"],
        tracking_since=_parse_date(row["tracking_since"]),
        live_channel_id=row["live_channel_id"],
        pickem_channel_id=row["pickem_channel_id"],
        rankings_channel_id=row["rankings_channel_id"],
        rankings_include_women=bool(row["rankings_include_women"]),
    )


def _pickem_from_row(row: aiosqlite.Row) -> PickemRecord:
    return PickemRecord(
        guild_id=row["guild_id"],
        user_id=row["user_id"],
        espn_event_id=row["espn_event_id"],
        bout_id=row["bout_id"],
        event_name=row["event_name"],
        event_start=_parse_dt(row["event_start"]),
        athlete_id=row["athlete_id"],
        athlete_name=row["athlete_name"],
        opponent_id=row["opponent_id"],
        opponent_name=row["opponent_name"],
        odds=row["odds"],
        points_if_right=row["points_if_right"],
        locks_at=_parse_dt(row["locks_at"]),
        picked_at=_parse_dt(row["picked_at"]),
        result=row["result"],
        points=row["points"],
        graded_at=_parse_dt(row["graded_at"]),
    )


def _prediction_from_row(row: aiosqlite.Row) -> PredictionRecord:
    return PredictionRecord(
        espn_event_id=row["espn_event_id"],
        bout_id=row["bout_id"],
        event_name=row["event_name"],
        event_start=_parse_dt(row["event_start"]),
        athlete_a=row["athlete_a"],
        name_a=row["name_a"],
        athlete_b=row["athlete_b"],
        name_b=row["name_b"],
        prob_a=row["prob_a"],
        weight_class=row["weight_class"],
        position=row["position"],
        odds_a=row["odds_a"],
        odds_b=row["odds_b"],
        method=row["method"],
        technique=row["technique"],
        method_prob=row["method_prob"],
        detail_json=row["detail_json"],
        winner_athlete=row["winner_athlete"],
        correct=_bool_or_none(row["correct"]),
        graded_at=_parse_dt(row["graded_at"]),
        result_method=row["result_method"],
        result_technique=row["result_technique"],
        result_round=row["result_round"],
        result_time=row["result_time"],
        method_correct=_bool_or_none(row["method_correct"]),
        technique_correct=_bool_or_none(row["technique_correct"]),
    )
