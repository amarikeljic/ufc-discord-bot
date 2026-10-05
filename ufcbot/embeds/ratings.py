"""The ratings boards, and how they moved since they were last published."""

from __future__ import annotations

import discord

from ..util import truncate
from .common import (
    DASH,
    FIELD_LIMIT,
    UFC_RED,
    add_chunked_fields,
    join,
    keep,
    plural,
    stamp,
)

ARROWS = {"entered": "🆕", "left": "🚪", "up": "🔼", "down": "🔽", "returned": "↩️"}


# What a line gives up, in the order it gives it up, when fifteen of them will
# not fit in one field. Discord refuses the whole embed rather than the overflow,
# so a board that cannot be trimmed is a board that does not post -- and the
# alternative to trimming is a ranking split down the middle into two fields,
# which is what this all started as. The record goes first, because it is the one
# thing on the line that is also on the fighter's own card.
_DROP_IN_ORDER = ("record",)


def _fitted(entries: list, **kw) -> list[str]:
    """The lines, trimmed until a full board fits in one field.

    The real boards run to about 750 of the 1024 with everything on, so this does
    nothing almost always. It exists because "almost" is doing work there: one
    long name arriving on a board, or a sixteenth no contest, is the difference
    between a board and no board at all.
    """
    lines = _rating_lines(entries, **kw)
    for give_up in _DROP_IN_ORDER:
        if len("\n".join(lines)) <= FIELD_LIMIT:
            break
        kw[give_up] = False
        lines = _rating_lines(entries, **kw)
    return lines


def _rating_lines(
    entries: list,
    *,
    with_division: bool,
    career: bool = False,
    record: bool = True,
) -> list[str]:
    """One line per fighter.

    ``career`` prints the all-time boards, whose number is a career score on its
    own scale rather than a rating -- how good a fighter was, fitted from the
    whole record, plus what he won. Printing the defences alongside is the other
    half of what stops it reading as the same quantity as the board above.
    """
    lines = []
    for entry in entries:
        # Three characters wide, so every name starts in the same column.
        badge = f"`{entry.rank:>3}`"
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
            if record:
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
        embed,
        "Current Ratings",
        _fitted(entries, with_division=pound_for_pound),
    )
    if all_time:
        add_chunked_fields(
            embed,
            "🐐 All Time Ratings",
            _fitted(all_time, with_division=pound_for_pound, career=True),
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
                "🏆 current champion · 🎖️ former champion\n\n"
                "Ranked here: three or more UFC fights, and a fight in the last eighteen "
                "months. After a year out a rating fades — halving what a fighter holds over "
                "the starting rating for every further year — so a number nobody is "
                "defending stops outranking the fighters competing for it.\n\n"
                "**🐐 All Time Ratings** asks a different question and answers it in a "
                "different unit: a career score, not a rating, so there is nothing to be "
                "read in the gap between a fighter's two numbers. Higher is better and "
                "nothing caps it. Nothing fades and nobody is dropped for having retired, "
                "and instead of the rating a fighter carries now it uses how good he was "
                "across the whole record, plus credit for every title he won and "
                "defended.\n\n"
                "Not the rating with the fade taken off: a rating adds up results, so it "
                "pays for a long career over a good one, and it cannot say *beat him three "
                "times*. A career judged by the number it ended on is judged by its "
                "decline. Five or more fights to qualify, and a fighter is listed in the "
                "division they fought in most rather than the one they finished in, or "
                "St-Pierre is a middleweight.\n\n"
                "Defences are this board's belt, so a two-division champion's are split "
                "between his divisions."
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
        elif change.kind == "returned":
            # Where they are, not how they got there. A fighter back from a
            # layoff is handed the fade back and pays for the result out of it,
            # so an arrow between two places would be describing the layoff
            # ending and attributing it to the fight.
            what = f"back, now **{change.now}**"
        else:
            what = f"**{change.was} → {change.now}**"
        facts = [what, f"after {change.reason}"]
        if change.rating is not None:
            facts.append(str(change.rating))
        lines.append(f"{mark} {keep(change.name)} · {join(facts)}")

    add_chunked_fields(embed, "Moves", lines or [DASH])
    return stamp(embed)
