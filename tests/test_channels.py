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
from conftest import bout, card, fighter, pickem_record

from ufcbot.embeds import pickem_board_embed
from ufcbot.features import channels as channels_module
from ufcbot.features.channels import (
    BOARD_KEY,
    KIND_PICKEM,
    KIND_PICKEM_CARD_LEADERBOARD,
    KIND_PICKEM_LEADERBOARD,
    KIND_PICKS,
    LAST_CARD_TITLE,
    THIS_CARD_TITLE,
    ChannelPublisher,
    PublishResult,
    card_leaderboard_title,
)

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
        ("pickem_channel_id", "rankings_channel_id"), None
    ))()

    result = await pub.publish(guild, settings)

    assert calls == ["boom", "ok"], "it carried on after the failure"
    assert result.schedule is True
    assert len(result.errors) == 1 and "card would not load" in result.errors[0]


# -- the two leaderboards ----------------------------------------------------------


async def pick(storage, *, user: int, event: str, bout: str, start, athlete: str = "A") -> None:
    await storage.save_pickem_pick(
        pickem_record(
            user_id=user, bout_id=bout, athlete_id=athlete, opponent_id="B",
            locks_at=start, event_id=event,
        )
    )


def an_event(event_id: str, start) -> object:
    return card(bout("B1", ALLEN, PICO), start=start, event_id=event_id)


async def test_the_card_leaderboard_is_about_the_last_card_scored(storage, soon):
    """Two cards, only the older one graded: that is the one to show."""
    older, newer = soon - timedelta(days=30), soon
    await pick(storage, user=1, event="OLD", bout="B1", start=older)
    await pick(storage, user=1, event="NEW", bout="B2", start=newer)
    await storage.grade_pickem_bout("B1", "A", loss_points=-100, fighters={"A", "B"})

    assert (await storage.pickem_last_scored_card(1))[0] == "OLD"

    # Once the newer card starts landing results it takes over.
    await storage.grade_pickem_bout("B2", "A", loss_points=-100, fighters={"A", "B"})
    assert (await storage.pickem_last_scored_card(1))[0] == "NEW"


async def test_a_card_nobody_has_a_settled_pick_on_is_not_the_one_shown(storage, soon):
    await pick(storage, user=1, event="EV1", bout="B1", start=soon)
    assert await storage.pickem_last_scored_card(1) is None




def test_the_card_leaderboard_title_follows_the_card_being_fought(soon):
    """It shows whichever card was scored most recently, so the title turns
    over on its own as one card ends and the next opens."""
    current = an_event("NEW", soon)

    assert card_leaderboard_title("NEW", current) == THIS_CARD_TITLE
    assert card_leaderboard_title("OLD", current) == LAST_CARD_TITLE
    assert card_leaderboard_title("OLD", None) == LAST_CARD_TITLE
    assert card_leaderboard_title(None, current) == LAST_CARD_TITLE


async def test_neither_leaderboard_is_ever_deleted(storage):
    """All three pick'em messages have a permanent place in the channel now, so
    a delete here would mean something had gone wrong with the keying."""
    pub, guild, channel, result = publisher(storage), FakeGuild(), FakeChannel(), PublishResult()

    for kind in (KIND_PICKEM_LEADERBOARD, KIND_PICKEM_CARD_LEADERBOARD):
        await pub._upsert(guild, channel, kind, BOARD_KEY, an_embed("standings"), result)
    leaderboards = list(channel.sent)

    # A board left over from the version that keyed by card is swept once.
    await pub._upsert(guild, channel, KIND_PICKEM, "OLD", an_embed("old card"), result)
    await pub._remove_post(guild, channel, KIND_PICKEM, "OLD", result)
    await pub._upsert(guild, channel, KIND_PICKEM, "NEW", an_embed("new card"), result)

    assert all(message_id not in channel.deleted for message_id in leaderboards)
    assert len(channel.deleted) == 1, "only the card's board went"


async def test_a_leaderboard_is_edited_where_a_new_card_gets_a_new_message(storage):
    """The leaderboards keep their place in the scrollback; a card turning over
    should read as something happening in the channel."""
    pub, guild, channel, result = publisher(storage), FakeGuild(), FakeChannel(), PublishResult()

    await pub._upsert(guild, channel, KIND_PICKEM_CARD_LEADERBOARD, BOARD_KEY, an_embed("last card"), result)
    await pub._upsert(guild, channel, KIND_PICKEM_CARD_LEADERBOARD, BOARD_KEY, an_embed("this card"), result)

    assert len(channel.sent) == 1 and len(channel.edited) == 1

    await pub._upsert(guild, channel, KIND_PICKEM, "OLD", an_embed("old"), result)
    await pub._remove_post(guild, channel, KIND_PICKEM, "OLD", result)
    await pub._upsert(guild, channel, KIND_PICKEM, "NEW", an_embed("new"), result)

    assert len(channel.sent) == 3, "a different card is never an edit of the last one"


async def test_the_board_is_edited_as_the_card_turns_over_not_reposted(storage):
    """All three pick'em messages keep their place now. A card changing is an
    edit, so nobody has to scroll to find where the board went."""
    pub, guild, channel, result = publisher(storage), FakeGuild(), FakeChannel(), PublishResult()

    await pub._upsert(guild, channel, KIND_PICKEM, BOARD_KEY, an_embed("UFC 332"), result, extra="332")
    await pub._upsert(guild, channel, KIND_PICKEM, BOARD_KEY, an_embed("UFC 333"), result, extra="333")

    assert len(channel.sent) == 1 and len(channel.edited) == 1
    assert channel.deleted == []


def test_a_card_with_no_prices_still_gets_a_board(soon):
    """The board says which fights have no line yet and will not take a pick on
    them. An empty channel says nothing at all."""
    event = card(bout("B1", ALLEN, PICO), start=soon)
    embed = pickem_board_embed(event, {}, 0, now=soon - timedelta(days=3))

    assert "Odds not posted yet" in embed.fields[0].value


async def test_the_pickem_title_jumps_to_this_card_on_the_picks_board(storage, soon):
    """Tapping the title should land on what the model said about the same
    fights, not on ESPN. The picks board is published earlier in the same pass,
    so the message it points at is already there."""
    pub, guild, event = publisher(storage), FakeGuild(), card(bout("B1", ALLEN, PICO), start=soon)
    await storage.save_post(guild.id, KIND_PICKS, event.id, channel_id=555, message_id=777)

    link = await pub._picks_link(guild, event)

    assert link == f"https://discord.com/channels/{guild.id}/555/777"


async def test_the_pickem_title_falls_back_to_espn_with_no_picks_channel(storage, soon):
    """A server that runs pick'em without the picks board has nothing to jump
    to, and a title that links nowhere is worse than one that links out."""
    pub, guild = publisher(storage), FakeGuild()
    assert await pub._picks_link(guild, card(bout("B1", ALLEN, PICO), start=soon)) is None


def test_the_board_uses_the_jump_link_when_it_has_one(soon):
    event = card(bout("B1", ALLEN, PICO), start=soon, event_id="E9")
    event.espn_url = "https://espn.com/mma/fightcenter"
    jump = "https://discord.com/channels/1/2/3"

    assert pickem_board_embed(event, {}, 0, now=soon, picks_link=jump).url == jump
    assert pickem_board_embed(event, {}, 0, now=soon).url == event.espn_url
