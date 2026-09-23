"""What the bot says when something it depends on has stopped working.

These are the only posts that are about the bot rather than about fighting, so
they say plainly what has broken, what still works, and whether there is
anything for anyone to do. Most of the time the answer to the last one is no:
an upstream that has gone down comes back on its own.
"""

from __future__ import annotations

from datetime import date

import discord

from ..util import truncate
from .common import UFC_RED, stamp

WORKING_RED = discord.Colour.from_str("#2ECC71")


def stale_data_embed(expected: date, newest: date | None, last_error: str | None) -> discord.Embed:
    """That the fight dataset has stopped arriving, and what still works meanwhile.

    Said once per card that fails to land, in the same channel as the other
    news, because the boards go on looking authoritative while the numbers
    behind them quietly stop moving.
    """
    embed = discord.Embed(
        title="⚠️ Fight data is behind",
        colour=UFC_RED,
        description=(
            f"The results from **{expected:%b %d}** have not arrived, and the bot has been "
            "asking for them for days.\n\nRatings, picks and the model are still working, "
            "but they do not know about that card yet."
        ),
    )
    facts = [f"Newest fights held: {newest:%b %d, %Y}" if newest else "No fight data loaded"]
    if last_error:
        facts.append(f"Last attempt: {truncate(last_error, 200)}")
    facts.append("Nothing to do if upstream is simply late — it catches up on its own.")
    embed.add_field(name="Where it stands", value="\n".join(facts), inline=False)
    return stamp(embed)


def api_outage_embed(outage, *, odds_working: bool) -> discord.Embed:
    """That ESPN has stopped answering, after hours of it rather than a bad minute.

    Everything the bot shows about a card — who is fighting, when, who won —
    comes from one place, so this is the failure that stops most of it. It is
    said once per outage and only after the bot has spent hours getting nothing,
    because ESPN drops requests every day without anything being wrong.
    """
    embed = discord.Embed(
        title="🔌 Can't reach ESPN",
        colour=UFC_RED,
        description=(
            f"No answer for **{outage.hours:.0f} hours**, across {outage.failures:,} attempts "
            f"since {discord.utils.format_dt(outage.since, 'f')}.\n\n"
            "ESPN is where fight cards, fighters, results and live coverage come from, so "
            "those are frozen at whatever the bot last saw."
        ),
    )
    embed.add_field(
        name="Still working",
        value="\n".join(
            [
                "• Pick'em, points and both leaderboards",
                "• Ratings boards and fighter stats",
                "• " + ("Odds" if odds_working else "~~Odds~~"),
            ]
        ),
        inline=True,
    )
    embed.add_field(
        name="Not working",
        value="\n".join(["• Fight cards and the schedule", "• Live coverage", "• Grading results"]),
        inline=True,
    )

    # Whether the trouble is at their end or this one is the first thing worth
    # knowing, and a second host answering settles it.
    verdict = (
        "Another site is answering normally, so this is ESPN rather than the bot's connection."
        if odds_working
        else "Nothing else is answering either, so this may be the bot's own connection."
    )
    if outage.last_error:
        verdict += f"\nLast error: {truncate(outage.last_error, 180)}"
    verdict += "\n\nNothing to do — the bot keeps trying and picks up where it left off."
    embed.add_field(name="What this looks like", value=verdict, inline=False)
    return stamp(embed)


def api_restored_embed(hours: float) -> discord.Embed:
    """That ESPN is answering again, so the warning can be forgotten."""
    return stamp(
        discord.Embed(
            title="✅ ESPN is back",
            colour=WORKING_RED,
            description=(
                f"Answering again after about **{hours:.0f} hours**. Cards, results and live "
                "coverage catch up on the next pass; nothing was lost."
            ),
        )
    )
