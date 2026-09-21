"""The ratings boards, and how they moved since they were last published."""

from __future__ import annotations

from datetime import date

import discord

from ..util import truncate
from .common import DASH, MEDALS, UFC_RED, add_chunked_fields, join, keep, stamp

ARROWS = {"entered": "🆕", "left": "🚪", "up": "🔼", "down": "🔽"}


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
        # A shared rank keeps its number but loses the medal: a joint first is
        # not a winner, and two of the same medal reads as a mistake.
        badge = f"`={entry.rank:>2}`" if entry.tied else MEDALS.get(entry.rank, f"`{entry.rank:>2}`")
        facts = [f"**{entry.rating}**", entry.record]
        if pound_for_pound and entry.division:
            facts.append(entry.division)
        lines.append(f"{badge} {keep(entry.name)} · {join(facts)}")

    add_chunked_fields(embed, "Ratings", lines)
    if note:
        embed.add_field(
            name="About these ratings",
            value=(
                "The bot's own rating, not the UFC's ranking. Everyone starts level and a "
                "win moves it by how good the fighter beaten was, so beating a contender is "
                "worth more than beating a debutant. Nobody votes and a belt counts for "
                "nothing by itself, which is why champions often sit below contenders "
                "here.\n\n"
                "**=** marks a shared rank: ratings a few points apart are a tie, not an "
                "order.\n\n"
                "Ranked here: three or more UFC fights, and a fight in the last eighteen "
                "months. After a year out a rating fades — halving what a fighter holds over "
                "the starting rating for every further year — so a number nobody is "
                "defending stops outranking the fighters competing for it.\n\n"
                "Records are UFC fights only. A fighter's division is wherever they last "
                "fought, and the rating travels with them."
            ),
            inline=False,
        )
    return stamp(embed)

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
