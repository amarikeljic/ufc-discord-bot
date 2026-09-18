"""Bot wiring: shared services, extension loading and command-tree registration."""

from __future__ import annotations

import logging
from pathlib import Path

import discord
from discord.ext import commands

from .config import Config
from .embeds.images import MatchupImages
from .features.cardwatch import CardWatch
from .features.channels import ChannelPublisher
from .features.live import LiveCoverage
from .features.pickem import PickemService
from .features.sync import EventSyncer
from .features.tracking import PredictionTracker
from .models import Event
from .sources.espn import UFCData
from .sources.http import HttpClient
from .sources.polymarket import PolymarketOdds
from .sources.posters import PosterLookup
from .stats.prediction import Evaluation, Prediction
from .stats.service import StatsService
from .storage import GuildSettings, Storage
from .ui.pickem import PICKEM_BUTTONS

log = logging.getLogger(__name__)

EXTENSIONS = ("ufcbot.cogs.ufc",)


class UFCBot(commands.Bot):
    def __init__(self, config: Config) -> None:
        # Slash commands only, so no privileged intents are needed.
        intents = discord.Intents.default()

        super().__init__(
            command_prefix=commands.when_mentioned,
            intents=intents,
            help_command=None,
            allowed_mentions=discord.AllowedMentions.none(),
            # Nothing here reads message history from the cache: the pick'em sweep
            # asks Discord for it, and everything else arrives as an interaction.
            # Keeping a thousand messages per shard is memory for nobody.
            max_messages=None,
        )

        self.config = config
        self.http_client = HttpClient(cache_ttl=config.cache_ttl_seconds)
        self.storage = Storage(config.database_path)
        self.data = UFCData(self.http_client, odds_fallback=PolymarketOdds(self.http_client))
        self.posters = PosterLookup(self.http_client)
        self.stats = StatsService(
            Path(config.data_dir),
            Path(config.model_dir),
            refresh_interval_hours=config.stats_refresh_hours,
        )
        self.syncer = EventSyncer(
            self.data,
            self.posters,
            self.storage,
            enable_poster_art=config.enable_poster_art,
            pick_provider=self.picks_for if config.enable_predictions else None,
        )
        self.tracker = PredictionTracker(self.storage, self.data)
        self.pickem = PickemService(self.data, self.storage)
        self.cardwatch = CardWatch(self.data, self.storage, days_ahead=config.default_days_ahead)
        self.publisher = ChannelPublisher(
            self.data,
            self.storage,
            self.tracker,
            self.pickem,
            pick_provider=self.picks_for,
            evaluation_provider=lambda: self.evaluation,
        )
        self.images = MatchupImages(self.http_client)
        self.live = LiveCoverage(
            self.data, self.storage, images=self.images, career_provider=self.stats.career
        )

    @property
    def predictions_available(self) -> bool:
        return self.config.enable_predictions and self.stats.can_predict

    @property
    def evaluation(self) -> Evaluation | None:
        """The model's held-out accuracy, when a model is loaded."""
        return self.stats.model.evaluation if self.stats.model else None

    def picks_for(self, event: Event) -> dict[str, Prediction]:
        """Model picks for a card, or nothing when predictions are off or not ready."""
        return self.stats.predict_event(event) if self.predictions_available else {}

    def default_settings(self) -> GuildSettings:
        """Settings applied to a guild that has never been configured."""
        return GuildSettings(
            guild_id=0,
            days_ahead=self.config.default_days_ahead,
            duration_minutes=self.config.default_duration_minutes,
            start_anchor=self.config.default_start_anchor,
        )

    async def setup_hook(self) -> None:
        await self.http_client.start()
        await self.storage.connect()
        # Stored picks follow the current scoring rules, even if they changed since.
        await self.pickem.apply_scoring()
        if self.config.enable_predictions:
            # Loads what is on disk; downloading and training happen in the
            # background loop so startup is never blocked.
            await self.stats.start()

        # Pick'em board buttons carry their card in the custom id and must keep
        # working on messages posted before this restart.
        self.add_dynamic_items(*PICKEM_BUTTONS)

        for extension in EXTENSIONS:
            await self.load_extension(extension)
            log.info("Loaded extension %s", extension)

        await self._register_commands()

    async def _register_commands(self) -> None:
        if self.config.dev_guild_ids:
            # Guild commands appear immediately, which makes development bearable.
            for guild_id in self.config.dev_guild_ids:
                guild = discord.Object(id=guild_id)
                self.tree.copy_global_to(guild=guild)
                synced = await self.tree.sync(guild=guild)
                log.info("Registered %d commands in guild %s", len(synced), guild_id)
        else:
            synced = await self.tree.sync()
            log.info("Registered %d global commands (may take up to an hour to appear)", len(synced))

    async def on_ready(self) -> None:
        log.info("Signed in as %s (%s), in %d servers", self.user, self.user.id, len(self.guilds))
        await self.change_presence(
            activity=discord.Activity(type=discord.ActivityType.watching, name="the octagon")
        )

    async def on_guild_remove(self, guild: discord.Guild) -> None:
        # Drop stored state for servers the bot is no longer in.
        await self.storage.delete_guild(guild.id)
        log.info("Removed stored data for guild %s", guild.id)

    async def close(self) -> None:
        await super().close()
        await self.storage.close()
        await self.http_client.close()
