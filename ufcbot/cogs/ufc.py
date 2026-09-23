"""All slash commands, under a single ``/ufc`` group, plus the background jobs."""

from __future__ import annotations

import asyncio
import io
import logging
import math
import sys
from datetime import date, timedelta
from datetime import time as time_of_day
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from ..config import VALID_ANCHORS
from ..embeds import (
    UFC_RED,
    event_embed,
    fighter_embed,
    model_status_embed,
    pickem_picks_embed,
    pickem_stats_embed,
    prediction_embed,
    predictions_embed,
    stamp,
)
from ..embeds.common import plural
from ..features.sync import MissingPermissions
from ..models import Event
from ..records import GuildSettings
from ..stats.rankings import Ranked, pound_for_pound_rank, standing
from ..util import central_time, format_duration, resident_memory_mb, truncate

if TYPE_CHECKING:
    from ..bot import UFCBot

log = logging.getLogger(__name__)

# Live coverage bookkeeping is only needed while a card can still be interrupted
# by a restart; after this it is dead weight in the database.
LIVE_HISTORY = timedelta(days=14)

# The boards are rebuilt on the hour rather than on a timer from start-up, so
# "last updated" means the same thing whenever the bot was last restarted.
ON_THE_HOUR = [time_of_day(hour=hour) for hour in range(24)]

# Scheduled events are mirrored once a night, on Central time. A card being added
# to the calendar is days-ahead news, so the only thing the hour decides is that
# it does not arrive in the middle of a card.
MIDNIGHT_CENTRAL = [time_of_day(hour=0, tzinfo=central_time())]

# How long the newest card can be missing upstream before the bot says so. Two
# days is ordinary lateness; beyond that something is wrong worth knowing about.
STALE_DATA_AFTER = timedelta(days=4)

# A live post makes the boards wrong at once -- a winner named, a pick settled --
# so they are rebuilt there and then instead of waiting for the hour. Posts arrive
# in bursts, so a rebuild that comes too soon after the last one waits for the
# next tick rather than being dropped: the update always lands, at worst this
# long after the post that caused it.
LIVE_REFRESH_COOLDOWN = timedelta(seconds=45)


class UFCCog(commands.Cog):
    """UFC schedules, fight cards, fighter stats, predictions and event syncing."""

    ufc = app_commands.Group(
        name="ufc",
        description="UFC schedules, fight cards, fighters and predictions",
    )
    sync = app_commands.Group(
        name="sync",
        description="Mirror UFC cards into this server's scheduled events",
        parent=ufc,
        guild_only=True,
        default_permissions=discord.Permissions(manage_events=True),
    )
    model = app_commands.Group(
        name="model",
        description="The fight prediction model and its data",
        parent=ufc,
    )
    channels = app_commands.Group(
        name="channels",
        description="Auto-updating boards: picks, schedule and model accuracy",
        parent=ufc,
        guild_only=True,
        default_permissions=discord.Permissions(manage_guild=True),
    )
    pickem = app_commands.Group(
        name="pickem",
        description="Your pick'em record: points, win rate and card-by-card history",
        parent=ufc,
        guild_only=True,
    )

    def __init__(self, bot: UFCBot) -> None:
        self.bot = bot
    # -- lookups ------------------------------------------------------------

    @ufc.command(name="results", description="Show results from the most recent UFC card")
    @app_commands.describe(event="Optional event name; defaults to the last card")
    async def results(self, interaction: discord.Interaction, event: str | None = None) -> None:
        await interaction.response.defer()

        if event:
            found = await self.bot.data.find_event(event)
        else:
            recent = await self.bot.data.recent_events(days=60, limit=1)
            found = await self.bot.data.get_event(recent[0].id) if recent else None

        if found is None:
            await interaction.followup.send("No completed event found.")
            return
        await interaction.followup.send(embed=await self._card_embed(found, with_picks=False))

    @ufc.command(name="fighter", description="Fighter profile with exact ufcstats.com career numbers")
    @app_commands.describe(name="Fighter name, for example 'Alexa Grasso'")
    async def fighter(self, interaction: discord.Interaction, name: str) -> None:
        await interaction.response.defer()

        career = self.bot.stats.career(name) if self.bot.config.enable_predictions else None
        lookup = career.name if career else name

        profile = None
        matches = await self.bot.data.search_fighters(lookup, limit=1)
        if matches:
            profile = await self.bot.data.get_fighter(matches[0].id)
            # The dataset spelling can differ from ESPN's; retry with ESPN's name.
            if career is None and profile is not None and self.bot.config.enable_predictions:
                career = self.bot.stats.career(profile.display_name)

        if profile is None and career is None:
            await interaction.followup.send(self._not_found("fighter", name))
            return

        place, p4p = self._standing(career)
        await interaction.followup.send(
            embed=fighter_embed(profile, career, standing=place, pound_for_pound=p4p)
        )

    def _standing(self, career) -> tuple[Ranked | None, Ranked | None]:
        """Where this fighter sits in their division and pound for pound."""
        key = self.bot.stats.resolve(career.name) if career else None
        if key is None:
            return None, None
        ledgers = self.bot.ledgers()
        today = date.today()
        return standing(ledgers, key, on=today), pound_for_pound_rank(ledgers, key, on=today)

    @fighter.autocomplete("name")
    async def fighter_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        return self._fighter_choices(current)

    # -- predictions --------------------------------------------------------

    @ufc.command(name="predict", description="Predict a matchup between any two fighters")
    @app_commands.describe(
        fighter_a="First fighter",
        fighter_b="Second fighter",
        rounds="Scheduled rounds (3 or 5)",
        title="Is it a title fight?",
    )
    @app_commands.choices(rounds=[app_commands.Choice(name="3", value=3), app_commands.Choice(name="5", value=5)])
    async def predict(
        self,
        interaction: discord.Interaction,
        fighter_a: str,
        fighter_b: str,
        rounds: app_commands.Choice[int] | None = None,
        title: bool = False,
    ) -> None:
        if not self.bot.predictions_available:
            await interaction.response.send_message(self._model_unavailable(), ephemeral=True)
            return

        career_a = self.bot.stats.career(fighter_a)
        career_b = self.bot.stats.career(fighter_b)
        if career_a is None:
            await interaction.response.send_message(self._not_found("fighter", fighter_a), ephemeral=True)
            return
        if career_b is None:
            await interaction.response.send_message(self._not_found("fighter", fighter_b), ephemeral=True)
            return
        if career_a.name == career_b.name:
            await interaction.response.send_message("Pick two different fighters.", ephemeral=True)
            return

        scheduled = rounds.value if rounds else (5 if title else 3)
        prediction = self.bot.stats.predict(
            career_a.name, career_b.name, title_fight=title, scheduled_rounds=scheduled
        )
        if prediction is None:
            await interaction.response.send_message("Could not score that matchup.", ephemeral=True)
            return

        await interaction.response.defer()
        embed = prediction_embed(prediction, career_a, career_b)
        image = await self._matchup_image(career_a.name, career_b.name)
        if image:
            embed.set_image(url="attachment://matchup.jpg")
            await interaction.followup.send(embed=embed, file=discord.File(io.BytesIO(image), filename="matchup.jpg"))
        else:
            await interaction.followup.send(embed=embed)

    async def _matchup_image(self, name_a: str, name_b: str) -> bytes | None:
        """Side-by-side headshots, looked up on ESPN by name."""

        async def espn_fighter(name: str):
            hits = await self.bot.data.search_fighters(name, limit=1)
            return await self.bot.data.get_fighter(hits[0].id) if hits else None

        try:
            a, b = await asyncio.gather(espn_fighter(name_a), espn_fighter(name_b))
            if a is None or b is None:
                return None
            return await self.bot.images.matchup(a, b)
        except Exception as exc:  # the prediction still posts without art
            log.debug("Matchup image failed: %r", exc)
            return None

    @predict.autocomplete("fighter_a")
    @predict.autocomplete("fighter_b")
    async def predict_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        return self._fighter_choices(current)

    @ufc.command(name="predictions", description="Model picks for every fight on a card")
    @app_commands.describe(event="Event name; defaults to the next card")
    async def predictions(self, interaction: discord.Interaction, event: str | None = None) -> None:
        if not self.bot.predictions_available:
            await interaction.response.send_message(self._model_unavailable(), ephemeral=True)
            return
        await interaction.response.defer()

        found = await self.bot.data.find_event(event) if event else await self.bot.data.next_event()
        if found is None:
            await interaction.followup.send("No event found.")
            return

        await self.bot.data.load_odds(found)
        picks = self.bot.picks_for(found)
        if not picks:
            await interaction.followup.send(
                f"No picks for **{found.name}**: the fight card is not announced yet or the fighters are not in the data."
            )
            return

        await interaction.followup.send(embed=predictions_embed(found, picks))

    @predictions.autocomplete("event")
    async def predictions_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        return await self._event_choices(current)


    # -- pick'em ---------------------------------------------------------------

    @pickem.command(name="stats", description="Points, win rate and card-by-card history")
    @app_commands.describe(member="Whose stats to show; defaults to you")
    async def pickem_stats(self, interaction: discord.Interaction, member: discord.Member | None = None) -> None:
        target = member or interaction.user
        guild_id = interaction.guild_id or 0
        summary = await self.bot.storage.pickem_user_summary(guild_id, target.id)
        cards = await self.bot.storage.pickem_user_cards(guild_id, target.id)
        await interaction.response.send_message(embed=pickem_stats_embed(target, summary, cards))

    @pickem.command(name="picks", description="Everyone's picks for one card, fight by fight")
    @app_commands.describe(event="Card name, for example 'UFC 331'")
    async def pickem_picks(self, interaction: discord.Interaction, event: str) -> None:
        await interaction.response.defer()
        found = await self.bot.data.find_event(event)
        if found is None:
            await interaction.followup.send(f"No event matched **{truncate(event, 80)}**.", ephemeral=True)
            return
        picks = await self.bot.storage.pickem_card_picks(interaction.guild_id or 0, found.id)
        # A pick is nobody's business until its fight locks, or the card would be
        # a list of answers for whoever asks last.
        now = discord.utils.utcnow()
        visible = [pick for pick in picks if pick.locks_at <= now]
        embed = pickem_picks_embed(found.name, visible, hidden=len(picks) - len(visible))
        await interaction.followup.send(embed=embed)

    @pickem_picks.autocomplete("event")
    async def pickem_card_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        needle = current.casefold()
        names = await self.bot.storage.pickem_event_names(interaction.guild_id or 0)
        upcoming = [choice.value for choice in await self._event_choices(current)]
        merged = list(dict.fromkeys(upcoming + names))
        return [
            app_commands.Choice(name=truncate(name, 100), value=truncate(name, 100))
            for name in merged
            if needle in name.casefold()
        ][:25]

    # -- health -----------------------------------------------------------------

    @ufc.command(name="server", description="How the bot is doing: latency, uptime, memory and data")
    async def server(self, interaction: discord.Interaction) -> None:
        # Measured around the reply itself: the gateway heartbeat says how far
        # away Discord is, this says how long the bot took to answer.
        started = discord.utils.utcnow()
        await interaction.response.defer()

        gateway = self.bot.latency
        up = (discord.utils.utcnow() - self.bot.started_at).total_seconds()
        answered = (discord.utils.utcnow() - started).total_seconds() * 1000
        memory = resident_memory_mb()

        embed = discord.Embed(
            title=self.bot.user.display_name if self.bot.user else "Bot",
            description=f"Online for **{format_duration(up)}** \u00b7 since "
            f"{discord.utils.format_dt(self.bot.started_at, 'f')}",
            colour=UFC_RED,
        )
        if self.bot.user:
            embed.set_thumbnail(url=self.bot.user.display_avatar.url)

        embed.add_field(
            name="Connection",
            value="\n".join(
                [
                    "Gateway \u2014" if math.isnan(gateway) else f"Gateway **{gateway * 1000:.0f}ms**",
                    f"Response **{answered:.0f}ms**",
                    f"Serving {plural(len(self.bot.guilds), 'server')}",
                ]
            ),
            inline=True,
        )

        health = [f"Memory **{memory:,.0f} MB**" if memory is not None else "Memory \u2014"]
        stats = self.bot.stats
        if self.bot.config.enable_predictions:
            newest = stats.careers.newest_event if stats.careers else None
            if not stats.can_predict:
                health.append("Model **not loaded**")
            elif newest:
                health.append(f"Fights to **{newest:%b %d}**")
                health.append("Ratings **up to date**" if not stats.is_behind else "Ratings **behind**")
            else:
                health.append("Model **ready**")
        embed.add_field(name="Health", value="\n".join(health), inline=True)

        if "pandas" in sys.modules:
            # Training normally runs in a subprocess that exits and gives its
            # memory back. Where spawning is unavailable it falls back to this
            # process, and what it loads there stays for the life of the bot.
            embed.add_field(
                name="\u26a0\ufe0f Heads up",
                value="Training ran in this process. A restart gives back about 150 MB.",
                inline=False,
            )
        await interaction.followup.send(embed=stamp(embed))

    # -- channel boards ---------------------------------------------------------

    @channels.command(name="set", description="Choose channels for picks, accuracy, schedule and live fight coverage")
    @app_commands.describe(
        predictions="Channel for the picks board (one message per upcoming card)",
        accuracy="Channel for results recaps and the running scorecard",
        schedule="Channel for the upcoming-cards board",
        live="Channel for live coverage: fight previews, round stats and results",
        pickem="Channel for the pick'em game: the next card's board and the leaderboard",
        rankings="Channel for the ratings boards: one per division, plus pound for pound",
        womens_divisions="Include the women's divisions on the ratings boards (default yes)",
        since="Only count cards from this date (YYYY-MM-DD) in the scorecard",
    )
    async def channels_set(
        self,
        interaction: discord.Interaction,
        predictions: discord.TextChannel | None = None,
        accuracy: discord.TextChannel | None = None,
        schedule: discord.TextChannel | None = None,
        live: discord.TextChannel | None = None,
        pickem: discord.TextChannel | None = None,
        rankings: discord.TextChannel | None = None,
        womens_divisions: bool | None = None,
        since: str | None = None,
    ) -> None:
        if not any((predictions, accuracy, schedule, live, pickem, rankings, since)) and womens_divisions is None:
            await interaction.response.send_message(
                "Pass at least one of predictions, accuracy, schedule, live, pickem, rankings or since.",
                ephemeral=True,
            )
            return

        tracking_since = None
        if since:
            try:
                tracking_since = date.fromisoformat(since.strip())
            except ValueError:
                await interaction.response.send_message(
                    "`since` must look like 2026-09-19.", ephemeral=True
                )
                return

        settings = await self._settings(interaction.guild_id)
        if predictions is not None:
            settings.predictions_channel_id = predictions.id
        if accuracy is not None:
            settings.accuracy_channel_id = accuracy.id
        if schedule is not None:
            settings.schedule_channel_id = schedule.id
        if live is not None:
            settings.live_channel_id = live.id
        if pickem is not None:
            settings.pickem_channel_id = pickem.id
        if rankings is not None:
            settings.rankings_channel_id = rankings.id
        if womens_divisions is not None:
            settings.rankings_include_women = womens_divisions
        if tracking_since is not None:
            settings.tracking_since = tracking_since
        elif accuracy is not None and settings.tracking_since is None:
            # Start counting from now, so old cards never pollute the scorecard.
            settings.tracking_since = date.today()
        await self.bot.storage.save_settings(settings)

        await interaction.response.defer(ephemeral=True)
        result = await self.bot.publisher.publish(interaction.guild, settings, force=True)
        await interaction.followup.send(
            f"Saved. Posted {result.summary()}.\n{self._channels_summary(settings)}", ephemeral=True
        )

    @channels.command(name="clear", description="Stop maintaining all boards in this server")
    async def channels_clear(self, interaction: discord.Interaction) -> None:
        settings = await self._settings(interaction.guild_id)
        settings.predictions_channel_id = None
        settings.accuracy_channel_id = None
        settings.schedule_channel_id = None
        settings.live_channel_id = None
        settings.pickem_channel_id = None
        settings.rankings_channel_id = None
        await self.bot.storage.save_settings(settings)
        await interaction.response.send_message(
            "Boards cleared. Existing messages were left in place.", ephemeral=True
        )

    @channels.command(name="status", description="Which channels the boards post to")
    async def channels_status(self, interaction: discord.Interaction) -> None:
        settings = await self._settings(interaction.guild_id)
        await interaction.response.send_message(self._channels_summary(settings), ephemeral=True)

    @channels.command(name="refresh", description="Update every board in this server now")
    async def channels_refresh(self, interaction: discord.Interaction) -> None:
        settings = await self._settings(interaction.guild_id)
        if not settings.has_channels:
            await interaction.response.send_message(
                "No boards configured. Use `/ufc channels set` first.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True)
        await self.bot.tracker.grade_due()
        result = await self.bot.publisher.publish(interaction.guild, settings, force=True)
        await interaction.followup.send(f"Updated {result.summary()}.", ephemeral=True)

    def _channels_summary(self, settings: GuildSettings) -> str:
        def chan(channel_id: int | None) -> str:
            return f"<#{channel_id}>" if channel_id else "off"

        lines = [
            f"Picks board: {chan(settings.predictions_channel_id)}",
            f"Accuracy: {chan(settings.accuracy_channel_id)}",
            f"Schedule: {chan(settings.schedule_channel_id)}",
            f"Live fights: {chan(settings.live_channel_id)}",
            f"Pick'em: {chan(settings.pickem_channel_id)}",
            f"Ratings: {chan(settings.rankings_channel_id)}"
            + ("" if settings.rankings_include_women else " · men's divisions only"),
        ]
        if settings.tracking_since:
            lines.append(f"Scorecard counts cards from {settings.tracking_since:%b %d, %Y}")
        return "\n".join(lines)

    # -- model administration -------------------------------------------------

    @model.command(name="status", description="Dataset freshness and model accuracy")
    async def model_status(self, interaction: discord.Interaction) -> None:
        if not self.bot.config.enable_predictions:
            await interaction.response.send_message("Predictions are disabled in this bot's config.", ephemeral=True)
            return
        stats = self.bot.stats
        embed = model_status_embed(
            fight_count=stats.careers.fight_count if stats.careers else 0,
            newest_event=stats.careers.newest_event if stats.careers else None,
            behind=stats.expected_newest if stats.is_behind else None,
            model=stats.model,
            last_check=stats.last_check,
            last_error=stats.last_error,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @model.command(name="refresh", description="Pull the latest fight data and retrain (bot owner only)")
    async def model_refresh(self, interaction: discord.Interaction) -> None:
        if not await self.bot.is_owner(interaction.user):
            await interaction.response.send_message("Only the bot owner can do that.", ephemeral=True)
            return
        if not self.bot.config.enable_predictions:
            await interaction.response.send_message("Predictions are disabled in this bot's config.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        result = await self.bot.stats.refresh(force_retrain=True)
        await interaction.followup.send(result.message, ephemeral=True)

    # -- helpers --------------------------------------------------------------

    def _model_unavailable(self) -> str:
        if not self.bot.config.enable_predictions:
            return "Predictions are disabled in this bot's config."
        if self.bot.stats.last_error:
            return f"The model is not ready: {truncate(self.bot.stats.last_error, 200)}"
        return "The model is still downloading data and training. Try again in a few minutes."

    def _not_found(self, kind: str, query: str) -> str:
        message = f"No {kind} matched **{truncate(query, 80)}**."
        suggestions = self.bot.stats.suggest(query, 4) if self.bot.config.enable_predictions else []
        if suggestions:
            message += " Did you mean: " + ", ".join(f"**{s}**" for s in suggestions) + "?"
        return message

    def _fighter_choices(self, current: str) -> list[app_commands.Choice[str]]:
        if len(current) < 2 or not self.bot.stats.ready:
            return []
        try:
            names = self.bot.stats.suggest(current, 10)
        except Exception as exc:  # autocomplete must never raise
            log.debug("Fighter autocomplete failed: %r", exc)
            return []
        return [app_commands.Choice(name=truncate(n, 100), value=truncate(n, 100)) for n in names]

    async def _event_choices(self, current: str) -> list[app_commands.Choice[str]]:
        try:
            events = await self.bot.data.upcoming_events(days=270, limit=25)
        except Exception as exc:  # autocomplete must never raise
            log.debug("Autocomplete lookup failed: %r", exc)
            return []
        needle = current.casefold()
        return [
            app_commands.Choice(name=truncate(event.name, 100), value=truncate(event.name, 100))
            for event in events
            if needle in event.name.casefold()
        ][:25]

    async def _card_embed(self, event: Event, *, with_picks: bool = True) -> discord.Embed:
        """A fight card with the model's picks, current odds, and graded results where there are any."""
        records = {record.bout_id: record for record in await self.bot.tracker.records_for(event.id)}
        picks = {}
        if with_picks:
            await self.bot.data.load_odds(event)
            picks = self.bot.picks_for(event)
        return event_embed(event, picks=picks, records=records)

    # -- sync administration ------------------------------------------------

    @sync.command(name="enable", description="Turn on automatic Discord scheduled events")
    async def sync_enable(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        settings = await self._settings(interaction.guild_id)
        settings.sync_enabled = True
        await self.bot.storage.save_settings(settings)
        await self._run_and_report(interaction, settings, header="Sync enabled.")

    @sync.command(name="disable", description="Turn off automatic Discord scheduled events")
    async def sync_disable(self, interaction: discord.Interaction) -> None:
        settings = await self._settings(interaction.guild_id)
        settings.sync_enabled = False
        await self.bot.storage.save_settings(settings)
        await interaction.response.send_message(
            "Sync disabled. Existing scheduled events were left alone.", ephemeral=True
        )

    @sync.command(name="now", description="Run a sync straight away")
    async def sync_now(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        settings = await self._settings(interaction.guild_id)
        await self._run_and_report(interaction, settings)

    @sync.command(name="status", description="Show this server's sync settings")
    async def sync_status(self, interaction: discord.Interaction) -> None:
        settings = await self._settings(interaction.guild_id)
        links = await self.bot.storage.get_links(interaction.guild_id or 0)

        embed = discord.Embed(title="UFC sync settings", colour=UFC_RED)
        embed.add_field(name="Automatic sync", value="On" if settings.sync_enabled else "Off", inline=True)
        embed.add_field(name="Window", value=f"{settings.days_ahead} days", inline=True)
        embed.add_field(name="Duration", value=f"{settings.duration_minutes} min", inline=True)
        embed.add_field(
            name="Starts at",
            value="Main card" if settings.start_anchor == "main_card" else "Prelims",
            inline=True,
        )
        embed.add_field(
            name="Contender Series",
            value="Included" if settings.include_contender_series else "Excluded",
            inline=True,
        )
        embed.add_field(name="Events tracked", value=str(len(links)), inline=True)
        embed.add_field(
            name="Announcements",
            value=f"<#{settings.announce_channel_id}>" if settings.announce_channel_id else "Off",
            inline=True,
        )
        embed.add_field(name="Runs", value="Nightly at midnight Central", inline=True)
        await interaction.response.send_message(embed=stamp(embed), ephemeral=True)

    @sync.command(name="settings", description="Change how cards are mirrored into this server")
    @app_commands.describe(
        days_ahead="How far ahead to create events (1-365)",
        duration_minutes="How long each event lasts (30-1440)",
        starts_at="Whether the event starts at the main card or the prelims",
        include_contender_series="Include Dana White's Contender Series cards",
        announce_channel="Channel to post in when a new card is added; omit to leave unchanged",
    )
    @app_commands.choices(
        starts_at=[
            app_commands.Choice(name="Main card", value="main_card"),
            app_commands.Choice(name="Prelims", value="prelims"),
        ]
    )
    async def sync_settings(
        self,
        interaction: discord.Interaction,
        days_ahead: int | None = None,
        duration_minutes: int | None = None,
        starts_at: app_commands.Choice[str] | None = None,
        include_contender_series: bool | None = None,
        announce_channel: discord.TextChannel | None = None,
    ) -> None:
        settings = await self._settings(interaction.guild_id)

        if days_ahead is not None:
            settings.days_ahead = max(1, min(365, days_ahead))
        if duration_minutes is not None:
            settings.duration_minutes = max(30, min(1440, duration_minutes))
        if starts_at is not None and starts_at.value in VALID_ANCHORS:
            settings.start_anchor = starts_at.value
        if include_contender_series is not None:
            settings.include_contender_series = include_contender_series
        if announce_channel is not None:
            settings.announce_channel_id = announce_channel.id

        await self.bot.storage.save_settings(settings)
        await interaction.response.send_message(
            "Settings saved. Run `/ufc sync now` to apply them.", ephemeral=True
        )

    async def _settings(self, guild_id: int | None) -> GuildSettings:
        return await self.bot.storage.get_settings(guild_id or 0, self.bot.default_settings())

    async def _run_and_report(
        self,
        interaction: discord.Interaction,
        settings: GuildSettings,
        *,
        header: str | None = None,
    ) -> None:
        guild = interaction.guild
        if guild is None:
            await interaction.followup.send("This command only works inside a server.")
            return

        try:
            result = await self.bot.syncer.sync_guild(guild, settings)
        except MissingPermissions as exc:
            await interaction.followup.send(f"⚠️ {exc}")
            return
        except Exception as exc:  # surfaced to the user
            log.exception("Sync failed for guild %s", guild.id)
            await interaction.followup.send(f"Sync failed: {exc}")
            return

        await self.bot.syncer.announce_new_cards(guild, settings, result)

        lines = [header] if header else []
        lines.append(f"Scheduled events: {result.summary()}.")
        lines.extend(f"• {truncate(error, 200)}" for error in result.errors[:3])
        await interaction.followup.send("\n".join(lines))

async def setup(bot: UFCBot) -> None:
    await bot.add_cog(UFCCog(bot))
