"""A fighter's profile: ESPN's bio, the ufcstats.com career numbers, the rating."""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord

from ..models import Fighter
from ..util import truncate
from .common import (
    DASH,
    UFC_RED,
    age,
    inches,
    is_nan,
    join,
    num,
    pct,
    stamp,
    streak,
)

if TYPE_CHECKING:
    from ..stats.rankings import Ranked
    from ..stats.service import FighterCareer


def _ordinal(number: int) -> str:
    """1 -> 1st, 2 -> 2nd, 13 -> 13th."""
    if 10 <= number % 100 <= 20:
        return f"{number}th"
    return f"{number}{ {1: 'st', 2: 'nd', 3: 'rd'}.get(number % 10, 'th') }"


def _place(entry: Ranked) -> str:
    """Where a fighter stands: "4th", or "joint 4th" when the rating is too close to call."""
    return f"joint {_ordinal(entry.rank)}" if entry.tied else _ordinal(entry.rank)

def fighter_embed(
    profile: Fighter | None,
    career: FighterCareer | None,
    *,
    standing: Ranked | None = None,
    pound_for_pound: Ranked | None = None,
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

    if ledger and ledger.fights:
        # The board's number, not the raw one: a rating faded by a long layoff
        # has to read the same here as it does where they are ranked.
        shown = standing or pound_for_pound
        rating = [f"**{shown.rating if shown else round(ledger.elo)}**"]
        if standing:
            rating.append(f"{_place(standing)} at {standing.division}")
        if pound_for_pound:
            rating.append(f"{_place(pound_for_pound)} P4P")
        embed.add_field(name="Bot Rating", value=join(rating), inline=True)

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
