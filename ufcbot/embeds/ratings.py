"""The ratings boards, and how they moved since they were last published."""

from __future__ import annotations

import discord

from ..util import truncate
from .common import DASH, UFC_RED, add_chunked_fields, join, keep, plural, stamp

ARROWS = {"entered": "🆕", "left": "🚪", "up": "🔼", "down": "🔽"}



def _rating_lines(entries: list, *, with_division: bool, career: bool = False) -> list[str]:
    """One line per fighter.

    ``career`` prints the all-time boards, whose number is a career score rather
    than a rating -- a peak plus what the fighter won with it. Printing the
    defences alongside is what stops it reading as the same quantity as the
    board above, where it would look wrong: a three-fight peak can sit below the
    single rating a fighter carries today.
    """
    lines = []
    for entry in entries:
        # A shared rank keeps its number but loses the medal: a joint first is
        # not a winner, and two of the same medal reads as a mistake.
        # Every badge is the same three characters wide, so every name starts in
        # the same column. A medal is a different width from a number and a
        # shared rank is a character wider again, which is what pushed the
        # names out of line.
        badge = f"`{'=' if entry.tied else ' '}{entry.rank:>2}`"
        facts = [f"**{entry.rating}**"]
        # The record and the division are alternatives rather than both. A
        # divisional board has no division to give, so the record is the context
        # there; pound for pound the division is, and carrying both put the
        # all-time list over the 1024 characters Discord allows in one field --
        # which it does not refuse, it just splits, leaving a gap through the
        # middle of a ranking.
        if career and with_division:
            facts.append(entry.division or entry.record)
        else:
            facts.append(entry.record)
            if with_division and entry.division:
                facts.append(entry.division)
        # The belt is shown rather than ranked on. It is the one thing a reader
        # already knows and will look for, and its absence next to the top name
        # is the question the board was getting asked.
        belt = " 🏆" if entry.champion else (" 🎖️" if entry.former_champion else "")
        if career and entry.defences:
            facts.insert(1, plural(entry.defences, "defence"))

        lines.append(f"{badge} {keep(entry.name)}{belt} · {join(facts)}")
    return lines


def rankings_embed(
    division: str,
    entries: list,
    *,
    pound_for_pound: bool = False,
    note: bool = False,
    all_time: list | None = None,
) -> discord.Embed:
    """One division's ratings board. ``entries`` are ``Ranked`` records.

    ``all_time`` is the same division judged over the whole history of the UFC,
    printed beneath. The board above it is about who is best now, so it hides
    anyone who has stopped fighting; this one is about who was ever best, so it
    hides nobody. Most of the names people argue about are only on this one.

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

    add_chunked_fields(
        embed, "Current Ratings", _rating_lines(entries, with_division=pound_for_pound)
    )
    if all_time:
        add_chunked_fields(
            embed,
            "🐐 All Time Ratings",
            _rating_lines(all_time, with_division=pound_for_pound, career=True),
        )
    if note:
        # Chunked rather than added whole: the explanation covers two boards now
        # and is past the 1024 characters Discord allows in one field, which it
        # rejects outright rather than truncating.
        add_chunked_fields(
            embed,
            "About these ratings",
            _paragraphs(
                "The bot's own rating, not the UFC's ranking. Everyone starts level and a "
                "win moves it by how good the fighter beaten was, so beating a contender is "
                "worth more than beating a debutant. Nobody votes, and a belt counts for "
                "nothing by itself, which is why a champion can sit below a contender here.\n\n"
                "🏆 holds the belt · 🎖️ held it once · **=** a shared rank, because ratings a "
                "few points apart are a tie rather than an order.\n\n"
                "Ranked here: three or more UFC fights, and a fight in the last eighteen "
                "months. After a year out a rating fades — halving what a fighter holds over "
                "the starting rating for every further year — so a number nobody is "
                "defending stops outranking the fighters competing for it.\n\n"
                "**🐐 All Time Ratings** asks a different question and scores it differently. "
                "Nothing "
                "fades and nobody is dropped for having retired, and instead of the rating a "
                "fighter carries now it uses the best they held across three fights, plus "
                "credit for every title they won and defended.\n\n"
                "A rating on its own cannot say *beat him three times*: it adds up results, "
                "so a longer career outscores a better one. And a career judged by the "
                "rating it ended on is judged by its decline. Five or more fights to qualify, "
                "and a fighter is listed in the division they fought in most rather than the "
                "one they finished in, or St-Pierre is a middleweight.\n\n"
                "Records are UFC fights only. On the current boards a fighter's division is "
                "wherever they last fought, and the rating travels with them."
            ),
        )
    return stamp(embed)


def _paragraphs(text: str) -> list[str]:
    """Blank-line-separated prose as lines, so a long note can be split in two.

    ``add_chunked_fields`` joins with single newlines, so the blank lines have to
    survive as entries of their own or the paragraphs run together.
    """
    paragraphs = text.split("\n\n")
    # The blank line rides on the front of the paragraph it precedes rather than
    # standing alone, or a split lands between them and leaves a field ending in
    # whitespace.
    return paragraphs[:1] + ["\n" + para for para in paragraphs[1:]]

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
