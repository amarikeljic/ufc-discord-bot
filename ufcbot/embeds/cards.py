"""Fight cards, the schedule, fighter profiles and Discord scheduled-event text."""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord

from ..models import Bout, Event, Fighter
from ..stats.prediction import Prediction
from ..util import truncate
from .common import (
    DASH,
    EMBED_BUDGET,
    FIELD_LIMIT,
    UFC_RED,
    add_chunked_fields,
    age,
    inches,
    is_nan,
    join,
    keep,
    num,
    pct,
    short_record,
    stamp,
    streak,
    surname,
)
from .picks import pick_value, result_lines

if TYPE_CHECKING:
    from ..stats.service import FighterCareer
    from ..storage import PredictionRecord

SCHEDULED_EVENT_NAME_LIMIT = 100
SCHEDULED_EVENT_DESCRIPTION_LIMIT = 1000
SCHEDULED_EVENT_LOCATION_LIMIT = 100
MAX_FIELDS = 25
MEDALS = {1: "🥇", 2: "🥈", 3: "🥉"}


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
                record.name_a,
                record.name_b,
                favourite=record.favourite,
                confidence=record.confidence,
                prediction=Prediction.from_dict(record.name_a, record.name_b, record.detail) if record.detail else None,
                odds_a=record.odds_a,
                odds_b=record.odds_b,
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
                a.display_name,
                b.display_name,
                favourite=favourite.display_name,
                confidence=pick.confidence,
                prediction=pick,
                odds_a=bout.odds.get(a.id),
                odds_b=bout.odds.get(b.id),
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


def rankings_embed(
    division: str, entries: list, *, pound_for_pound: bool = False, note: bool = False
) -> discord.Embed:
    """One division's ratings board. ``entries`` are ``Ranked`` records.

    ``note`` explains what the rating is. Only the last board posted carries it,
    so the channel says it once rather than a dozen times.
    """
    embed = discord.Embed(
        title=truncate(f"📈 {division}" if not pound_for_pound else "👑 Pound for pound", 256),
        colour=UFC_RED,
    )
    if not entries:
        embed.description = "Nobody ranked here yet."
        return stamp(embed)

    lines = []
    for entry in entries:
        badge = MEDALS.get(entry.rank, f"`{entry.rank:>2}`")
        facts = [f"**{entry.rating}**", entry.record]
        if pound_for_pound and entry.division:
            facts.append(entry.division)
        lines.append(f"{badge} {keep(entry.name)} · {join(facts)}")

    add_chunked_fields(embed, "Ratings", lines)
    if note:
        embed.add_field(
            name="About these ratings",
            value=(
                "The bot's own rating, not the UFC's ranking. Everyone starts level and a win "
                "moves it by how good the fighter beaten was, so beating a contender is worth "
                "more than beating a debutant. Nobody votes, a belt counts for nothing by "
                "itself, and a fighter arriving from another promotion starts level however "
                "good they already are.\n\nRanked here: three or more UFC fights and a fight "
                "in the last two years. A fighter's division is wherever they last fought, so "
                "a move up shows the week it happens."
            ),
            inline=False,
        )
    return stamp(embed)


ARROWS = {"entered": "🆕", "left": "🚪", "up": "🔼", "down": "🔽"}


def ratings_changes_embed(division: str, changes: list) -> discord.Embed:
    """How one division's board moved. ``changes`` are ``RatingChange`` records."""
    embed = discord.Embed(title=truncate(f"📊 {division} ratings", 256), colour=UFC_RED)

    lines = []
    for change in changes:
        mark = ARROWS.get(change.kind, "•")
        if change.kind == "entered":
            what = f"in at **{change.now}**"
        elif change.kind == "left":
            what = f"out, was **{change.was}**"
        else:
            what = f"**{change.was} → {change.now}**"
        facts = [what, f"after {change.reason}"]
        if change.rating is not None:
            facts.append(str(change.rating))
        lines.append(f"{mark} {keep(change.name)} · {join(facts)}")

    add_chunked_fields(embed, "Moves", lines or [DASH])
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


def fighter_embed(
    profile: Fighter | None,
    career: FighterCareer | None,
) -> discord.Embed:
    """Profile card: ESPN bio plus the exact ufcstats.com career numbers."""
    name = (career.name if career else None) or (profile.display_name if profile else "Unknown")
    if profile and profile.nickname:
        name = f'{name} "{profile.nickname}"'

    embed = discord.Embed(
        title=truncate(name, 256),
        url=(profile.profile_url if profile else None) or (career.info.ufcstats_url if career and career.info else None),
        colour=UFC_RED,
    )

    info = career.info if career else None
    ledger = career.ledger if career else None

    record_lines = []
    if profile and profile.record:
        record_lines.append(f"Pro **{profile.record}**")
    if ledger:
        record_lines.append(f"UFC **{ledger.record}**")
    if record_lines:
        embed.add_field(name="Record", value="\n".join(record_lines), inline=True)

    division = (profile.weight_class if profile else None) or (
        f"{info.weight_lb:.0f} lbs" if info and not is_nan(info.weight_lb) else None
    )
    if division:
        embed.add_field(name="Division", value=division, inline=True)

    if ledger and ledger.fights:
        embed.add_field(name="Streak", value=streak(ledger), inline=True)

    tape = []
    height = inches(info.height_in) if info else (profile.height if profile else None)
    reach = inches(info.reach_in) if info else (profile.reach if profile else None)
    stance = (info.stance if info else None) or (profile.stance if profile else None)
    years = age(info.dob) if info and info.dob else (str(profile.age) if profile and profile.age else None)
    if height and height != DASH:
        tape.append(f"Height {height}")
    if reach and reach != DASH:
        tape.append(f"Reach {reach}")
    if stance:
        tape.append(f"Stance {stance}")
    if years and years != DASH:
        tape.append(f"Age {years}")
    if profile and profile.citizenship:
        tape.append(f"From {profile.citizenship}")
    if tape:
        embed.add_field(name="Tale of the tape", value=join(tape), inline=False)

    if ledger and ledger.stat_fights:
        embed.add_field(
            name="Striking",
            value=(
                f"SLpM **{num(ledger.slpm)}** · Acc. **{pct(ledger.str_acc)}**\n"
                f"SApM **{num(ledger.sapm)}** · Def. **{pct(ledger.str_def)}**"
            ),
            inline=True,
        )
        embed.add_field(
            name="Grappling",
            value=(
                f"TD Avg. **{num(ledger.td_avg)}** · Acc. **{pct(ledger.td_acc)}**\n"
                f"TD Def. **{pct(ledger.td_def)}** · Sub. Avg. **{num(ledger.sub_avg, 1)}**"
            ),
            inline=True,
        )
        finishes = join([f"KO/TKO {ledger.wins_ko}", f"Sub {ledger.wins_sub}", f"Dec {ledger.wins_dec}"])
        if ledger.losses:
            finishes += "\nLosses: " + join([f"KO {ledger.losses_ko}", f"Sub {ledger.losses_sub}", f"Dec {ledger.losses_dec}"])
        embed.add_field(name="Wins by", value=finishes, inline=False)
        if ledger.last_fight:
            embed.add_field(name="Last fight", value=f"{ledger.last_fight:%b %d, %Y} · {ledger.last_result or DASH}", inline=True)
        embed.add_field(name="Fight time", value=f"{ledger.seconds / 60:.0f} min · {ledger.stat_fights} fights", inline=True)
    elif career is None:
        embed.add_field(
            name="UFC stats",
            value="No ufcstats.com record found. Debutants appear after their first fight.",
            inline=False,
        )

    if profile and profile.headshot_url:
        embed.set_thumbnail(url=profile.headshot_url)

    return stamp(embed)


# -- Discord scheduled events --------------------------------------------------------


def scheduled_event_name(event: Event) -> str:
    return truncate(event.name, SCHEDULED_EVENT_NAME_LIMIT)


def scheduled_event_location(event: Event) -> str:
    return truncate(event.location, SCHEDULED_EVENT_LOCATION_LIMIT)


def scheduled_event_description(event: Event, picks: dict[str, Prediction] | None = None) -> str:
    """Main card summary, trimmed to Discord's 1000 character limit."""
    lines: list[str] = []

    main_card = next((bouts for _segment, bouts in event.bouts_by_segment() if bouts), [])
    if main_card:
        lines.append("Main card:")
        for bout in main_card:
            entry = f"• {bout.matchup}"
            if bout.weight_class:
                entry += f" ({bout.weight_class})"
            pick = (picks or {}).get(bout.id)
            if pick is not None and bout.has_opponents:
                favourite = bout.fighters[0] if pick.prob_a >= 0.5 else bout.fighters[1]
                entry += f" · pick {surname(favourite.display_name)} {pick.confidence:.0%}"
            if len("\n".join(lines)) + len(entry) + 1 > SCHEDULED_EVENT_DESCRIPTION_LIMIT - 80:
                lines.append("• …")
                break
            lines.append(entry)

    if event.espn_url:
        lines.append("")
        lines.append(event.espn_url)

    return truncate("\n".join(lines).strip(), SCHEDULED_EVENT_DESCRIPTION_LIMIT)
