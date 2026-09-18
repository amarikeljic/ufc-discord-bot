"""Watches upcoming fight cards and announces what changes on them.

Fights come off cards all the time: a withdrawal, a short-notice replacement, a
bout added late. None of that is in ESPN's play-by-play, which only covers a
card while it is being fought, so it is found by keeping the last card seen and
comparing it with the current one.

Every guess about a card having changed is made against a card that read
cleanly. A response missing a fight, or missing the fighters in one, says
nothing about the fight and is left alone until the next pass.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import discord

from ..embeds import card_changes_embed
from ..models import Event
from ..sources.espn import UFCData
from ..storage import CardBout, GuildSettings, Storage
from ..util import normalise

log = logging.getLogger(__name__)

ADDED = "added"
REMOVED = "removed"
REPLACED = "replaced"

# A card nothing has refreshed in this long has been and gone.
STATE_LIFETIME = timedelta(days=21)


@dataclass(slots=True)
class CardChange:
    """One difference between the card as it was and the card as it is."""

    kind: str
    matchup: str
    weight_class: str | None = None
    left: str | None = None
    """Who is no longer in the fight, for a replacement."""
    arrived: str | None = None
    """Who is in it instead."""
    opponent: str | None = None
    """The fighter who stayed."""


def _bout_state(event: Event) -> list[CardBout]:
    """The card in the form it is remembered, skipping fights still being named."""
    return [
        CardBout(
            bout_id=bout.id,
            fighters=tuple((f.id, f.display_name) for f in bout.fighters[:2]),
            weight_class=bout.weight_class,
        )
        for bout in event.ordered_bouts()
        if not bout.awaiting_names
    ]


def diff(
    previous: dict[str, CardBout], current: list[CardBout], unresolved: set[str] | None = None
) -> list[CardChange]:
    """What changed between two readings of the same card.

    ``unresolved`` are bouts the card still lists but whose fighters did not
    load. They are absent from ``current`` and must not read as cancelled.
    """
    changes: list[CardChange] = []
    seen = {bout.bout_id for bout in current} | (unresolved or set())

    for bout in current:
        was = previous.get(bout.bout_id)
        if was is None:
            changes.append(CardChange(ADDED, bout.matchup, bout.weight_class))
            continue
        gone = was.athletes - bout.athletes
        arrived = bout.athletes - was.athletes
        # One corner changing is a replacement. Both changing is a different
        # fight reusing the slot, which reads better as one off and one on.
        if len(gone) == 1 and len(arrived) == 1:
            stayed = next(iter(was.athletes & bout.athletes), None)
            changes.append(
                CardChange(
                    REPLACED,
                    bout.matchup,
                    bout.weight_class,
                    left=was.name_of(next(iter(gone))),
                    arrived=bout.name_of(next(iter(arrived))),
                    opponent=bout.name_of(stayed) if stayed else None,
                )
            )
        elif gone and arrived:
            changes.append(CardChange(REMOVED, was.matchup, was.weight_class))
            changes.append(CardChange(ADDED, bout.matchup, bout.weight_class))

    for bout_id, was in previous.items():
        if bout_id not in seen:
            changes.append(CardChange(REMOVED, was.matchup, was.weight_class))
    return changes


class CardWatch:
    """Finds card changes once, then tells every guild that wants to hear about them."""

    def __init__(self, data: UFCData, storage: Storage, *, days_ahead: int = 60) -> None:
        self.data = data
        self.storage = storage
        self.days_ahead = days_ahead

    async def poll(self) -> list[tuple[Event, list[CardChange]]]:
        """Compare every upcoming card with the last reading and record the new one.

        Runs once per cycle, not once per guild: the cards are the same for
        everyone, and whichever guild looked first would otherwise be the only
        one to see a change.
        """
        try:
            upcoming = await self.data.upcoming_events(days=self.days_ahead, limit=25)
        except Exception as exc:  # announcements are never worth a failed tick
            log.debug("Could not list cards to check for changes: %r", exc)
            return []

        found: list[tuple[Event, list[CardChange]]] = []
        for summary in upcoming:
            event = await self.data.get_event(summary.id)
            if event is None or event.partial or not event.bouts:
                continue
            current = _bout_state(event)
            if not current:
                continue

            previous = await self.storage.card_bouts(event.id)
            unresolved = {bout.id for bout in event.bouts if bout.awaiting_names}
            # A bout that did not resolve keeps the fighters it was last seen
            # with, so the next reading still has something to compare against.
            await self.storage.save_card_bouts(
                event.id, current + [previous[b] for b in unresolved if b in previous]
            )
            if not previous:
                # First time this card has been read; the whole card is not news.
                log.debug("Now watching %s for changes (%d fights)", event.name, len(current))
                continue

            changes = diff(previous, current, unresolved)
            if changes:
                log.info("%s changed: %s", event.name, ", ".join(c.kind for c in changes))
                found.append((event, changes))

        await self.storage.prune_card_bouts(datetime.now(UTC) - STATE_LIFETIME)
        return found

    async def announce(
        self,
        guild: discord.Guild,
        settings: GuildSettings,
        changes: list[tuple[Event, list[CardChange]]],
    ) -> int:
        """Post card changes to this guild's live channel, or its schedule channel."""
        if not changes:
            return 0
        channel = guild.get_channel(settings.live_channel_id or 0) or guild.get_channel(
            settings.schedule_channel_id or 0
        )
        if not isinstance(channel, discord.TextChannel):
            return 0
        permissions = channel.permissions_for(guild.me)
        if not (permissions.send_messages and permissions.embed_links):
            return 0

        posted = 0
        for event, event_changes in changes:
            if not settings.include_contender_series and "contender series" in normalise(event.name):
                continue
            try:
                await channel.send(embed=card_changes_embed(event, event_changes))
                posted += 1
            except discord.HTTPException as exc:
                log.warning("Card update failed in #%s: %r", channel.name, exc)
                return posted
        return posted
