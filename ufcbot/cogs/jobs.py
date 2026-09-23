"""The background jobs: what the bot does when nobody has asked it anything.

Two loops. ``live_loop`` every fifteen seconds, which is cheap when no card is on
and is the only thing that runs during one. ``channels_loop`` on the hour, which
is the whole housekeeping pass in the order the steps depend on each other, and
which mirrors the calendar on the one pass a day that lands at midnight Central.

Anything that happens once a day belongs in that pass rather than in a loop of
its own: a loop whose whole job is to wake up, notice nothing has changed and go
back to sleep is a loop that only exists to be forgotten about.

Kept apart from the slash commands because the two answer to different things:
a command answers a person and returns, a job answers a clock and must survive
whatever it finds.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from datetime import time as time_of_day
from typing import TYPE_CHECKING

import discord
from discord.ext import commands, tasks

from ..embeds import api_outage_embed, api_restored_embed, stale_data_embed
from ..features.cardwatch import announcement_channel, news_channel
from ..features.sync import MissingPermissions
from ..stats.service import in_dataset
from ..util import central_time

if TYPE_CHECKING:
    from ..bot import UFCBot

log = logging.getLogger(__name__)

# Live coverage bookkeeping is only needed while a card can still be interrupted
# by a restart; after this it is dead weight in the database.
LIVE_HISTORY = timedelta(days=14)

# The boards are rebuilt on the hour rather than on a timer from start-up, so
# "last updated" means the same thing whenever the bot was last restarted.
ON_THE_HOUR = [time_of_day(hour=hour) for hour in range(24)]

# Scheduled events are mirrored once a night, on the hourly pass that lands at
# midnight Central. A card being added to the calendar is days-ahead news, so the
# only thing the hour decides is that it does not arrive in the middle of a card.
SYNC_HOUR_CENTRAL = 0

# How long the newest card can be missing upstream before the bot says so. Two
# days is ordinary lateness; beyond that something is wrong worth knowing about.
STALE_DATA_AFTER = timedelta(days=4)

# Where the fight data comes from. If this host stops answering, most of what
# the bot shows stops with it, and nothing else will say so.
ESPN_HOST = "sports.core.api.espn.com"
# A second host, used only as a control: if it is answering while ESPN is not,
# the trouble is at ESPN's end rather than with this machine's connection.
ODDS_HOST = "gamma-api.polymarket.com"

# A live post makes the boards wrong at once -- a winner named, a pick settled --
# so they are rebuilt there and then instead of waiting for the hour. Posts arrive
# in bursts, so a rebuild that comes too soon after the last one waits for the
# next tick rather than being dropped: the update always lands, at worst this
# long after the post that caused it.
LIVE_REFRESH_COOLDOWN = timedelta(seconds=45)


class JobsCog(commands.Cog):
    """Everything the bot does on a clock rather than on a command."""

    def __init__(self, bot: UFCBot) -> None:
        self.bot = bot
        self._last_live_refresh: datetime | None = None
        # Set when live coverage posts something, cleared once the boards have
        # caught up with it.
        self._boards_stale = False
        # The outage this cog has already warned about, so it says it once.
        self._reported_outage: str | None = None
        self._outage_hours = 0.0

    async def cog_load(self) -> None:
        self.channels_loop.start()
        self.live_loop.start()

    async def cog_unload(self) -> None:
        self.channels_loop.cancel()
        self.live_loop.cancel()

    async def _sync_calendar(self) -> None:
        """Mirror upcoming cards into each server's scheduled events, once a night.

        A step of the hourly pass rather than a loop of its own. All it does is
        notice cards that are new and put them on the calendar, which is
        days-ahead news; running it on the one pass a day that lands at midnight
        Central costs a comparison and saves a whole loop.
        """
        if datetime.now(central_time()).hour != SYNC_HOUR_CENTRAL:
            return
        for settings in await self.bot.storage.guilds_with_sync_enabled():
            guild = self.bot.get_guild(settings.guild_id)
            if guild is None:
                continue
            try:
                result = await self.bot.syncer.sync_guild(guild, settings)
                if result.created or result.updated:
                    log.info("Synced guild %s: %s", guild.id, result.summary())
                await self.bot.syncer.announce_new_cards(guild, settings, result)
            except MissingPermissions as exc:
                log.warning("Skipping guild %s: %s", guild.id, exc)
            except Exception:  # one guild must not stop the rest
                log.exception("Background sync failed for guild %s", guild.id)

    async def _refresh_stats(self) -> None:
        """Keep the dataset and model current.

        The most recent completed card on ESPN tells the service which event it
        should expect to find upstream, counting only the cards upstream carries.
        A Contender Series card is never coming, so waiting for one would have
        the bot call itself behind every Tuesday. While a card it does expect is
        missing the service checks on every pass instead of waiting: the
        maintainer publishes once a day, in one go, and the check itself costs
        four conditional requests.

        This runs first in the hourly pass so that a retrain's new ratings are
        announced and drawn by the rest of it, rather than a pass later.
        """
        if not self.bot.config.enable_predictions:
            return
        stats = self.bot.stats
        try:
            recent = await self.bot.data.recent_events(days=21, limit=8)
            expected = next((event for event in recent if in_dataset(event.name)), None)
            if expected is not None:
                stats.expected_newest = expected.start.date()
        except Exception as exc:  # freshness hint is optional
            log.debug("Could not determine the latest completed card: %r", exc)

        if not stats.needs_check():
            return
        result = await stats.refresh()
        if result.downloaded or result.retrained:
            log.info("Stats refresh: %s", result.message)

    @tasks.loop(time=ON_THE_HOUR)
    async def channels_loop(self) -> None:
        """The hourly pass, in the order the steps depend on each other.

        New data first, so everything below it describes the same fights: grade
        what has finished, tidy up, then say what changed and redraw the boards.
        One pass a day, the one at midnight Central, also mirrors the calendar.
        """
        try:
            await self._refresh_stats()
        except Exception:  # the rest of the pass still has work to do
            log.exception("Refreshing the fight dataset failed")
        try:
            await self._sync_calendar()
        except Exception:  # the boards do not depend on the calendar
            log.exception("Mirroring cards onto the calendar failed")
        try:
            graded = await self.bot.tracker.grade_due()
            if graded:
                log.info("Graded %d card(s)", len(graded))
        except Exception:  # boards can still refresh
            log.exception("Grading failed")
        try:
            await self.bot.pickem.grade_due()
        except Exception:  # boards can still refresh
            log.exception("Pick'em grading failed")
        try:
            await self.bot.storage.prune_live_data(discord.utils.utcnow() - LIVE_HISTORY)
        except Exception:  # housekeeping is never worth a failed tick
            log.exception("Pruning live coverage history failed")
        # Normally done the moment the last result posts; this catches a card
        # whose final result landed while the bot was not running.
        await self._close_finished_events()

        await self._announce_and_publish()

    async def _announce_and_publish(self) -> None:
        """Say what has changed since the last look, then redraw every board."""
        stale = await self._stale_notice()
        health = await self._api_health_notice()

        # Found once for everyone: whichever guild looked first would otherwise
        # be the only one told about a fight coming off a card.
        try:
            changes = await self.bot.cardwatch.poll()
        except Exception:  # announcements are never worth a failed tick
            log.exception("Checking cards for changes failed")
            changes = []
        try:
            moves = await self.bot.ratingswatch.poll(self.bot.ledgers())
        except Exception:
            log.exception("Checking the ratings boards for changes failed")
            moves = []

        for settings in await self.bot.storage.guilds_with_channels():
            guild = self.bot.get_guild(settings.guild_id)
            if guild is None:
                continue
            try:
                await self.bot.cardwatch.announce(guild, settings, changes)
                await self.bot.ratingswatch.announce(guild, settings, moves)
                if stale is not None:
                    channel = news_channel(guild, settings)
                    if channel is not None:
                        await channel.send(embed=stale)
                if health is not None:
                    channel = announcement_channel(guild, settings)
                    if channel is not None:
                        await channel.send(embed=health)
            except Exception:
                log.exception("Announcing changes failed in guild %s", guild.id)

        await self._publish_boards()

    async def _stale_notice(self) -> discord.Embed | None:
        """A warning when the dataset has stopped arriving, or None while it is fine.

        A broken refresh is invisible otherwise: the boards keep drawing happily
        from whatever was last downloaded. Said once per card that fails to
        land, so a genuinely dead pipeline does not become daily noise.
        """
        stats = self.bot.stats
        expected = stats.expected_newest
        if not self.bot.config.enable_predictions or not stats.is_behind or expected is None:
            return None
        if date.today() - expected < STALE_DATA_AFTER:
            return None  # upstream is often a day or two late; that is not news
        if not await self.bot.storage.notice_due("stale_data", expected.isoformat()):
            return None
        newest = stats.careers.newest_event if stats.careers else None
        return stale_data_embed(expected, newest, stats.last_error)

    async def _close_finished_events(self) -> None:
        """End the calendar entry for any card whose results are all in.

        Discord runs an external event to the end time it was given, which is a
        guess made days before the card. Left alone a card that finishes at ten
        sits live on the calendar until the small hours.
        """
        for settings in await self.bot.storage.guilds_with_sync_enabled():
            guild = self.bot.get_guild(settings.guild_id)
            if guild is None:
                continue
            try:
                ended = await self.bot.syncer.close_finished(guild)
                if ended:
                    log.info("Ended %d finished event(s) in guild %s", ended, guild.id)
            except Exception:  # the boards still have work to do
                log.exception("Closing finished events failed in guild %s", guild.id)

    async def _api_health_notice(self) -> discord.Embed | None:
        """A warning that ESPN has stopped answering, or the all-clear, or nothing.

        Deliberately slow to complain. ESPN drops requests every day, so this
        only fires once the client has spent hours getting nothing back across
        dozens of attempts, and a bot with no card to look at makes too few
        requests to ever reach that. The thresholds live in ``sources.http``.
        """
        http = self.bot.http_client
        outage = http.outage(ESPN_HOST)

        if outage is None:
            if self._reported_outage is None:
                return None
            # It is back, and the warning is still on screen saying otherwise.
            self._reported_outage = None
            return api_restored_embed(self._outage_hours)

        subject = outage.since.isoformat(timespec="minutes")
        if self._reported_outage == subject:
            return None
        if not await self.bot.storage.notice_due("espn_outage", subject):
            # Warned before a restart; do not say it twice.
            self._reported_outage = subject
            return None

        self._reported_outage = subject
        self._outage_hours = outage.hours
        log.warning("ESPN has not answered for %.1f hours (%d attempts)", outage.hours, outage.failures)
        return api_outage_embed(outage, odds_working=http.answering(ODDS_HOST))

    async def _publish_boards(self) -> None:
        """Bring every guild's boards in line with what the bot knows now."""
        for settings in await self.bot.storage.guilds_with_channels():
            guild = self.bot.get_guild(settings.guild_id)
            if guild is None:
                continue
            result = await self.bot.publisher.publish(guild, settings)
            if result.errors:
                log.warning("Boards in guild %s: %s", guild.id, "; ".join(result.errors))

    async def _refresh_after_live(self) -> None:
        """Redraw the boards once live coverage has posted something.

        A result names a winner and settles pick'em points, and both boards show
        it; on the hourly pass alone they would disagree with the live channel
        for most of an hour.

        Called on every tick while the boards are behind, not only on the tick
        that posted. A fight's last round and its result often post seconds
        apart, and the second of those is the one worth showing; holding the
        flag until the rebuild actually runs means it is never the one dropped.
        """
        now = discord.utils.utcnow()
        if self._last_live_refresh is not None and now - self._last_live_refresh < LIVE_REFRESH_COOLDOWN:
            return  # too soon; the next tick will pick this up
        self._last_live_refresh = now
        self._boards_stale = False
        try:
            await self.bot.pickem.grade_due()
        except Exception:  # the boards can still refresh
            log.exception("Pick'em grading after a live update failed")
        # A result may have been the last one on the card. Closing the calendar
        # entry here rather than on the hourly pass is the difference between
        # the event ending with the card and ending at midnight.
        await self._close_finished_events()
        try:
            await self._publish_boards()
        except Exception:  # never worth killing the live loop over
            log.exception("Refreshing the boards after a live update failed")

    @channels_loop.before_loop
    async def before_channels_loop(self) -> None:
        await self.bot.wait_until_ready()
        # A loop on a clock waits for the next hour before its first run, so a
        # restart at 3:05 would leave yesterday's boards up until 4:00.
        try:
            await self.channels_loop()
        except Exception:
            log.exception("The first board refresh after start-up failed")

    @tasks.loop(seconds=15)
    async def live_loop(self) -> None:
        """Post live fight updates while a card is on. Cheap when nothing is happening."""
        channels: list[discord.TextChannel] = []
        for settings in await self.bot.storage.guilds_with_live():
            guild = self.bot.get_guild(settings.guild_id)
            channel = guild.get_channel(settings.live_channel_id) if guild else None
            if not isinstance(channel, discord.TextChannel):
                continue
            permissions = channel.permissions_for(guild.me)
            if permissions.send_messages and permissions.embed_links:
                channels.append(channel)
        if not channels:
            return
        try:
            posted = await self.bot.live.tick(channels)
        except Exception:  # keep the loop alive through a bad tick
            log.exception("Live coverage tick failed")
            return
        self._boards_stale = self._boards_stale or bool(posted)
        if self._boards_stale:
            await self._refresh_after_live()

    @live_loop.before_loop
    async def before_live_loop(self) -> None:
        await self.bot.wait_until_ready()


async def setup(bot: UFCBot) -> None:
    await bot.add_cog(JobsCog(bot))
