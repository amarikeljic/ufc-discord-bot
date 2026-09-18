"""All slash commands, under a single ``/ufc`` group, plus the background jobs."""

from __future__ import annotations

import asyncio
import io
import logging
from datetime import date, timedelta
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands, tasks

from ..config import VALID_ANCHORS
from ..embeds import (
    UFC_RED,
    event_embed,
    fighter_embed,
    pickem_card_embed,
    pickem_stats_embed,
    prediction_embed,
    predictions_embed,
    scorecard_embed,
    stamp,
)
from ..features.sync import MissingPermissions, SyncResult
from ..models import Event
from ..storage import GuildSettings
from ..util import truncate

if TYPE_CHECKING:
    from ..bot import UFCBot

log = logging.getLogger(__name__)

# Live coverage bookkeeping is only needed while a card can still be interrupted
# by a restart; after this it is dead weight in the database.
LIVE_HISTORY = timedelta(days=14)


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

    async def cog_load(self) -> None:
        self.sync_loop.change_interval(minutes=self.bot.config.sync_interval_minutes)
        self.sync_loop.start()
        if self.bot.config.enable_predictions:
            self.stats_loop.start()
        self.channels_loop.start()
        self.live_loop.start()

    async def cog_unload(self) -> None:
        self.sync_loop.cancel()
        self.stats_loop.cancel()
        self.channels_loop.cancel()
        self.live_loop.cancel()

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

        await interaction.followup.send(embed=fighter_embed(profile, career))

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

    @ufc.command(name="scorecard", description="How the model's picks have done since tracking began")
    async def scorecard(self, interaction: discord.Interaction) -> None:
        settings = await self._settings(interaction.guild_id)
        card = await self.bot.tracker.scorecard(settings.tracking_since)
        embed = scorecard_embed(card, evaluation=self.bot.evaluation)
        if not card.total:
            embed.description = (
                "No graded picks yet. Picks lock when a card starts and are scored once results are in."
            )
        await interaction.response.send_message(embed=embed)

    # -- pick'em ---------------------------------------------------------------

    @pickem.command(name="stats", description="Points, win rate and card-by-card history")
    @app_commands.describe(member="Whose stats to show; defaults to you")
    async def pickem_stats(self, interaction: discord.Interaction, member: discord.Member | None = None) -> None:
        target = member or interaction.user
        guild_id = interaction.guild_id or 0
        summary = await self.bot.storage.pickem_user_summary(guild_id, target.id)
        cards = await self.bot.storage.pickem_user_cards(guild_id, target.id)
        await interaction.response.send_message(embed=pickem_stats_embed(target, summary, cards))

    @pickem.command(name="card", description="Pick-by-pick results for one card")
    @app_commands.describe(event="Card name, for example 'UFC 331'", member="Whose picks to show; defaults to you")
    async def pickem_card(
        self, interaction: discord.Interaction, event: str, member: discord.Member | None = None
    ) -> None:
        target = member or interaction.user
        is_self = target.id == interaction.user.id
        # Your own unlocked picks stay private; other members' unlocked picks stay hidden.
        await interaction.response.defer(ephemeral=is_self)
        found = await self.bot.data.find_event(event)
        if found is None:
            await interaction.followup.send(f"No event matched **{truncate(event, 80)}**.", ephemeral=True)
            return
        picks = await self.bot.storage.pickem_user_picks(interaction.guild_id or 0, target.id, found.id)
        now = discord.utils.utcnow()
        visible = picks if is_self else [pick for pick in picks if pick.locks_at <= now]
        embed = pickem_card_embed(target, found.name, visible, hidden=len(picks) - len(visible))
        await interaction.followup.send(embed=embed, ephemeral=is_self)

    @pickem_card.autocomplete("event")
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

    # -- channel boards ---------------------------------------------------------

    @channels.command(name="set", description="Choose channels for picks, accuracy, schedule and live fight coverage")
    @app_commands.describe(
        predictions="Channel for the picks board (one message per upcoming card)",
        accuracy="Channel for results recaps and the running scorecard",
        schedule="Channel for the upcoming-cards board",
        live="Channel for live coverage: fight previews, knockdowns, round stats and results",
        pickem="Channel for the pick'em game: the next card's board and the leaderboard",
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
        since: str | None = None,
    ) -> None:
        if not any((predictions, accuracy, schedule, live, pickem, since)):
            await interaction.response.send_message(
                "Pass at least one of predictions, accuracy, schedule, live, pickem or since.", ephemeral=True
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
        embed = discord.Embed(title="Prediction model", colour=UFC_RED)
        embed.description = "\n".join(self.bot.stats.status_lines())
        if self.bot.stats.model and self.bot.stats.model.importances:
            top = self.bot.stats.model.importances[:6]
            embed.add_field(
                name="What matters most",
                value="\n".join(f"• {name[2:].replace('_', ' ')}" for name, _ in top),
                inline=False,
            )
        stamp(embed)
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
        embed.add_field(name="Runs every", value=f"{self.bot.config.sync_interval_minutes} min", inline=True)
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

        await self._announce(guild, settings, result)

        lines = [header] if header else []
        lines.append(f"Scheduled events: {result.summary()}.")
        for error in result.errors[:3]:
            lines.append(f"• {truncate(error, 200)}")
        await interaction.followup.send("\n".join(lines))

    async def _announce(
        self, guild: discord.Guild, settings: GuildSettings, result: SyncResult
    ) -> None:
        """Post newly added cards to the configured channel, if there is one."""
        if not settings.announce_channel_id or not result.new_events:
            return

        channel = guild.get_channel(settings.announce_channel_id)
        if not isinstance(channel, discord.TextChannel):
            return
        if not channel.permissions_for(guild.me).send_messages:
            return

        for event in result.new_events[:5]:
            try:
                await channel.send(
                    content="📅 New UFC card on the calendar",
                    embed=event_embed(event, show_records=False, picks=self.bot.picks_for(event)),
                )
            except discord.HTTPException as exc:
                log.debug("Announcement failed in guild %s: %r", guild.id, exc)
                return

    # -- background jobs ----------------------------------------------------

    @tasks.loop(minutes=180)
    async def sync_loop(self) -> None:
        for settings in await self.bot.storage.guilds_with_sync_enabled():
            guild = self.bot.get_guild(settings.guild_id)
            if guild is None:
                continue
            try:
                result = await self.bot.syncer.sync_guild(guild, settings)
                if result.created or result.updated:
                    log.info("Synced guild %s: %s", guild.id, result.summary())
                await self._announce(guild, settings, result)
            except MissingPermissions as exc:
                log.warning("Skipping guild %s: %s", guild.id, exc)
            except Exception:  # one guild must not stop the rest
                log.exception("Background sync failed for guild %s", guild.id)

    @sync_loop.before_loop
    async def before_sync_loop(self) -> None:
        await self.bot.wait_until_ready()

    @tasks.loop(minutes=60)
    async def stats_loop(self) -> None:
        """Keep the dataset and model current.

        The most recent completed card on ESPN tells the service which event it
        should expect to find upstream; while that card is missing the service
        polls more often, since the dataset maintainer publishes the morning after.
        """
        stats = self.bot.stats
        try:
            recent = await self.bot.data.recent_events(days=21, limit=1)
            if recent:
                stats.expected_newest = recent[0].start.date()
        except Exception as exc:  # freshness hint is optional
            log.debug("Could not determine the latest completed card: %r", exc)

        if stats.needs_check():
            result = await stats.refresh()
            if result.downloaded or result.retrained:
                log.info("Stats refresh: %s", result.message)

    @stats_loop.before_loop
    async def before_stats_loop(self) -> None:
        await self.bot.wait_until_ready()

    @tasks.loop(minutes=20)
    async def channels_loop(self) -> None:
        """Grade finished cards, look for card changes, then refresh every board."""
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

        # Found once for everyone: whichever guild looked first would otherwise
        # be the only one told about a fight coming off a card.
        try:
            changes = await self.bot.cardwatch.poll()
        except Exception:  # announcements are never worth a failed tick
            log.exception("Checking cards for changes failed")
            changes = []

        for settings in await self.bot.storage.guilds_with_channels():
            guild = self.bot.get_guild(settings.guild_id)
            if guild is None:
                continue
            try:
                await self.bot.cardwatch.announce(guild, settings, changes)
            except Exception:
                log.exception("Announcing card changes failed in guild %s", guild.id)
            result = await self.bot.publisher.publish(guild, settings)
            if result.errors:
                log.warning("Boards in guild %s: %s", guild.id, "; ".join(result.errors))

    @channels_loop.before_loop
    async def before_channels_loop(self) -> None:
        await self.bot.wait_until_ready()

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
            await self.bot.live.tick(channels)
        except Exception:  # keep the loop alive through a bad tick
            log.exception("Live coverage tick failed")

    @live_loop.before_loop
    async def before_live_loop(self) -> None:
        await self.bot.wait_until_ready()


async def setup(bot: UFCBot) -> None:
    await bot.add_cog(UFCCog(bot))
