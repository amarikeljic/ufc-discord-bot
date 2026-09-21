"""The publisher: what it edits, what it re-sends, and what it deletes.

This is the module that writes in people's channels, so the behaviour worth
pinning down is mostly about restraint — not rewriting a board that has not
changed, not leaving two of the same board behind when Discord refuses an edit,
and not deleting a message it does not own.

Discord is faked rather than mocked: a channel that records what it was asked to
do, and can be told to fail. The database is real.
"""

from __future__ import annotations

from datetime import timedelta

import discord
import pytest
from conftest import bout, card, fighter

from ufcbot.features import channels as channels_module
from ufcbot.features.channels import (
    BOARD_KEY,
    KIND_PICKS,
    ChannelPublisher,
    PublishResult,
)
from ufcbot.features.pickem import ready_to_open

ALLEN = fighter("1", "Arnold Allen")
PICO = fighter("2", "Aaron Pico")


@pytest.fixture(autouse=True)
def no_write_spacing(monkeypatch):
    """The real publisher paces itself past Discord's edit limit; tests need not wait."""
    monkeypatch.setattr(channels_module, "WRITE_SPACING", 0)


class FakeMessage:
    def __init__(self, channel: FakeChannel, message_id: int) -> None:
        self.channel, self.id = channel, message_id

    async def edit(self, **kwargs) -> None:
        if self.channel.fail_with is not None:
            raise self.channel.fail_with
        self.channel.edited.append(self.id)

    async def delete(self) -> None:
        if self.channel.fail_with is not None:
            raise self.channel.fail_with
        self.channel.deleted.append(self.id)


class FakeChannel:
    """Records what the publisher asked Discord to do, and can refuse."""

    def __init__(self, channel_id: int = 500) -> None:
        self.id, self.name = channel_id, "boards"
        self.sent: list[int] = []
        self.edited: list[int] = []
        self.deleted: list[int] = []
        self.fail_with: Exception | None = None
        self._next_id = 1000

    async def send(self, **kwargs) -> FakeMessage:
        self._next_id += 1
        self.sent.append(self._next_id)
        return FakeMessage(self, self._next_id)

    def get_partial_message(self, message_id: int) -> FakeMessage:
        return FakeMessage(self, message_id)


class FakeGuild:
    def __init__(self, guild_id: int = 1) -> None:
        self.id = guild_id
        self.me = type("Me", (), {"id": 99})()


def http_error(status: int) -> discord.HTTPException:
    response = type("Response", (), {"status": status, "reason": "", "headers": {}})()
    return discord.HTTPException(response, {"code": 0, "message": "no"})


def publisher(storage) -> ChannelPublisher:
    """A publisher wired to nothing but the database; the boards under test build their own embeds."""
    return ChannelPublisher(
        data=None,
        storage=storage,
        tracker=None,
        pickem=None,
        pick_provider=lambda event: {},
        evaluation_provider=lambda: None,
    )


def an_embed(text: str = "first") -> discord.Embed:
    return discord.Embed(title="Board", description=text)


async def upsert(pub, guild, channel, embed, result, **kwargs) -> bool:
    return await pub._upsert(guild, channel, KIND_PICKS, BOARD_KEY, embed, result, **kwargs)


# -- editing in place ------------------------------------------------------------


async def test_a_board_nobody_has_changed_is_not_rewritten(storage):
    """The point of the content signature. Rewriting every pass would burn the
    rate limit and make "Last updated" mean "last refreshed"."""
    pub, guild, channel, result = publisher(storage), FakeGuild(), FakeChannel(), PublishResult()

    await upsert(pub, guild, channel, an_embed(), result)
    assert len(channel.sent) == 1

    await upsert(pub, guild, channel, an_embed(), result)

    assert channel.edited == [] and len(channel.sent) == 1
    assert result.unchanged == 1 and result.updated == 1


async def test_a_board_that_changed_is_edited_not_posted_again(storage):
    pub, guild, channel, result = publisher(storage), FakeGuild(), FakeChannel(), PublishResult()

    await upsert(pub, guild, channel, an_embed("first"), result)
    await upsert(pub, guild, channel, an_embed("second"), result)

    assert channel.edited == channel.sent, "the message it sent is the one it edited"
    assert len(channel.sent) == 1
    assert result.unchanged == 0


async def test_only_the_time_changing_does_not_count_as_a_change(storage):
    """Every embed is stamped with the time it was built, so the signature has to
    ignore it or nothing would ever be unchanged."""
    pub, guild, channel, result = publisher(storage), FakeGuild(), FakeChannel(), PublishResult()

    first, second = an_embed(), an_embed()
    first.timestamp = discord.utils.utcnow()
    second.timestamp = first.timestamp + timedelta(hours=1)

    await upsert(pub, guild, channel, first, result)
    await upsert(pub, guild, channel, second, result)

    assert result.unchanged == 1


async def test_force_rewrites_a_board_that_has_not_changed(storage):
    pub, guild, channel, result = publisher(storage), FakeGuild(), FakeChannel(), PublishResult()

    await upsert(pub, guild, channel, an_embed(), result)
    await upsert(pub, guild, channel, an_embed(), result, force=True)

    assert len(channel.edited) == 1 and result.unchanged == 0


async def test_a_board_someone_deleted_comes_back(storage):
    """Editing a message that is gone raises NotFound; the board should be re-sent
    and remembered by its new id, or it would be lost until a forced refresh."""
    pub, guild, channel, result = publisher(storage), FakeGuild(), FakeChannel(), PublishResult()
    await upsert(pub, guild, channel, an_embed("first"), result)
    first_id = channel.sent[0]

    channel.fail_with = discord.NotFound(
        type("R", (), {"status": 404, "reason": "", "headers": {}})(), {"code": 0, "message": "gone"}
    )
    await upsert(pub, guild, channel, an_embed("second"), result)
    channel.fail_with = None

    assert len(channel.sent) == 2 and channel.sent[1] != first_id
    post = await storage.get_post(guild.id, KIND_PICKS, BOARD_KEY)
    assert post.message_id == channel.sent[1], "the replacement is the one it now tracks"


async def test_an_edit_discord_refuses_does_not_leave_two_boards(storage):
    """If the edit failed for any reason other than the message being gone, the
    message is probably still there. Sending a replacement would show the card
    twice; the next pass tries again instead."""
    pub, guild, channel, result = publisher(storage), FakeGuild(), FakeChannel(), PublishResult()
    await upsert(pub, guild, channel, an_embed("first"), result)

    channel.fail_with = http_error(500)
    await upsert(pub, guild, channel, an_embed("second"), result)

    assert len(channel.sent) == 1, "no duplicate board"


# -- deleting -------------------------------------------------------------------


async def test_removing_a_board_deletes_the_message_and_forgets_it(storage):
    pub, guild, channel, result = publisher(storage), FakeGuild(), FakeChannel(), PublishResult()
    await upsert(pub, guild, channel, an_embed(), result)

    await pub._remove_post(guild, channel, KIND_PICKS, BOARD_KEY, result)

    assert channel.deleted == channel.sent
    assert await storage.get_post(guild.id, KIND_PICKS, BOARD_KEY) is None


async def test_a_board_already_gone_is_still_forgotten(storage):
    """Otherwise the bot keeps a pointer to a message that does not exist and
    tries to delete it on every pass."""
    pub, guild, channel, result = publisher(storage), FakeGuild(), FakeChannel(), PublishResult()
    await upsert(pub, guild, channel, an_embed(), result)

    channel.fail_with = discord.NotFound(
        type("R", (), {"status": 404, "reason": "", "headers": {}})(), {"code": 0, "message": "gone"}
    )
    await pub._remove_post(guild, channel, KIND_PICKS, BOARD_KEY, result)

    assert await storage.get_post(guild.id, KIND_PICKS, BOARD_KEY) is None


async def test_a_board_that_moved_channels_is_not_deleted_from_the_wrong_one(storage):
    """The record points at another channel, so there is nothing here to delete
    and the publisher must not go deleting whatever it finds."""
    pub, guild, result = publisher(storage), FakeGuild(), PublishResult()
    old, new = FakeChannel(500), FakeChannel(600)
    await upsert(pub, guild, old, an_embed(), result)

    await pub._remove_post(guild, new, KIND_PICKS, BOARD_KEY, result)

    assert new.deleted == [] and old.deleted == []
    assert await storage.get_post(guild.id, KIND_PICKS, BOARD_KEY) is None


# -- one board failing --------------------------------------------------------------


async def test_one_board_blowing_up_does_not_stop_the_others(storage):
    """Every board is published in the same pass; a card that will not build
    should cost that board, not the schedule and the leaderboard with it."""
    pub = publisher(storage)
    guild, channel = FakeGuild(), FakeChannel()
    calls: list[str] = []

    async def ok(_guild, _channel, _settings, result, _force):
        calls.append("ok")
        result.schedule = True

    async def boom(_guild, _channel, _settings, _result, _force):
        calls.append("boom")
        raise RuntimeError("card would not load")

    pub._publish_schedule, pub._publish_picks = boom, ok
    pub._channel = lambda _guild, channel_id: channel if channel_id else None

    settings = type("S", (), dict.fromkeys(
        ("schedule_channel_id", "predictions_channel_id"), 500
    ) | dict.fromkeys(
        ("accuracy_channel_id", "pickem_channel_id", "rankings_channel_id"), None
    ))()

    result = await pub.publish(guild, settings)

    assert calls == ["boom", "ok"], "it carried on after the failure"
    assert result.schedule is True
    assert len(result.errors) == 1 and "card would not load" in result.errors[0]


# -- when the pick'em board is allowed to open --------------------------------------


def test_a_priced_card_opens_and_an_unpriced_one_waits(soon):
    a, b = bout("B1", ALLEN, PICO), bout("B2", ALLEN, PICO, match=2)
    event = card(a, b, start=soon)
    now = soon - timedelta(days=10)

    a.odds = {ALLEN.id: -150, PICO.id: 130}
    assert not ready_to_open(event, now), "half a card, and ten days to wait for the rest"

    b.odds = {ALLEN.id: -110, PICO.id: -110}
    assert ready_to_open(event, now)


def test_a_card_two_days_out_opens_on_whatever_prices_exist(soon):
    """One prelim that never gets a line must not keep the whole server from
    playing the card."""
    priced, unpriced = bout("B1", ALLEN, PICO), bout("B2", ALLEN, PICO, match=2)
    priced.odds = {ALLEN.id: -150, PICO.id: 130}
    event = card(priced, unpriced, start=soon)

    assert not ready_to_open(event, soon - timedelta(days=3))
    assert ready_to_open(event, soon - timedelta(hours=47))


def test_a_card_with_no_prices_at_all_never_opens(soon):
    event = card(bout("B1", ALLEN, PICO), start=soon)
    assert not ready_to_open(event, soon - timedelta(minutes=5))
    assert not ready_to_open(card(start=soon), soon)
