"""Maintains the bot's channel boards: picks, schedule, accuracy and pick'em.

Each board is one message the bot edits in place, so a channel stays tidy. A hash
of each board's content is stored with its message id and a board is only edited
when that hash changes, so a refresh where nothing moved costs no Discord API calls.
Writes that do happen are spaced out to stay under Discord's per-channel edit limit.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import discord

from ..embeds import (
    pickem_board_embed,
    pickem_leaderboard_embed,
    picks_board_embed,
    rankings_embed,
    recap_embed,
    schedule_embed,
    scorecard_embed,
)
from ..models import Event
from ..records import GuildSettings
from ..sources.espn import UFCData
from ..stats.prediction import Evaluation, Prediction
from ..stats.rankings import POUND_FOR_POUND, divisions_with_fighters, rank_division
from ..storage import Storage
from ..ui.pickem import board_view, my_picks_view
from ..util import normalise
from .pickem import (
    OPEN,
    PickemService,
    benchmarks,
    bout_status,
    ready_to_open,
)
from .tracking import PredictionTracker, build_scorecard

log = logging.getLogger(__name__)

KIND_PICKS = "picks"
KIND_SCHEDULE = "schedule"
KIND_SCORECARD = "scorecard"
KIND_PICKEM = "pickem"
KIND_PICKEM_LEADERBOARD = "pickem_leaderboard"
KIND_RANKINGS = "rankings"
BOARD_KEY = "board"

# Bot messages in the pick'em channel with these titles are pick'em posts; any
# that aren't the current board or the leaderboard are leftovers to delete.
PICKEM_TITLES = ("🎯 Pick'em:", "🏆")

THIS_CARD_TITLE = "🏆 This Card's Pick'em Leaderboard"
LAST_CARD_TITLE = "🏆 Last Card's Pick'em Leaderboard"

# The pick'em channel holds two messages: the all-time leaderboard, which is
# never deleted, and one below it that turns over with the card. That second
# message is only ever in one of three states.
STAGE_BOARD = "board"
STAGE_THIS_CARD = "this"
STAGE_LAST_CARD = "last"

# How long before the first bell the game opens. Before that the channel shows
# the last card instead, so there is something to read between cards without a
# board sitting open for a fortnight.
PICKEM_OPENS = timedelta(hours=48)


def pickem_stage(
    current: Event | None,
    last_card_id: str | None,
    now: datetime,
    *,
    priced: bool,
) -> tuple[str, str | None]:
    """Which of the three the rotating message should be, and for which card.

    In order of precedence, because they describe one card's night as it runs:
    the card being fought, then the card about to be fought once it is close
    enough and priced, then the last card anyone scored.
    """
    if current is not None:
        if current.start <= now:
            return STAGE_THIS_CARD, current.id
        if current.start - now <= PICKEM_OPENS and priced:
            return STAGE_BOARD, current.id
    return STAGE_LAST_CARD, last_card_id

# Keep editing a card's board this long after it starts, so results show up.
PICKS_BOARD_LIFETIME = timedelta(days=4)

# A card that has started is asked for this fresh: its winner flags change by the
# minute and the board is redrawn as soon as live coverage posts a result.
LIVE_CARD_TTL = 60

# Discord allows roughly five message edits per channel every five seconds.
WRITE_SPACING = 1.2


def _signature(embed: discord.Embed, extra: str = "") -> str:
    """Content hash of a board, ignoring its "Last updated" time.

    Leaving the time out means an unchanged board is skipped, so the time shown
    in Discord stays at the last real change instead of every refresh. ``extra``
    covers anything else that should force an edit, such as button state.
    """
    content = embed.to_dict()
    content.pop("timestamp", None)
    payload = json.dumps(content, sort_keys=True, default=str) + extra
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()


@dataclass(slots=True)
class PublishResult:
    picks_boards: int = 0
    pickem_boards: int = 0
    rankings: int = 0
    recaps: int = 0
    schedule: bool = False
    updated: int = 0
    """Messages sent, edited or deleted."""
    unchanged: int = 0
    """Boards skipped because nothing on them changed."""
    errors: list[str] = field(default_factory=list)

    def summary(self) -> str:
        parts = []
        if self.picks_boards:
            parts.append(f"{self.picks_boards} picks boards")
        if self.pickem_boards:
            parts.append(f"{self.pickem_boards} pick'em boards")
        if self.rankings:
            parts.append(f"{self.rankings} ratings boards")
        if self.schedule:
            parts.append("schedule board")
        if self.recaps:
            parts.append(f"{self.recaps} recaps")
        parts.append(f"{self.updated} messages updated, {self.unchanged} unchanged")
        if self.errors:
            parts.append(f"{len(self.errors)} errors")
        return ", ".join(parts)


class ChannelPublisher:
    def __init__(
        self,
        data: UFCData,
        storage: Storage,
        tracker: PredictionTracker,
        pickem: PickemService,
        *,
        pick_provider: Callable[[Event], dict[str, Prediction]],
        evaluation_provider: Callable[[], Evaluation | None],
        ledger_provider: Callable[[], dict] | None = None,
    ) -> None:
        self.data = data
        self.storage = storage
        self.tracker = tracker
        self.pickem = pickem
        self.pick_provider = pick_provider
        self.evaluation_provider = evaluation_provider
        # Optional: the career ledgers the ratings boards rank.
        self.ledger_provider = ledger_provider

    async def publish(self, guild: discord.Guild, settings: GuildSettings, *, force: bool = False) -> PublishResult:
        """Bring every configured board in this guild up to date.

        ``force`` rewrites boards even when unchanged, which also re-sends any
        board message someone deleted.
        """
        result = PublishResult()
        jobs = (
            ("schedule", settings.schedule_channel_id, self._publish_schedule),
            ("picks", settings.predictions_channel_id, self._publish_picks),
            ("accuracy", settings.accuracy_channel_id, self._publish_accuracy),
            ("pickem", settings.pickem_channel_id, self._publish_pickem),
            ("rankings", settings.rankings_channel_id, self._publish_rankings),
        )
        for name, channel_id, job in jobs:
            channel = self._channel(guild, channel_id)
            if channel is None:
                continue
            try:
                await job(guild, channel, settings, result, force)
            except Exception as exc:  # one board must not block the others
                log.exception("%s board failed in guild %s", name, guild.id)
                result.errors.append(f"{name}: {exc}")
        return result

    # -- boards ------------------------------------------------------------------

    async def _upcoming(self, settings: GuildSettings) -> list[Event]:
        events = await self.data.upcoming_events(days=settings.days_ahead, limit=25)
        if settings.include_contender_series:
            return events
        return [e for e in events if "contender series" not in normalise(e.name)]

    async def _publish_schedule(
        self,
        guild: discord.Guild,
        channel: discord.TextChannel,
        settings: GuildSettings,
        result: PublishResult,
        force: bool,
    ) -> None:
        events = await self._upcoming(settings)
        embed = schedule_embed(events[:15], title="📅 Upcoming UFC cards")
        await self._upsert(guild, channel, KIND_SCHEDULE, BOARD_KEY, embed, result, force=force)
        result.schedule = True

    async def _publish_picks(
        self,
        guild: discord.Guild,
        channel: discord.TextChannel,
        settings: GuildSettings,
        result: PublishResult,
        force: bool,
    ) -> None:
        now = datetime.now(UTC)
        upcoming = await self._upcoming(settings)

        for summary in upcoming:
            # Without the full card there is nothing to re-record against, so the
            # board is rebuilt from what is already on record rather than from a
            # card that happens to have no fights on it. A card under way is asked
            # for fresh: winner flags live in that document, and this board is
            # rebuilt the moment live coverage posts a result.
            event = await self.data.get_event(summary.id, ttl=LIVE_CARD_TTL if summary.start <= now else None)
            if event is None:
                log.debug("Card for %s did not load; leaving its picks as they are", summary.name)
                event = summary
            elif event.start > now:
                # Lines move all week; refresh them each time the board is rebuilt.
                await self.data.load_odds(event)
                await self.tracker.record(event, self.pick_provider(event))
            records = await self.tracker.records_for(event.id)
            if not records:
                continue
            embed = picks_board_embed(
                event.name,
                event.start,
                records,
                locked=event.start <= now,
                espn_url=event.espn_url,
            )
            await self._upsert(guild, channel, KIND_PICKS, event.id, embed, result, force=force)
            result.picks_boards += 1

        # Cards that recently happened are no longer "upcoming" but their boards
        # should still pick up results.
        upcoming_ids = {e.id for e in upcoming}
        for event_id in await self.storage.posts_of_kind(guild.id, KIND_PICKS):
            if event_id in upcoming_ids:
                continue
            records = await self.tracker.records_for(event_id)
            if not records or now - records[0].event_start > PICKS_BOARD_LIFETIME:
                # Leave the message as its final state; just stop tracking it.
                await self.storage.delete_post(guild.id, KIND_PICKS, event_id)
                continue
            # Usually a card that has already happened, but a card can also drop
            # out of the upcoming list for a tick when ESPN is slow, and that one
            # has not locked.
            embed = picks_board_embed(
                records[0].event_name,
                records[0].event_start,
                records,
                locked=records[0].event_start <= now,
            )
            await self._upsert(guild, channel, KIND_PICKS, event_id, embed, result, force=force)
            result.picks_boards += 1

    async def _publish_rankings(
        self,
        guild: discord.Guild,
        channel: discord.TextChannel,
        settings: GuildSettings,
        result: PublishResult,
        force: bool,
    ) -> None:
        """One ratings board per division, then pound for pound below them."""
        ledgers = self.ledger_provider() if self.ledger_provider else None
        if not ledgers:
            return

        today = datetime.now(UTC).date()
        women = settings.rankings_include_women
        divisions = divisions_with_fighters(ledgers, on=today, include_women=women)
        wanted: list[tuple[str, str, bool]] = [(name, name, False) for name in divisions]
        wanted.append((POUND_FOR_POUND, POUND_FOR_POUND, True))

        for key, title, p4p in wanted:
            entries = rank_division(ledgers, None if p4p else key, on=today, include_women=women)
            if not entries:
                continue
            await self._upsert(
                guild,
                channel,
                KIND_RANKINGS,
                key,
                # The last board carries the explanation, so the channel says it once.
                rankings_embed(title, entries, pound_for_pound=p4p, note=p4p),
                result,
                force=force,
            )
            result.rankings += 1

        # A division that has emptied out, or was renamed, leaves a board behind.
        keep = {name for name, _t, _p in wanted}
        for key in await self.storage.posts_of_kind(guild.id, KIND_RANKINGS):
            if key not in keep:
                await self._remove_post(guild, channel, KIND_RANKINGS, key, result)

    async def _publish_accuracy(
        self,
        guild: discord.Guild,
        channel: discord.TextChannel,
        settings: GuildSettings,
        result: PublishResult,
        force: bool,
    ) -> None:
        since = settings.tracking_since
        events = await self.tracker.graded_events(since)  # oldest first

        posted = False
        for event in events:
            if await self.storage.recap_posted(guild.id, event.espn_event_id):
                continue
            # The scorecard shown with a recap only counts cards up to that one.
            partial = build_scorecard(since, [e for e in events if e.start <= event.start])
            await channel.send(embed=recap_embed(event, partial))
            await self.storage.mark_recap_posted(guild.id, event.espn_event_id)
            posted = True
            result.recaps += 1
            result.updated += 1
            await asyncio.sleep(WRITE_SPACING)

        await self._upsert(
            guild,
            channel,
            KIND_SCORECARD,
            BOARD_KEY,
            scorecard_embed(build_scorecard(since, events), evaluation=self.evaluation_provider()),
            result,
            force=force,
            # Move the scorecard below a new recap so it stays the latest message.
            resend=posted,
        )

    async def _publish_pickem(
        self,
        guild: discord.Guild,
        channel: discord.TextChannel,
        settings: GuildSettings,
        result: PublishResult,
        force: bool,
    ) -> None:
        """The all-time leaderboard, and one message below it that turns over.

        The second message is the card's board while the game is open, that
        card's leaderboard while it is being fought, and the last card's
        leaderboard the rest of the time. It is keyed by which of those it is,
        so moving between them deletes the old message and posts a new one:
        a card ending and the next beginning should read as an event in the
        channel, not as a message quietly changing under everyone.
        """
        now = datetime.now(UTC)
        current = await self.pickem.current_event()
        last = await self.storage.pickem_last_scored_card(guild.id)
        stage, event_id = pickem_stage(
            current,
            last[0] if last else None,
            now,
            priced=current is not None and ready_to_open(current, now),
        )

        await self._upsert(
            guild,
            channel,
            KIND_PICKEM_LEADERBOARD,
            BOARD_KEY,
            pickem_leaderboard_embed(
                await self.storage.pickem_leaderboard(guild.id),
                subtitle="Every card since the bot started keeping score.",
                benchmarks=benchmarks(await self.storage.graded_predictions()),
            ),
            result,
            force=force,
        )

        # Whatever the message used to be goes first, so the replacement lands
        # at the bottom of the channel rather than above the leaderboard.
        wanted = f"{stage}:{event_id}" if event_id else None
        removed = False
        for key in await self.storage.posts_of_kind(guild.id, KIND_PICKEM):
            if key != wanted:
                await self._remove_post(guild, channel, KIND_PICKEM, key, result)
                removed = True
        if wanted is None:
            return

        if stage == STAGE_BOARD:
            await self._pickem_board(guild, channel, current, wanted, result, force, now)
        else:
            await self._pickem_card_leaderboard(guild, channel, stage, event_id, result, force)
        if removed or force:
            await self._sweep_pickem(guild, channel, result)

    async def _pickem_card_leaderboard(
        self,
        guild: discord.Guild,
        channel: discord.TextChannel,
        stage: str,
        event_id: str,
        result: PublishResult,
        force: bool,
    ) -> None:
        """One card's standings, with the button that shows what you picked on it."""
        standings = await self.storage.pickem_leaderboard(guild.id, event_id)
        name = await self.storage.pickem_event_name(guild.id, event_id)
        this_card = stage == STAGE_THIS_CARD
        result.pickem_boards += 1
        await self._upsert(
            guild,
            channel,
            KIND_PICKEM,
            f"{stage}:{event_id}",
            pickem_leaderboard_embed(
                standings,
                title=THIS_CARD_TITLE if this_card else LAST_CARD_TITLE,
                subtitle=name,
                benchmarks=benchmarks(await self.storage.predictions_for_event(event_id)),
                empty="No results in yet." if this_card else "Nobody played this card.",
            ),
            result,
            force=force,
            view=my_picks_view(event_id),
            extra=stage,
        )

    async def _pickem_board(
        self,
        guild: discord.Guild,
        channel: discord.TextChannel,
        event: Event,
        key: str,
        result: PublishResult,
        force: bool,
        now: datetime,
    ) -> None:
        """The card's board, with the picker and the button that shows your picks."""
        counts = await self.storage.pickem_counts(guild.id, event.id)
        players = await self.storage.pickem_player_count(guild.id, event.id)
        accepting = any(bout_status(event, bout, now) == OPEN for bout in event.fights)
        result.pickem_boards += 1
        await self._upsert(
            guild,
            channel,
            KIND_PICKEM,
            key,
            pickem_board_embed(event, counts, players, now=now),
            result,
            force=force,
            view=board_view(event.id, accepting=accepting),
            extra=f"accepting={accepting}",
        )

    async def _remove_post(
        self, guild: discord.Guild, channel: discord.TextChannel, kind: str, key: str, result: PublishResult
    ) -> None:
        post = await self.storage.get_post(guild.id, kind, key)
        if post and post.channel_id == channel.id:
            try:
                await channel.get_partial_message(post.message_id).delete()
                result.updated += 1
                await asyncio.sleep(WRITE_SPACING)
            except discord.NotFound:
                pass  # already gone
            except discord.HTTPException as exc:
                log.warning("Could not delete %s message in #%s: %r", kind, channel.name, exc)
                return
        await self.storage.delete_post(guild.id, kind, key)

    async def _sweep_pickem(self, guild: discord.Guild, channel: discord.TextChannel, result: PublishResult) -> None:
        """Delete leftover pick'em posts, such as old results posts or boards for other cards."""
        keep = {post.message_id for post in (await self.storage.posts_of_kind(guild.id, KIND_PICKEM)).values()}
        leaderboard = await self.storage.get_post(guild.id, KIND_PICKEM_LEADERBOARD, BOARD_KEY)
        if leaderboard:
            keep.add(leaderboard.message_id)
        try:
            async for message in channel.history(limit=100):
                if message.id in keep or message.author.id != guild.me.id or not message.embeds:
                    continue
                if (message.embeds[0].title or "").startswith(PICKEM_TITLES):
                    await message.delete()
                    result.updated += 1
                    await asyncio.sleep(WRITE_SPACING)
        except discord.HTTPException as exc:
            log.warning("Could not tidy #%s: %r", channel.name, exc)

    # -- discord plumbing ----------------------------------------------------------

    def _channel(self, guild: discord.Guild, channel_id: int | None) -> discord.TextChannel | None:
        channel = guild.get_channel(channel_id) if channel_id else None
        if not isinstance(channel, discord.TextChannel):
            return None
        permissions = channel.permissions_for(guild.me)
        if not (permissions.send_messages and permissions.embed_links):
            log.warning("Missing send/embed permission in #%s (guild %s)", channel.name, guild.id)
            return None
        return channel

    async def _upsert(
        self,
        guild: discord.Guild,
        channel: discord.TextChannel,
        kind: str,
        key: str,
        embed: discord.Embed,
        result: PublishResult,
        *,
        force: bool = False,
        resend: bool = False,
        view: discord.ui.View | None = None,
        extra: str = "",
    ) -> bool:
        """Edit the stored message for (kind, key) if its content changed, or send a new one.

        Returns True when a new message was sent.
        """
        # Boards are edited in place, so their timestamp reads as when they last changed.
        embed.set_footer(text="Last updated")
        signature = _signature(embed, extra)
        post = await self.storage.get_post(guild.id, kind, key)
        # Only pass a view when there is one; editing with view=None would strip buttons.
        components = {"view": view} if view is not None else {}

        if post and post.channel_id == channel.id:
            if not force and not resend and post.signature == signature:
                result.unchanged += 1
                return False
            # A partial message edits without first fetching it, saving a request.
            message = channel.get_partial_message(post.message_id)
            try:
                if resend:
                    await message.delete()
                else:
                    await message.edit(embed=embed, **components)
                    await self.storage.save_post(guild.id, kind, key, channel.id, post.message_id, signature)
                    result.updated += 1
                    await asyncio.sleep(WRITE_SPACING)
                    return False
            except discord.NotFound:
                pass  # deleted by someone; send a fresh one below
            except discord.HTTPException as exc:
                # Sending a replacement when the old message is still there would
                # leave two of the same board behind; the next pass tries again.
                log.warning(
                    "Could not %s the %s board in #%s: %r",
                    "move" if resend else "update",
                    kind,
                    channel.name,
                    exc,
                )
                return False

        message = await channel.send(embed=embed, **components)
        await self.storage.save_post(guild.id, kind, key, channel.id, message.id, signature)
        result.updated += 1
        await asyncio.sleep(WRITE_SPACING)
        return True
