"""Mirrors UFC cards into Discord scheduled events."""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import discord

from ..embeds import (
    scheduled_event_description,
    scheduled_event_location,
    scheduled_event_name,
)
from ..models import Event
from ..sources.espn import UFCData
from ..sources.posters import PosterLookup
from ..storage import GuildSettings, Storage
from ..util import normalise

if TYPE_CHECKING:
    from ..stats.prediction import Prediction

log = logging.getLogger(__name__)

# Discord rejects a start time in the past, so leave a margin for clock skew
# and for the time this job takes to run.
START_MARGIN = timedelta(minutes=5)

# Small pause between writes so a first run over a full calendar does not
# hammer the guild's scheduled-event rate limit.
WRITE_DELAY = 1.0


@dataclass(slots=True)
class SyncResult:
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    skipped: int = 0
    pruned: int = 0
    errors: list[str] = field(default_factory=list)
    # Cards that got a brand new Discord event this run, for announcements.
    new_events: list[Event] = field(default_factory=list)

    def summary(self) -> str:
        parts = [
            f"{self.created} created",
            f"{self.updated} updated",
            f"{self.unchanged} unchanged",
        ]
        if self.skipped:
            parts.append(f"{self.skipped} skipped")
        if self.pruned:
            parts.append(f"{self.pruned} pruned")
        if self.errors:
            parts.append(f"{len(self.errors)} failed")
        return ", ".join(parts)


class MissingPermissions(Exception):
    """The bot cannot manage scheduled events in this guild."""


class EventSyncer:
    def __init__(
        self,
        data: UFCData,
        posters: PosterLookup,
        storage: Storage,
        *,
        enable_poster_art: bool = True,
        pick_provider: Callable[[Event], dict[str, Prediction]] | None = None,
    ) -> None:
        self.data = data
        self.posters = posters
        self.storage = storage
        self.enable_poster_art = enable_poster_art
        # Optional: returns model picks keyed by bout id, shown in event descriptions.
        self.pick_provider = pick_provider

    async def collect_events(self, settings: GuildSettings) -> list[Event]:
        """Upcoming cards for a guild, filtered and fully resolved."""
        summaries = await self.data.upcoming_events(days=settings.days_ahead, limit=40)

        wanted = [
            summary
            for summary in summaries
            if settings.include_contender_series
            or "contender series" not in normalise(summary.name)
        ]

        detailed = await asyncio.gather(
            *(self.data.get_event(summary.id) for summary in wanted), return_exceptions=True
        )

        events: list[Event] = []
        for summary, result in zip(wanted, detailed):
            if isinstance(result, Event):
                events.append(result)
            else:
                # The card list is still useful without the full fight card.
                log.debug("Falling back to summary for %s: %r", summary.name, result)
                events.append(summary)

        events.sort(key=lambda e: e.start)
        return events

    async def sync_guild(self, guild: discord.Guild, settings: GuildSettings) -> SyncResult:
        """Create or update one Discord scheduled event per upcoming UFC card."""
        me = guild.me
        if me is None or not me.guild_permissions.manage_events:
            raise MissingPermissions(
                "I need the Manage Events permission in this server to create scheduled events."
            )

        result = SyncResult()
        events = await self.collect_events(settings)
        links = await self.storage.get_links(guild.id)

        try:
            existing = {event.id: event for event in await guild.fetch_scheduled_events()}
        except discord.HTTPException as exc:
            raise MissingPermissions(f"Could not read this server's scheduled events: {exc}") from exc

        now = datetime.now(UTC)
        seen: set[str] = set()

        for event in events:
            seen.add(event.id)
            start = event.start_for(settings.start_anchor)
            end = start + timedelta(minutes=settings.duration_minutes)

            if start <= now + START_MARGIN:
                # Already under way or over; Discord will not accept it.
                result.skipped += 1
                continue

            picks = self.pick_provider(event) if self.pick_provider else None
            payload = _EventPayload(
                name=scheduled_event_name(event),
                description=scheduled_event_description(event, picks),
                location=scheduled_event_location(event),
                start=start,
                end=end,
            )

            link = links.get(event.id)
            scheduled = existing.get(link[0]) if link else None

            try:
                if scheduled is None:
                    await self._create(guild, event, payload)
                    result.created += 1
                    result.new_events.append(event)
                    await asyncio.sleep(WRITE_DELAY)
                elif link is not None and link[1] != payload.signature:
                    if scheduled.status is not discord.EventStatus.scheduled:
                        # Cannot rewrite an event that has started or finished.
                        result.skipped += 1
                        continue
                    await self._update(scheduled, payload)
                    result.updated += 1
                    await asyncio.sleep(WRITE_DELAY)
                else:
                    result.unchanged += 1
                    continue
            except discord.Forbidden as exc:
                raise MissingPermissions(f"Discord refused the request: {exc}") from exc
            except discord.HTTPException as exc:
                log.warning("Sync failed for %s in guild %s: %r", event.name, guild.id, exc)
                result.errors.append(f"{event.name}: {exc}")
                continue

            await self.storage.save_link(guild.id, event.id, payload.discord_id, payload.signature)

        result.pruned = await self._prune(guild, links, existing, seen, now)
        return result

    async def _create(self, guild: discord.Guild, event: Event, payload: _EventPayload) -> None:
        image = None
        if self.enable_poster_art:
            image = await self.posters.poster_bytes(event)

        kwargs = {
            "name": payload.name,
            "description": payload.description,
            "start_time": payload.start,
            "end_time": payload.end,
            "entity_type": discord.EntityType.external,
            "privacy_level": discord.PrivacyLevel.guild_only,
            "location": payload.location,
            "reason": "UFC event sync",
        }
        if image:
            kwargs["image"] = image

        created = await guild.create_scheduled_event(**kwargs)
        payload.discord_id = created.id
        log.info("Created scheduled event %r in guild %s", payload.name, guild.id)

    async def _update(self, scheduled: discord.ScheduledEvent, payload: _EventPayload) -> None:
        await scheduled.edit(
            name=payload.name,
            description=payload.description,
            start_time=payload.start,
            end_time=payload.end,
            location=payload.location,
            reason="UFC event sync",
        )
        payload.discord_id = scheduled.id
        log.info("Updated scheduled event %r", payload.name)

    async def _prune(
        self,
        guild: discord.Guild,
        links: dict[str, tuple[int, str]],
        existing: dict[int, discord.ScheduledEvent],
        seen: set[str],
        now: datetime,
    ) -> int:
        """Drop rows for cards that are gone from Discord and no longer upcoming.

        Scheduled events are never deleted here. A card dropping out of the
        window usually just means it has happened, and deleting the Discord
        event would erase whatever members had marked themselves interested in.
        """
        pruned = 0
        for espn_id, (discord_id, _signature) in links.items():
            if espn_id in seen:
                continue
            scheduled = existing.get(discord_id)
            if scheduled is None or (
                scheduled.start_time is not None and scheduled.start_time < now
            ):
                await self.storage.delete_link(guild.id, espn_id)
                pruned += 1
        return pruned


@dataclass(slots=True)
class _EventPayload:
    """The Discord-facing fields for one card, plus a signature to detect changes."""

    name: str
    description: str
    location: str
    start: datetime
    end: datetime
    discord_id: int = 0

    @property
    def signature(self) -> str:
        raw = "|".join(
            [
                self.name,
                self.description,
                self.location,
                self.start.isoformat(),
                self.end.isoformat(),
            ]
        )
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()
