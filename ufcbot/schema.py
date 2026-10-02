"""The database as it is declared: every table, index and added column.

Apart from ``storage.py`` nothing in the bot writes SQL, and this is the half
of it that is read for a different reason than the queries are -- to see what
is stored, or to add a column -- so it is kept where that can be done without
scrolling past a thousand lines of query.

``MIGRATIONS`` is how an existing database catches up: every column added after
the first release is listed against its table and applied on connect. Columns
are only ever added, never dropped, so a database that has been through an
older version keeps whatever that version gave it.
"""

from __future__ import annotations

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

-- Cards whose recap has already been posted to a guild's picks channel.
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
    "ranking_state": (("raw", "INTEGER NOT NULL DEFAULT 0"),),
    "guild_config": (
        ("predictions_channel_id", "INTEGER"),
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
