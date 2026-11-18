"""The picks board during a card: a result lands, and the board says so.

The bug this pins down was in the wiring rather than in any one piece: live
coverage posted a result and the boards were redrawn straight away, but the
model's picks were only graded on the hourly pass, and only for cards two hours
old. The board is drawn from what is graded, so the redraw came out the same as
before and was skipped as unchanged. The test runs the refresh the live loop
runs, on a card half an hour old.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import discord
from conftest import bout, card, fighter

from ufcbot.cogs.jobs import BOARD_RETRIES, JobsCog
from ufcbot.features import channels as channels_module
from ufcbot.features.channels import KIND_PICKS, ChannelPublisher
from ufcbot.features.tracking import PredictionTracker
from ufcbot.records import GuildSettings, PredictionRecord

ALLEN = fighter("1", "Arnold Allen")
PICO = fighter("2", "Aaron Pico")
CARD_ID = "600061500"
NO_BREAK = "\N{NO-BREAK SPACE}"


class BoardChannel:
    def __init__(self) -> None:
        self.id, self.name = 501, "picks"
        self.sent: list[discord.Embed] = []
        self.edits: list[tuple[int, discord.Embed]] = []
        self.refuse: discord.HTTPException | None = None

    async def send(self, **kwargs):
        self.sent.append(kwargs["embed"])
        return SimpleNamespace(id=7000 + len(self.sent))

    def get_partial_message(self, message_id: int):
        channel = self

        class Partial:
            id = message_id

            async def edit(self, **kwargs) -> None:
                if channel.refuse is not None:
                    raise channel.refuse
                channel.edits.append((message_id, kwargs["embed"]))

            async def delete(self) -> None:
                return None

        return Partial()


class CardUnderWay:
    """ESPN, on a card half an hour old whose first fight is about to end."""

    def __init__(self) -> None:
        self.fight = bout("B1", ALLEN, PICO)
        self.event = card(self.fight, start=datetime.now(UTC) - timedelta(minutes=30), event_id=CARD_ID)
        self.method: str | None = None

    def finish(self, *, method: str | None) -> None:
        self.fight.completed, self.fight.winner_id = True, ALLEN.id
        self.method = method

    async def upcoming_events(self, **kwargs):
        return [self.event]

    async def recent_events(self, **kwargs):
        return [self.event]

    async def get_event(self, event_id, **kwargs):
        return self.event if event_id == CARD_ID else None

    async def load_status(self, fight, **kwargs):
        if fight.completed:
            fight.state, fight.period, fight.clock, fight.result_method = "post", 2, "3:41", self.method

    async def load_odds(self, event, **kwargs):
        return None


class MidCardBot:
    """The parts of the bot the live refresh reaches for, the database real."""

    def __init__(self, storage, espn: CardUnderWay, channel: BoardChannel) -> None:
        self.storage = storage
        self.tracker = PredictionTracker(storage, espn)
        self.publisher = ChannelPublisher(
            espn, storage, self.tracker, pickem=None,
            pick_provider=lambda event: {}, evaluation_provider=lambda: None,
        )
        self.publisher._channel = lambda guild, channel_id: channel if channel_id == channel.id else None
        self.pickem = SimpleNamespace(grade_due=self._nothing)
        self.config = SimpleNamespace(backup_dir="")
        self.guild = SimpleNamespace(id=1, me=SimpleNamespace(id=99))

    @staticmethod
    async def _nothing() -> int:
        return 0

    def get_guild(self, guild_id: int):
        return self.guild


async def a_card_under_way(storage, monkeypatch):
    monkeypatch.setattr(channels_module, "WRITE_SPACING", 0)
    espn, channel = CardUnderWay(), BoardChannel()
    await storage.save_settings(GuildSettings(guild_id=1, predictions_channel_id=channel.id))
    # The pick on record from before the card, Allen at 62%.
    await storage.upsert_predictions([
        PredictionRecord(
            espn_event_id=CARD_ID, bout_id="B1", event_name="UFC 333", event_start=espn.event.start,
            athlete_a=ALLEN.id, name_a=ALLEN.display_name, athlete_b=PICO.id, name_b=PICO.display_name,
            prob_a=0.62, method="ko",
        )
    ])
    jobs = JobsCog(MidCardBot(storage, espn, channel))
    await jobs._publish_boards()  # the board as the card starts, nothing settled
    return espn, channel, jobs


def board_edit(channel: BoardChannel, storage_post) -> str | None:
    """The board's text as last edited, or None if it never was. Names and marks
    are joined with non-breaking spaces on the board, so they are read as spaces."""
    edits = [embed for message_id, embed in channel.edits if message_id == storage_post.message_id]
    if not edits:
        return None
    embed = edits[-1]
    text = "\n".join([embed.description or "", *(f.value for f in embed.fields)])
    return text.replace(NO_BREAK, " ")


async def test_a_result_mid_card_marks_the_pick_on_the_board(storage, monkeypatch):
    espn, channel, jobs = await a_card_under_way(storage, monkeypatch)
    post = await storage.get_post(1, KIND_PICKS, CARD_ID)

    espn.finish(method="kotko")
    await jobs._refresh_after_live()

    edited = board_edit(channel, post)
    assert edited is not None, "the board was redrawn the moment the result posted"
    assert "Picks: **1/1** correct" in edited, "and the card's running record moved with it"
    assert "Pick ✅" in edited and "Method ✅" in edited


async def test_a_winner_without_a_method_waits_for_one(storage, monkeypatch):
    """ESPN names the winner before it says how. Graded in that gap, the pick
    would keep a blank method for good, so the board waits a pass."""
    espn, channel, jobs = await a_card_under_way(storage, monkeypatch)
    post = await storage.get_post(1, KIND_PICKS, CARD_ID)

    espn.finish(method=None)
    await jobs._refresh_after_live()
    assert board_edit(channel, post) is None
    assert (await storage.predictions_for_event(CARD_ID))[0].graded_at is None

    espn.method = "kotko"
    jobs._last_live_refresh = None  # past the cooldown
    await jobs._refresh_after_live()
    assert "Method ✅" in board_edit(channel, post)


async def test_an_edit_discord_refuses_is_logged_and_tried_again(storage, monkeypatch, caplog):
    espn, channel, jobs = await a_card_under_way(storage, monkeypatch)
    post = await storage.get_post(1, KIND_PICKS, CARD_ID)
    response = SimpleNamespace(status=503, reason="Service Unavailable", headers={})
    channel.refuse = discord.HTTPException(response, {"code": 0, "message": "upstream connect error"})

    espn.finish(method="kotko")
    await jobs._refresh_after_live()
    assert "HTTP 503" in caplog.text and "upstream connect error" in caplog.text, "the reason is in the log"
    assert jobs._boards_stale, "and the refresh is due again, not left for the hour"

    channel.refuse = None
    jobs._last_live_refresh = None
    await jobs._refresh_after_live()
    assert "Pick ✅" in board_edit(channel, post)
    assert not jobs._boards_stale


async def test_a_refusal_that_never_clears_stops_being_retried(storage, monkeypatch):
    espn, channel, jobs = await a_card_under_way(storage, monkeypatch)
    response = SimpleNamespace(status=403, reason="Forbidden", headers={})
    channel.refuse = discord.HTTPException(response, {"code": 50013, "message": "Missing Permissions"})
    espn.finish(method="kotko")

    for _ in range(BOARD_RETRIES + 1):
        jobs._last_live_refresh = None
        await jobs._refresh_after_live()
    assert not jobs._boards_stale, "left for the hourly pass rather than retried forever"
