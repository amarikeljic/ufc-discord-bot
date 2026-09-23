"""Fight cards, the schedule, fighter profiles and Discord scheduled-event text."""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord

from ..models import Bout, Event
from ..stats.prediction import Prediction
from ..util import truncate
from .common import (
    DASH,
    EMBED_BUDGET,
    FIELD_LIMIT,
    UFC_RED,
    add_chunked_fields,
    join,
    keep,
    short_record,
    stamp,
    surname,
)
from .picks import pick_value, result_lines

if TYPE_CHECKING:
    from ..records import PredictionRecord

MAX_FIELDS = 25

def bout_line(bout: Bout, *, show_records: bool = True, pick: Prediction | None = None) -> str:
    names = []
    for fighter in bout.fighters[:2]:
        record = short_record(fighter.record) if show_records else None
        names.append(f"**{fighter.display_name}**" + (f" ({record})" if record else ""))
    while len(names) < 2:
        names.append("**TBA**")

    line = f"{names[0]} vs. {names[1]}"
    tags = []
    if bout.weight_class:
        tags.append(bout.weight_class)
    if bout.is_championship_rounds:
        tags.append("5 rounds")
    if tags:
        line += f"\n *{join(tags)}*"

    if bout.completed and bout.winner_id:
        winner = bout.fighter(bout.winner_id)
        if winner:
            line += f"\n ✅ {winner.display_name}"
    elif pick is not None and bout.has_opponents:
        favourite = bout.fighters[0] if pick.prob_a >= 0.5 else bout.fighters[1]
        line += f"\n Pick: {keep(surname(favourite.display_name))} {pick.confidence:.0%}"
    return line

def _header_lines(event: Event) -> list[str]:
    lines = [f"🗓️ {discord.utils.format_dt(event.start, 'F')}", f"⏱️ {discord.utils.format_dt(event.start, 'R')}"]
    if event.main_card_start and event.main_card_start != event.start:
        lines.append(f"🥊 Main card {discord.utils.format_dt(event.main_card_start, 't')}")
    lines.append(f"📍 {event.location}")
    if event.broadcast:
        lines.append(f"📺 {event.broadcast}")
    return lines

def _segment_value(bouts: list[Bout], *, show_records: bool) -> str:
    lines: list[str] = []
    for bout in bouts:
        candidate = bout_line(bout, show_records=show_records)
        if sum(len(line) + 1 for line in lines) + len(candidate) > FIELD_LIMIT - 20:
            lines.append("…")
            break
        lines.append(candidate)
    return "\n".join(lines) or "To be announced"

def _bout_value(
    bout: Bout,
    *,
    show_records: bool,
    pick: Prediction | None,
    record: PredictionRecord | None,
    started: bool,
    compact: bool,
) -> str:
    """One fight with the model's view of it: the pick before, the verdict after."""
    a, b = bout.fighters[0], bout.fighters[1]
    lines = []
    if not compact:
        tags = [bout.weight_class, "5 rounds" if bout.is_championship_rounds else None]
        if any(tags):
            lines.append(f"*{join(tags)}*")
        if show_records and (a.record or b.record):
            lines.append(
                join(
                    [
                        f"{surname(a.display_name)} {short_record(a.record) or DASH}",
                        f"{surname(b.display_name)} {short_record(b.record) or DASH}",
                    ]
                )
            )

    winner = bout.fighter(bout.winner_id) if bout.completed and bout.winner_id else None

    # Once the card starts, show the pick that was locked in rather than a fresh one.
    if record is not None and (started or winner or pick is None):
        extra = result_lines(record)
        if winner and not extra:
            # Finished, but grading has not run yet.
            right = winner.id == record.favourite_athlete
            extra = [f"Winner: **{keep(winner.display_name)}**", "Pick ✅" if right else "Pick ❌"]
        lines.append(
            pick_value(
                favourite=record.favourite,
                confidence=record.confidence,
                prediction=Prediction.from_dict(record.name_a, record.name_b, record.detail) if record.detail else None,
                extra_lines=extra,
                compact=compact,
            )
        )
    elif winner:
        lines.append(f"✅ Winner: **{keep(winner.display_name)}**")
    elif pick is not None:
        favourite = a if pick.prob_a >= 0.5 else b
        lines.append(
            pick_value(
                favourite=favourite.display_name,
                confidence=pick.confidence,
                prediction=pick,
                extra_lines=[],
                compact=compact,
            )
        )
    else:
        lines.append("No pick: not enough UFC history")
    return truncate("\n".join(lines), FIELD_LIMIT)

def _card_with_picks(
    event: Event,
    *,
    show_records: bool,
    picks: dict[str, Prediction],
    records: dict[str, PredictionRecord],
    compact: bool,
) -> discord.Embed:
    embed = discord.Embed(title=truncate(event.name, 256), url=event.espn_url, colour=UFC_RED)

    lines = _header_lines(event)
    covered = sum(1 for bout in event.bouts if bout.id in picks or bout.id in records)
    lines.append(f"🤖 Model picks for {covered} of {len(event.bouts)} fights")
    embed.description = "\n".join(lines)

    started = event.start <= discord.utils.utcnow()
    for segment, bouts in event.bouts_by_segment():
        if not compact:
            embed.add_field(name=f"━━ {segment} ━━", value="​", inline=False)
        for bout in bouts:
            if not bout.has_opponents:
                continue
            embed.add_field(
                name=truncate(bout.matchup, 256),
                value=_bout_value(
                    bout,
                    show_records=show_records,
                    pick=picks.get(bout.id),
                    record=records.get(bout.id),
                    started=started,
                    compact=compact,
                ),
                inline=False,
            )

    if event.poster_url:
        embed.set_image(url=event.poster_url)

    return stamp(embed)

def _card_by_segment(event: Event, *, show_records: bool) -> discord.Embed:
    embed = discord.Embed(title=truncate(event.name, 256), url=event.espn_url, colour=UFC_RED)
    embed.description = "\n".join(_header_lines(event))

    for segment, bouts in event.bouts_by_segment():
        embed.add_field(name=segment, value=_segment_value(bouts, show_records=show_records), inline=False)
    if not event.bouts:
        embed.add_field(name="Fight card", value="Not announced yet.", inline=False)

    if event.poster_url:
        embed.set_image(url=event.poster_url)
    return stamp(embed)

def event_embed(
    event: Event,
    *,
    show_records: bool = True,
    picks: dict[str, Prediction] | None = None,
    records: dict[str, PredictionRecord] | None = None,
) -> discord.Embed:
    """Full card for one event.

    With model picks or recorded picks, every fight gets its own block showing
    the pick, likeliest method and odds, or the result and whether the pick was
    right. Without them, fights are listed compactly under each card segment.
    """
    picks = picks or {}
    records = records or {}
    if picks or records:
        for compact in (False, True):
            embed = _card_with_picks(
                event,
                show_records=show_records,
                picks=picks,
                records=records,
                compact=compact,
            )
            if len(embed) <= EMBED_BUDGET and len(embed.fields) <= MAX_FIELDS:
                return embed
    return _card_by_segment(event, show_records=show_records)

def card_changes_embed(event: Event, changes: list) -> discord.Embed:
    """What has changed on a card since the bot last looked.

    ``changes`` are ``CardChange`` records; the import stays out of here so the
    embed layer keeps depending on nothing above it.
    """
    embed = discord.Embed(
        title=truncate(f"🔄 Card update: {event.name}", 256), url=event.espn_url, colour=UFC_RED
    )
    header = [f"🗓️ {discord.utils.format_dt(event.start, 'D')} · {discord.utils.format_dt(event.start, 'R')}"]

    lines = []
    for change in changes:
        weight = f" *({change.weight_class})*" if change.weight_class else ""
        if change.kind == "replaced":
            against = f" against {keep(change.opponent)}" if change.opponent else ""
            lines.append(f"🔁 **{keep(change.arrived)}** replaces {keep(change.left)}{against}{weight}")
        elif change.kind == "removed":
            lines.append(f"🚫 Off the card — {change.matchup}{weight}")
        else:
            lines.append(f"➕ Added — {change.matchup}{weight}")

    embed.description = "\n".join(header)
    add_chunked_fields(embed, "Changes", lines or [DASH])
    return stamp(embed)

def schedule_embed(events: list[Event], *, title: str = "Upcoming UFC events") -> discord.Embed:
    embed = discord.Embed(title=title, colour=UFC_RED)

    if not events:
        embed.description = "No events found."
        return stamp(embed)

    for event in events[:25]:
        when = discord.utils.format_dt(event.start, "F")
        relative = discord.utils.format_dt(event.start, "R")
        embed.add_field(
            name=truncate(event.name, 256),
            value=f"{when}\n{relative} · 📍 {keep(event.short_location)}",
            inline=False,
        )

    return stamp(embed)
