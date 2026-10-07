"""A fighter's profile: the page ESPN leads with, the career numbers, the history."""

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
    keep,
    num,
    pct,
    stamp,
    streak,
)
from .common import (
    reach as reach_of,
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
    """Where a fighter stands on their division's board: "4th"."""
    return _ordinal(entry.rank)

def _header(profile, career) -> tuple[str, str | None]:
    """The name and the link the title points at."""
    name = (career.name if career else None) or (profile.display_name if profile else "Unknown")
    if profile and profile.nickname:
        name = f'{name} "{profile.nickname}"'
    url = (profile.profile_url if profile else None) or (
        career.info.ufcstats_url if career and career.info else None
    )
    return name, url


def _shell(profile, career) -> discord.Embed:
    embed = discord.Embed(title=truncate(_header(profile, career)[0], 256),
                          url=_header(profile, career)[1], colour=UFC_RED)
    if profile and profile.headshot_url:
        embed.set_thumbnail(url=profile.headshot_url)
    return embed


def fighter_embed(
    profile: Fighter | None,
    career: FighterCareer | None,
    *,
    standing: Ranked | None = None,
    pound_for_pound: Ranked | None = None,
) -> discord.Embed:
    """The page ESPN leads with, plus what the bot makes of them.

    Record first and both ways round: the professional one, which counts every
    fight anywhere, and the UFC one, which is all the ratings have ever seen.
    For a fighter who arrived with a career behind them those two numbers are
    very different, and the gap is the part the bot is blind to.
    """
    embed = _shell(profile, career)
    info = career.info if career else None
    ledger = career.ledger if career else None

    records = []
    if profile and profile.record:
        records.append(f"Pro **{profile.record}**")
    if ledger:
        records.append(f"UFC **{ledger.record}**")
    if records:
        embed.add_field(name="Record", value="\n".join(records), inline=True)

    division = (profile.weight_class if profile else None) or (
        f"{info.weight_lb:.0f} lbs" if info and not is_nan(info.weight_lb) else None
    )
    if division:
        embed.add_field(name="Division", value=division, inline=True)

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
    span = reach_of(info.reach_in) if info else (profile.reach if profile else None)
    stance = (info.stance if info else None) or (profile.stance if profile else None)
    years = age(info.dob) if info and info.dob else (str(profile.age) if profile and profile.age else None)
    if height and height != DASH:
        tape.append(f"Height {height}")
    if span and span != DASH:
        tape.append(f"Reach {span}")
    if stance:
        tape.append(f"Stance {stance}")
    if years and years != DASH:
        tape.append(f"Age {years}")
    if profile and profile.citizenship:
        tape.append(f"From {profile.citizenship}")
    if profile and profile.team:
        tape.append(f"Team {profile.team}")
    if tape:
        embed.add_field(name="Tale of the tape", value=join(tape), inline=False)

    if ledger and ledger.fights:
        line = [streak(ledger)]
        if ledger.last_fight:
            line.append(f"last out {ledger.last_fight:%b %Y}")
        embed.add_field(name="Form", value=join(line), inline=False)
    elif career is None:
        embed.add_field(
            name="UFC stats",
            value="No ufcstats.com record found. Debutants appear after their first fight.",
            inline=False,
        )
    return stamp(embed)


def fighter_stats_embed(profile: Fighter | None, career: FighterCareer | None) -> discord.Embed:
    """The ufcstats.com career numbers, which are UFC fights only.

    Said on the embed rather than left to be assumed: a fighter who went 13-1
    somewhere else shows none of it here, and the per-minute rates are over the
    UFC part of a career alone.
    """
    embed = _shell(profile, career)
    ledger = career.ledger if career else None
    if not (ledger and ledger.stat_fights):
        embed.description = "No ufcstats.com record found. Debutants appear after their first fight."
        return stamp(embed)

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
        finishes += "\nLosses: " + join(
            [f"KO {ledger.losses_ko}", f"Sub {ledger.losses_sub}", f"Dec {ledger.losses_dec}"]
        )
    embed.add_field(name="Wins by", value=finishes, inline=False)
    if ledger.last_fight:
        embed.add_field(
            name="Last fight",
            value=f"{ledger.last_fight:%b %d, %Y} · {ledger.last_result or DASH}",
            inline=True,
        )
    embed.add_field(
        name="Fight time",
        value=f"{ledger.seconds / 60:.0f} min · {ledger.stat_fights} fights",
        inline=True,
    )
    embed.set_footer(text="ufcstats.com · UFC fights only")
    return embed


# How many fights fit before Discord's 4096-character description runs out. The
# longest careers run past forty and the oldest of those are the least worth
# reading, so the list is cut at the top rather than split over pages.
HISTORY_SHOWN = 20

_RESULT_MARK = {"W": "🟩", "L": "🟥", "D": "🟨", "NC": "⬜"}


def fighter_history_embed(
    profile: Fighter | None,
    career: FighterCareer | None,
    history: list,
) -> discord.Embed:
    """Every fight ESPN has, newest first -- the whole career, not the UFC part.

    This is the one page that knows about the fights before the UFC, so a
    newcomer who arrived 13-1 reads as what they are rather than as a debutant.
    """
    embed = _shell(profile, career)
    if not history:
        embed.description = "No fight history found for this fighter."
        return stamp(embed)

    lines = []
    for bout in history[:HISTORY_SHOWN]:
        mark = _RESULT_MARK.get((bout.result or "").upper(), "▫️")
        when = f"{bout.on:%b %Y}" if bout.on else "—"
        facts = [keep(bout.opponent or "Unknown")]
        if bout.method:
            ending = bout.method
            if bout.rounds:
                ending += f" R{bout.rounds}"
            facts.append(ending)
        if bout.title_fight:
            facts.append("🏆")
        lines.append(f"{mark} `{when:>8}` {join(facts)}")

    embed.description = "\n".join(lines)
    shown, total = min(len(history), HISTORY_SHOWN), len(history)
    embed.set_footer(
        text=f"ESPN · {shown} of {total} fights"
        + ("" if shown == total else ", newest first")
    )
    return embed


# -- Discord scheduled events --------------------------------------------------------
