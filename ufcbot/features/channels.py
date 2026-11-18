"""Maintains the bot's channel boards: picks, schedule, ratings and pick'em.

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
    fight_day_embed,
    pickem_board_embed,
    pickem_leaderboard_embed,
    picks_board_embed,
    rankings_embed,
    schedule_embed,
    scorecard_embed,
)
from ..models import Event
from ..records import BoardSlot, GuildSettings
from ..sources.espn import UFCData
from ..stats.prediction import Evaluation, Prediction
from ..stats.rankings import (
    POUND_FOR_POUND,
    all_time,
    divisions_with_fighters,
    rank_division,
)
from ..storage import Storage
from ..ui.pickem import board_view, my_picks_view
from ..util import normalise
from .pickem import (
    OPEN,
    PickemService,
    benchmarks,
    bout_status,
)
from .timing import estimate_first_bells, fight_clocks
from .tracking import PredictionTracker, build_scorecard

log = logging.getLogger(__name__)

KIND_PICKS = "picks"
KIND_SCHEDULE = "schedule"
KIND_SCORECARD = "scorecard"
KIND_PICKEM = "pickem"
KIND_PICKEM_LEADERBOARD = "pickem_leaderboard"
KIND_PICKEM_CARD_LEADERBOARD = "pickem_card_leaderboard"
KIND_FIGHT_DAY = "fight_day"
KIND_RANKINGS = "rankings"
BOARD_KEY = "board"

# Bot messages in the pick'em channel with these titles are pick'em posts; any
# that aren't the current board or the leaderboard are leftovers to delete.
PICKEM_TITLES = ("🎯 Pick'em:", "🏆")

THIS_CARD_TITLE = "🏆 This Card's Pick'em Leaderboard"
LAST_CARD_TITLE = "🏆 Last Card's Pick'em Leaderboard"

# Twelve hours before the main card, the pick'em channel says it is fight day.
FIGHT_DAY_NOTICE = timedelta(hours=12)


def card_leaderboard_title(scored_card_id: str | None, current: Event | None) -> str:
    """Whether the card leaderboard is about the card in play or the one before.

    The card it shows is whichever was scored most recently, so the title turns
    over without anything having to drive it: it reads "this card" from the
    moment the first fight of the card being fought is graded, and goes back to
    "last card" when the next card's board goes up with nothing scored on it.
    """
    in_play = scored_card_id is not None and current is not None and scored_card_id == current.id
    return THIS_CARD_TITLE if in_play else LAST_CARD_TITLE

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
class _Board:
    """One board the picks channel should show, for putting in its place."""

    kind: str
    key: str
    starts_at: datetime | None
    embed: discord.Embed
    signature: str
    expired: bool = False
    """Past its lifetime: shown once more where it belongs, then let go."""


def _board(
    kind: str, key: str, starts_at: datetime | None, embed: discord.Embed, *, expired: bool = False
) -> _Board:
    embed.set_footer(text="Last updated")
    return _Board(kind, key, starts_at, embed, _signature(embed), expired)


@dataclass(slots=True)
class PublishResult:
    picks_boards: int = 0
    pickem_boards: int = 0
    rankings: int = 0
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

    async def publish_pickem(self, guild: discord.Guild, settings: GuildSettings) -> PublishResult:
        """Just the pick'em channel.

        For redrawing the shares after someone picks, where running the whole
        pass would rebuild five boards to change two percentages.
        """
        result = PublishResult()
        channel = self._channel(guild, settings.pickem_channel_id)
        if channel is not None:
            await self._publish_pickem(guild, channel, settings, result, False)
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
        """Every card's picks in date order, earliest at the top, and the model's
        scorecard at the foot.

        The scorecard belongs under the picks rather than in a channel of its
        own: it is the answer to "should I believe any of this", and that
        question is asked while reading the picks. It is the only thing the bot
        says about its own record; a recap under every graded card used to say
        the same thing a third time.

        The channel is kept in order by editing, never by deleting. Each message
        the bot keeps here is a slot that shows one board, and the board a slot
        shows can move: a card that belongs above the last one is one new message
        at the bottom and every board from its place down shifting one message
        lower. The scorecard is the last board, so it stays at the foot without
        being deleted and posted again each time a card arrives, which is what it
        used to do.

        Nothing here has buttons, so nothing is bound to a message rather than
        to its card. Reactions and threads are another matter: Discord keeps
        those on the message, so one left on a board would end up under a
        different card after a reorder.
        """
        now = datetime.now(UTC)
        await self.storage.forget_board_slots_outside(guild.id, channel.id)
        slots = await self._picks_slots(guild, channel)
        upcoming = await self._upcoming(settings)
        boards: dict[str, _Board] = {}

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
            # Fights the model cannot call are still fights on the card, and a
            # board that leaves them out is a board missing a bout with no hint
            # it was ever there. Debuts are the usual reason.
            picked = {record.bout_id for record in records}
            embed = picks_board_embed(
                event.name,
                event.start,
                records,
                locked=event.start <= now,
                espn_url=event.espn_url,
                unpicked=[
                    (position, bout.matchup)
                    for position, bout in enumerate(event.ordered_bouts())
                    if bout.has_opponents and bout.id not in picked
                ],
            )
            boards[event.id] = _board(KIND_PICKS, event.id, event.start, embed)
            result.picks_boards += 1

        # Cards in the channel that are not upcoming any more: usually ones that
        # have happened and are still picking up results, though a card can also
        # drop out of the upcoming list for a tick when ESPN is slow.
        kept = []
        for slot in slots:
            if slot.kind == KIND_PICKS and slot.key not in boards:
                records = await self.tracker.records_for(slot.key)
                if not records:
                    # Nothing left to draw it from. It keeps what it last showed
                    # and is no longer kept in order.
                    await self._release(guild, slot)
                    continue
                start = records[0].event_start
                embed = picks_board_embed(records[0].event_name, start, records, locked=start <= now)
                boards[slot.key] = _board(
                    KIND_PICKS, slot.key, start, embed, expired=now - start > PICKS_BOARD_LIFETIME
                )
                result.picks_boards += 1
            kept.append(slot)

        scorecard = _board(
            KIND_SCORECARD,
            BOARD_KEY,
            None,
            scorecard_embed(
                build_scorecard(settings.tracking_since, await self.tracker.graded_events(settings.tracking_since)),
                evaluation=self.evaluation_provider(),
            ),
        )
        order = sorted(boards.values(), key=lambda board: (board.starts_at, board.key)) + [scorecard]
        await self._arrange(guild, channel, order, kept, result, force)

    async def _picks_slots(self, guild: discord.Guild, channel: discord.TextChannel) -> list[BoardSlot]:
        """The messages kept in order here, taking in any board posted without a slot.

        That is every board on the first pass after this was deployed, which
        tracked one message per card; they are taken over as they are, and the
        arrangement that follows puts them in order by editing them. It is also
        whatever an older version of the bot posted if one ran in between -- a
        test deploy rolled back -- because every version writes down which
        message shows each board, and that record is read on every pass.
        """
        slots = await self.storage.board_slots(guild.id, channel.id)
        held = {slot.message_id for slot in slots}
        adopted = False
        for kind in (KIND_PICKS, KIND_SCORECARD):
            for key, post in (await self.storage.posts_of_kind(guild.id, kind)).items():
                if post.channel_id != channel.id or post.message_id in held:
                    continue
                starts_at = None
                if kind == KIND_PICKS:
                    records = await self.tracker.records_for(key)
                    starts_at = records[0].event_start if records else None
                await self.storage.save_board_slot(
                    guild.id, channel.id, post.message_id, kind, key, starts_at, post.signature
                )
                adopted = True
        return await self.storage.board_slots(guild.id, channel.id) if adopted else slots

    async def _arrange(
        self,
        guild: discord.Guild,
        channel: discord.TextChannel,
        order: list[_Board],
        slots: list[BoardSlot],
        result: PublishResult,
        force: bool,
        *,
        attempts: int = 2,
    ) -> None:
        """Show ``order`` top to bottom in this channel's slots, by editing them.

        One board more than there are messages is one new message, posted at the
        bottom, which is the only place Discord can put one. Then the messages
        already here are edited from the bottom up, so that while the boards
        shift down each of them is on screen somewhere throughout: a pass cut
        short leaves a board shown twice for a moment, never one missing, and
        the next pass finishes the job. A slot already showing its board is not
        touched, so a pass where nothing moved costs nothing. Writes are spaced
        to stay under Discord's per-channel edit limit.
        """
        if len(slots) > len(order):
            # More messages than boards, which this version never produces on
            # its own. One an older version replaced goes first -- it posts the
            # scorecard again under every new card -- recognisable because the
            # record of which message shows that board points somewhere else.
            # Anything still over is let go from the top, as it is.
            extra = len(slots) - len(order)
            shown_by = {}
            for kind in {slot.kind for slot in slots}:
                for key, post in (await self.storage.posts_of_kind(guild.id, kind)).items():
                    shown_by[(kind, key)] = post.message_id
            replaced = [
                slot for slot in slots
                if shown_by.get((slot.kind, slot.key), slot.message_id) != slot.message_id
            ][:extra]
            for slot in replaced:
                await self._release(guild, slot)
            slots = [slot for slot in slots if slot not in replaced]
            for slot in slots[: len(slots) - len(order)]:
                await self._release(guild, slot)
            slots = slots[len(slots) - len(order) :]

        for board in order[len(slots) :]:
            message = await channel.send(embed=board.embed)
            await self._hold(guild, channel, message.id, board)
            result.updated += 1
            await asyncio.sleep(WRITE_SPACING)

        for slot, board in reversed(list(zip(slots, order))):
            if not force and (slot.kind, slot.key, slot.signature) == (board.kind, board.key, board.signature):
                result.unchanged += 1
                continue
            try:
                await channel.get_partial_message(slot.message_id).edit(embed=board.embed)
            except discord.NotFound:
                # Somebody deleted it. Forgotten, and arranged again: the board it
                # held comes back at the bottom and the rest shift to make room.
                await self.storage.delete_board_slot(guild.id, slot.message_id)
                if attempts > 1:
                    fresh = await self.storage.board_slots(guild.id, channel.id)
                    await self._arrange(guild, channel, order, fresh, result, False, attempts=attempts - 1)
                return
            except discord.HTTPException as exc:
                log.warning(
                    "Could not update the %s board in #%s: HTTP %s, Discord code %s: %s",
                    board.kind, channel.name, exc.status, exc.code, exc.text,
                )
                result.errors.append(f"update {board.kind} in #{channel.name}: HTTP {exc.status} {exc.text}")
                continue
            await self._hold(guild, channel, slot.message_id, board)
            result.updated += 1
            await asyncio.sleep(WRITE_SPACING)

        # A card past its lifetime has been shown at the top, where its date puts
        # it, with its final results. It is let go there, the message left as it
        # is, so the channel stops redrawing it.
        expired = {board.key for board in order if board.expired}
        for slot in await self.storage.board_slots(guild.id, channel.id):
            if slot.kind != KIND_PICKS or slot.key not in expired:
                break
            await self._release(guild, slot)

    async def _hold(self, guild: discord.Guild, channel: discord.TextChannel, message_id: int, board: _Board) -> None:
        """Write down that this message now shows this board.

        The per-card record follows the board to its new message too, because
        other things look a card's message up by the card: pick'em's title
        links to it."""
        await self.storage.save_board_slot(
            guild.id, channel.id, message_id, board.kind, board.key, board.starts_at, board.signature
        )
        await self.storage.save_post(guild.id, board.kind, board.key, channel.id, message_id, board.signature)

    async def _release(self, guild: discord.Guild, slot: BoardSlot) -> None:
        """Stop keeping a message in order, and leave it showing what it shows."""
        await self.storage.delete_board_slot(guild.id, slot.message_id)
        post = await self.storage.get_post(guild.id, slot.kind, slot.key)
        if post is not None and post.message_id == slot.message_id:
            await self.storage.delete_post(guild.id, slot.kind, slot.key)

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
                rankings_embed(
                    title,
                    entries,
                    pound_for_pound=p4p,
                    note=p4p,
                    all_time=all_time(ledgers, None if p4p else key, include_women=women),
                ),
                result,
                force=force,
            )
            result.rankings += 1

        # A division that has emptied out, or was renamed, leaves a board behind.
        keep = {name for name, _t, _p in wanted}
        for key in await self.storage.posts_of_kind(guild.id, KIND_RANKINGS):
            if key not in keep:
                await self._remove_post(guild, channel, KIND_RANKINGS, key, result)

    async def _publish_pickem(
        self,
        guild: discord.Guild,
        channel: discord.TextChannel,
        settings: GuildSettings,
        result: PublishResult,
        force: bool,
    ) -> None:
        """Two leaderboards that stay, and the current card's board beneath them.

        The leaderboards are never deleted, only edited. They are what the
        channel is for between cards, and a message that keeps its place is a
        message people can scroll back to.

        The board is the part that turns over: one per card, deleted and
        reposted as the card changes, so a card opening reads as something
        happening rather than as a message quietly changing under everyone.
        """
        now = datetime.now(UTC)
        current = await self.pickem.current_event()
        last = await self.storage.pickem_last_scored_card(guild.id)

        # Both leaderboards first, so a board posted below lands beneath them.
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
        await self._pickem_card_leaderboard(guild, channel, current, last, result, force)

        # One permanent board, following whichever card pick'em is on. It is
        # posted whatever the odds are doing: a fight with no price shows as
        # such and cannot be picked until it has one, which is a better answer
        # than an empty channel and a card nobody knew was open.
        removed = False
        for key in await self.storage.posts_of_kind(guild.id, KIND_PICKEM):
            # Earlier versions keyed the board by card. One left over from then
            # would otherwise sit below the new one for good.
            if key != BOARD_KEY:
                await self._remove_post(guild, channel, KIND_PICKEM, key, result)
                removed = True
        if current is not None:
            await self._pickem_board(guild, channel, current, result, force, now)
        if removed or force:
            await self._sweep_pickem(guild, channel, result)
        if current is not None:
            await self._fight_day_reminder(guild, channel, current, now, result)

    async def _fight_day_reminder(
        self,
        guild: discord.Guild,
        channel: discord.TextChannel,
        event: Event,
        now: datetime,
        result: PublishResult,
    ) -> None:
        """Twelve hours before the main card: it is today, go and pick.

        Timed off the main card rather than the first bell, because the early
        prelims can start four hours earlier and a reminder that lands at dawn
        is a reminder nobody reads.

        Taken down again the moment the first fight starts. By then it is no
        longer a reminder, it is a message telling people to do something they
        can no longer do, sitting above the board that matters.
        """
        key = str(event.id)
        main_card = event.main_card_start or event.start

        if event.start <= now:
            # The card has begun; picks on the early prelims are already closed.
            await self._remove_post(guild, channel, KIND_FIGHT_DAY, key, result)
            return
        if not timedelta(0) < main_card - now <= FIGHT_DAY_NOTICE:
            return
        if await self.storage.get_post(guild.id, KIND_FIGHT_DAY, key) is not None:
            return

        changed: list[tuple[str, str]] = []
        for row in await self.storage.short_notice_for(event.id):
            bout = next((b for b in event.fights if b.id == row.bout_id), None)
            if bout is None:
                continue  # the fight came off the card after the swap
            changed.append((bout.matchup, f"{row.arrived} replaced {row.departed}"))

        embed = fight_day_embed(
            event,
            changed=changed,
            players=await self.storage.pickem_player_count(guild.id, event.id),
        )
        try:
            message = await channel.send(
                content="@everyone",
                embed=embed,
                # The bot silences mentions everywhere else. This is the one
                # message whose whole point is to reach the room.
                allowed_mentions=discord.AllowedMentions(everyone=True),
            )
        except discord.HTTPException as exc:
            log.warning("Fight day reminder failed in #%s: %r", channel.name, exc)
            return
        # Remembered so it can be taken down when the first fight starts.
        await self.storage.save_post(
            guild.id, KIND_FIGHT_DAY, key, channel.id, message.id, signature="fight-day"
        )

    async def _pickem_card_leaderboard(
        self,
        guild: discord.Guild,
        channel: discord.TextChannel,
        current: Event | None,
        last: tuple[str, str, datetime] | None,
        result: PublishResult,
        force: bool,
    ) -> None:
        """One card's standings, and the button showing what you picked on it.

        Always the card scored most recently, so the title turns over on its
        own: "this card" from the moment the first fight of the card being
        fought is graded, and back to "last card" once the next card's board
        goes up with nothing scored on it yet.
        """
        if last is None:
            embed, view = pickem_leaderboard_embed(
                [], title=LAST_CARD_TITLE, empty="No card has been scored yet."
            ), None
        else:
            event_id, event_name, _start = last
            this_card = current is not None and event_id == current.id
            embed = pickem_leaderboard_embed(
                await self.storage.pickem_leaderboard(guild.id, event_id),
                title=card_leaderboard_title(event_id, current),
                subtitle=event_name,
                benchmarks=benchmarks(await self.storage.predictions_for_event(event_id)),
                empty="No results in yet." if this_card else "Nobody played this card.",
            )
            view = my_picks_view(event_id)

        result.pickem_boards += 1
        await self._upsert(
            guild,
            channel,
            KIND_PICKEM_CARD_LEADERBOARD,
            BOARD_KEY,
            embed,
            result,
            force=force,
            view=view,
            extra=embed.title or "",
        )

    async def _pickem_board(
        self,
        guild: discord.Guild,
        channel: discord.TextChannel,
        event: Event,
        result: PublishResult,
        force: bool,
        now: datetime,
    ) -> None:
        """The card's board, with the picker and the button showing your picks.

        One message for good, edited as the card turns over rather than deleted
        and reposted, so it keeps its place under the two leaderboards. The card
        id rides in the signature because a new card has to force the edit even
        where the text happens to look the same -- the buttons carry the card
        they belong to.
        """
        counts = await self.storage.pickem_counts(guild.id, event.id)
        players = await self.storage.pickem_player_count(guild.id, event.id)
        accepting = any(bout_status(event, bout, now) == OPEN for bout in event.fights)
        link = await self._picks_link(guild, event)
        result.pickem_boards += 1
        await self._upsert(
            guild,
            channel,
            KIND_PICKEM,
            BOARD_KEY,
            pickem_board_embed(
                event, counts, players, now=now, picks_link=link, starts=await self._fight_times(event, now)
            ),
            result,
            force=force,
            view=board_view(event.id, accepting=accepting),
            extra=f"{event.id}:accepting={accepting}:{link}",
        )

    async def _fight_times(self, event: Event, now: datetime) -> dict[str, datetime]:
        """Estimated first bells for the card's fights still to come.

        From the schedule before the card, and from the play-by-play once it is
        under way, so the times move with a card that runs early or late. Worth
        a board without them rather than no board, so a failure is logged and
        the board goes up as it is."""
        clocks = {}
        if self.data is not None and event.start <= now:
            try:
                clocks = await fight_clocks(self.data, event, now=now)
            except Exception:  # an estimate is never worth the board
                log.exception("Reading the fight times for %s failed", event.name)
        return estimate_first_bells(event, now=now, clocks=clocks)

    async def _picks_link(self, guild: discord.Guild, event: Event) -> str | None:
        """A jump link to this card's message on the picks board, if there is one.

        The picks board is published earlier in the same pass, so by the time
        pick'em is drawn the message it points at already exists. Where the picks
        channel is not set up there is nothing to point at and this is None.
        """
        post = await self.storage.get_post(guild.id, KIND_PICKS, event.id)
        if post is None:
            return None
        return f"https://discord.com/channels/{guild.id}/{post.channel_id}/{post.message_id}"

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
        for kind in (KIND_PICKEM_LEADERBOARD, KIND_PICKEM_CARD_LEADERBOARD):
            post = await self.storage.get_post(guild.id, kind, BOARD_KEY)
            if post:
                keep.add(post.message_id)
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
            if not force and post.signature == signature:
                result.unchanged += 1
                return False
            # A partial message edits without first fetching it, saving a request.
            message = channel.get_partial_message(post.message_id)
            try:
                await message.edit(embed=embed, **components)
                await self.storage.save_post(guild.id, kind, key, channel.id, post.message_id, signature)
                result.updated += 1
                await asyncio.sleep(WRITE_SPACING)
                return False
            except discord.NotFound:
                pass  # deleted by someone; send a fresh one below
            except discord.HTTPException as exc:
                # Sending a replacement when the old message is still there would
                # leave two of the same board behind. Said and counted as an error
                # instead, so the caller can try again rather than wait an hour.
                log.warning(
                    "Could not update the %s board in #%s: HTTP %s, Discord code %s: %s",
                    kind, channel.name, exc.status, exc.code, exc.text,
                )
                result.errors.append(f"update {kind} in #{channel.name}: HTTP {exc.status} {exc.text}")
                return False

        message = await channel.send(embed=embed, **components)
        await self.storage.save_post(guild.id, kind, key, channel.id, message.id, signature)
        result.updated += 1
        await asyncio.sleep(WRITE_SPACING)
        return True
