"""The picks channel reads top to bottom, earliest card first, and is kept that
way by editing messages, never by deleting them.

The channel is faked as Discord shows it: messages in the order they were
posted, each with whatever it says now. A test reads the titles top to bottom
and counts what was sent, edited and deleted to get there.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import discord
import pytest
from conftest import bout, card, fighter

from ufcbot.features import channels as channels_module
from ufcbot.features.channels import (
    BOARD_KEY,
    KIND_PICKS,
    KIND_SCORECARD,
    PICKS_BOARD_LIFETIME,
    ChannelPublisher,
)
from ufcbot.features.tracking import PredictionTracker
from ufcbot.records import GuildSettings, PredictionRecord

NOW = datetime.now(UTC)
SCORECARD = "📊 Model scorecard"


class Channel:
    """A channel's messages in posting order, and what each one says now."""

    def __init__(self) -> None:
        self.id, self.name = 501, "picks"
        self.messages: dict[int, discord.Embed] = {}
        self.sent = self.edited = 0
        self.deleted: list[int] = []
        self._next = 1000

    async def send(self, **kwargs):
        self._next += 1
        self.messages[self._next] = kwargs["embed"]
        self.sent += 1
        return SimpleNamespace(id=self._next)

    def get_partial_message(self, message_id: int):
        channel = self

        class Partial:
            id = message_id

            async def edit(self, **kwargs) -> None:
                if message_id not in channel.messages:
                    response = SimpleNamespace(status=404, reason="Not Found", headers={})
                    raise discord.NotFound(response, {"code": 10008, "message": "Unknown Message"})
                channel.messages[message_id] = kwargs["embed"]
                channel.edited += 1

            async def delete(self) -> None:
                channel.deleted.append(message_id)
                channel.messages.pop(message_id, None)

        return Partial()

    def titles(self) -> list[str]:
        return [self.messages[message_id].title for message_id in sorted(self.messages)]


class Schedule:
    """ESPN's upcoming cards, as many as the test has added."""

    def __init__(self) -> None:
        self.events = []

    async def upcoming_events(self, **kwargs):
        return sorted(self.events, key=lambda event: event.start)

    async def recent_events(self, **kwargs):
        return []

    async def get_event(self, event_id, **kwargs):
        return next((event for event in self.events if event.id == event_id), None)

    async def load_odds(self, event, **kwargs):
        return None


@pytest.fixture
def setup(storage, monkeypatch):
    monkeypatch.setattr(channels_module, "WRITE_SPACING", 0)
    schedule, channel = Schedule(), Channel()
    publisher = ChannelPublisher(
        schedule, storage, PredictionTracker(storage, schedule), pickem=None,
        pick_provider=lambda event: {}, evaluation_provider=lambda: None,
    )
    publisher._channel = lambda guild, channel_id: channel if channel_id == channel.id else None
    guild = SimpleNamespace(id=1, me=SimpleNamespace(id=99))
    settings = GuildSettings(guild_id=1, predictions_channel_id=channel.id)

    async def publish(*, force: bool = False):
        result = await publisher.publish(guild, settings, force=force)
        assert not result.errors, result.errors

    return SimpleNamespace(storage=storage, schedule=schedule, channel=channel, publish=publish)


async def add_card(s, number: int, *, days: float):
    """A card ``days`` out, with the model's pick on record for its one fight."""
    start = NOW + timedelta(days=days)
    a, b = fighter(f"{number}1", f"Al {number}"), fighter(f"{number}2", f"Bo {number}")
    event = card(bout(f"B{number}", a, b), start=start, event_id=f"6000{number:05d}")
    event.name = f"Card {number}"
    s.schedule.events.append(event)
    await s.storage.upsert_predictions([
        PredictionRecord(
            espn_event_id=event.id, bout_id=f"B{number}", event_name=event.name, event_start=start,
            athlete_a=a.id, name_a=a.display_name, athlete_b=b.id, name_b=b.display_name, prob_a=0.6,
        )
    ])
    return event


async def test_a_new_card_lands_in_date_order_wherever_it_belongs(setup):
    """At the end, in the middle, and at the top: one new message each time,
    the boards below the new card's place shifting down, nothing deleted."""
    s = setup
    await add_card(s, 1, days=7)
    await add_card(s, 3, days=21)
    await s.publish()
    assert s.channel.titles() == ["Card 1", "Card 3", SCORECARD]

    for number, days, expected in (
        (4, 28, ["Card 1", "Card 3", "Card 4", SCORECARD]),
        (2, 14, ["Card 1", "Card 2", "Card 3", "Card 4", SCORECARD]),
        (0, 3, ["Card 0", "Card 1", "Card 2", "Card 3", "Card 4", SCORECARD]),
    ):
        sent = s.channel.sent
        await add_card(s, number, days=days)
        await s.publish()
        assert s.channel.titles() == expected
        assert s.channel.sent == sent + 1, "one new message, at the bottom"

    assert s.channel.deleted == [], "nothing is ever deleted to reorder"


async def test_only_the_messages_below_the_new_card_are_edited(setup):
    s = setup
    for number, days in ((1, 7), (3, 21), (4, 28)):
        await add_card(s, number, days=days)
    await s.publish()
    top = min(s.channel.messages)
    first = s.channel.messages[top]

    edited = s.channel.edited
    await add_card(s, 2, days=14)
    await s.publish()

    assert s.channel.messages[top] is first, "the card above the new one's place is not touched"
    # Card 3, Card 4 and the scorecard move down a message each; the new
    # bottom message takes the scorecard, so three messages are edited.
    assert s.channel.edited - edited == 3


async def test_a_pass_where_nothing_moved_writes_nothing(setup):
    s = setup
    await add_card(s, 1, days=7)
    await add_card(s, 2, days=14)
    await s.publish()
    sent, edited = s.channel.sent, s.channel.edited

    await s.publish()
    assert (s.channel.sent, s.channel.edited) == (sent, edited)


async def test_a_card_that_moves_date_is_reordered_by_editing(setup):
    s = setup
    await add_card(s, 1, days=7)
    late = await add_card(s, 2, days=14)
    await s.publish()
    sent = s.channel.sent

    late.start = NOW + timedelta(days=3)  # ESPN brings it forward a week and a half
    await s.publish()

    assert s.channel.titles() == ["Card 2", "Card 1", SCORECARD]
    assert s.channel.sent == sent and s.channel.deleted == []


async def test_the_card_link_follows_the_board_to_its_new_message(setup):
    """Pick'em's title jumps to a card's message on the picks board, so the
    record of which message shows a card moves with the card."""
    s = setup
    await add_card(s, 1, days=7)
    later = await add_card(s, 3, days=21)
    await s.publish()

    await add_card(s, 2, days=14)
    await s.publish()

    post = await s.storage.get_post(1, KIND_PICKS, later.id)
    assert s.channel.messages[post.message_id].title == "Card 3"
    scorecard = await s.storage.get_post(1, KIND_SCORECARD, BOARD_KEY)
    assert s.channel.messages[scorecard.message_id].title == SCORECARD


async def test_boards_posted_before_this_are_taken_over_and_put_in_order(setup):
    """The channel as the old publisher left it: one message per card, the last
    card to arrive at the bottom whatever its date, the scorecard under it."""
    s = setup
    one = await add_card(s, 1, days=7)
    three = await add_card(s, 3, days=21)
    two = await add_card(s, 2, days=14)  # arrived last, so sits below Card 3
    for event in (one, three, two):
        message = await s.channel.send(embed=discord.Embed(title=event.name))
        await s.storage.save_post(1, KIND_PICKS, event.id, s.channel.id, message.id, signature=None)
    message = await s.channel.send(embed=discord.Embed(title=SCORECARD))
    await s.storage.save_post(1, KIND_SCORECARD, BOARD_KEY, s.channel.id, message.id, signature=None)
    sent = s.channel.sent

    await s.publish()

    assert s.channel.titles() == ["Card 1", "Card 2", "Card 3", SCORECARD]
    assert s.channel.sent == sent and s.channel.deleted == []


async def test_a_board_somebody_deleted_comes_back_in_its_place(setup):
    s = setup
    for number, days in ((1, 7), (2, 14), (3, 21)):
        await add_card(s, number, days=days)
    await s.publish()
    middle = sorted(s.channel.messages)[1]
    del s.channel.messages[middle]  # a moderator removed Card 2's board

    await s.publish(force=True)

    assert s.channel.titles() == ["Card 1", "Card 2", "Card 3", SCORECARD]
    assert s.channel.deleted == []


async def test_a_card_past_its_lifetime_is_let_go_at_the_top(setup):
    """It keeps its final board where it is, the oldest at the top, and the
    channel stops redrawing it. A new card later on does not disturb it."""
    s = setup
    old = await add_card(s, 1, days=-1)
    await add_card(s, 2, days=7)
    await s.publish()
    top = min(s.channel.messages)
    assert s.channel.messages[top].title == "Card 1"

    s.schedule.events.remove(old)  # it happened, so it is no longer upcoming
    for record in await s.storage.predictions_for_event(old.id):
        await s.storage.upsert_predictions([
            PredictionRecord(**{**{f: getattr(record, f) for f in record.__dataclass_fields__},
                                "event_start": NOW - PICKS_BOARD_LIFETIME - timedelta(hours=1)})
        ])
    await s.publish()
    assert old.id not in {slot.key for slot in await s.storage.board_slots(1, s.channel.id)}, "let go"

    await add_card(s, 3, days=14)
    await s.publish()
    assert s.channel.titles() == ["Card 1", "Card 2", "Card 3", SCORECARD]
    assert s.channel.messages[top].title == "Card 1", "its final board stays where it was"
    assert s.channel.deleted == []


async def test_an_older_version_running_in_between_is_tidied_up(setup):
    """A test deploy rolled back to the old code, which posts a new card at the
    bottom and takes the scorecard down to post it again beneath. The next pass
    of this code takes in what it posted and puts it in order: one scorecard,
    at the foot, and nothing deleted by this side."""
    s = setup
    await add_card(s, 1, days=7)
    await add_card(s, 3, days=21)
    await s.publish()

    # What the old publisher would do with a new card dated between the two.
    two = await add_card(s, 2, days=14)
    message = await s.channel.send(embed=discord.Embed(title=two.name))
    await s.storage.save_post(1, KIND_PICKS, two.id, s.channel.id, message.id, signature=None)
    old_scorecard = await s.storage.get_post(1, KIND_SCORECARD, BOARD_KEY)
    del s.channel.messages[old_scorecard.message_id]
    message = await s.channel.send(embed=discord.Embed(title=SCORECARD))
    await s.storage.save_post(1, KIND_SCORECARD, BOARD_KEY, s.channel.id, message.id, signature=None)

    await s.publish()

    assert s.channel.titles() == ["Card 1", "Card 2", "Card 3", SCORECARD]
    assert s.channel.deleted == []
