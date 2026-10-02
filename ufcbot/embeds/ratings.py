"""The ratings boards, and how they moved since they were last published."""

from __future__ import annotations

import discord

from ..stats.rankings import win_chance
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
    surname,
)

ARROWS = {"entered": "🆕", "left": "🚪", "up": "🔼", "down": "🔽", "returned": "↩️"}



def _odds_against(entry, champion, fading: bool) -> str | None:
    """What the board says this fighter would do against the champion.

    Shown instead of a tie marker, because a tie band says two fighters cannot be
    told apart without ever saying how far apart "cannot be told apart" is, and
    this says it in a unit everybody already reads.

    Against the champion rather than the board leader: the leader is whoever the
    rating puts first, which at featherweight is two fighters sharing the place,
    so "vs the leader" needs an arbitrary pick between two men the board calls
    equal. The champion needs no pick and answers the question people are
    actually asking.

    Nothing for a fighter whose rating is fading. The fade is a display rule for
    easing an absent fighter off the board, not a measured loss of skill, and
    running it through a win probability turns the one into the other -- a
    retired fighter would get a number that ticks down every day he stays
    retired.
    """
    if champion is None or entry.key == champion.key:
        return None
    if fading:
        return "inactive"
    return f"{win_chance(entry.rating, champion.rating):.0%} vs {surname(champion.name)}"


# What a line gives up, in the order it gives it up, when fifteen of them will
# not fit in one field. Discord refuses the whole embed rather than the overflow,
# so a board that cannot be trimmed is a board that does not post -- and the
# alternative to trimming is a ranking split down the middle into two fields,
# which is what this all started as. The record goes first, because it is the one
# thing on the line that is also on the fighter's own card.
_DROP_IN_ORDER = ("record", "odds")


def _fitted(entries: list, **kw) -> list[str]:
    """The lines, trimmed until a full board fits in one field.

    The real boards run to about 920 of the 1024 with everything on, so this does
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
    champion=None,
    fading: frozenset[str] = frozenset(),
    record: bool = True,
    odds: bool = True,
) -> list[str]:
    """One line per fighter.

    ``career`` prints the all-time boards, whose number is a career score rather
    than a rating -- a peak plus what the fighter won with it. Printing the
    defences alongside is what stops it reading as the same quantity as the
    board above, where it would look wrong: a three-fight peak can sit below the
    single rating a fighter carries today.
    """
    lines = []
    for place, entry in enumerate(entries, 1):
        # A shared rank keeps its number but loses the medal: a joint first is
        # not a winner, and two of the same medal reads as a mistake.
        # Every badge is the same three characters wide, so every name starts in
        # the same column. A medal is a different width from a number and a
        # shared rank is a character wider again, which is what pushed the
        # names out of line.
        # Where the odds are shown, places are numbered straight through and
        # the tie marker goes: "=3" beside two different percentages is the
        # board contradicting itself on one line, and a shared 3 with nothing
        # to explain it is worse than either. The column says how close they
        # are, in a unit that needs no key.
        if champion is not None:
            badge = f"`{place:>3}`"
        else:
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
        against = None if career or not odds else _odds_against(entry, champion, entry.key in fading)
        if against:
            facts.append(against)

        lines.append(f"{badge} {keep(entry.name)}{belt} · {join(facts)}")
    return lines


def rankings_embed(
    division: str,
    entries: list,
    *,
    pound_for_pound: bool = False,
    note: bool = False,
    all_time: list | None = None,
    fading: frozenset[str] = frozenset(),
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

    # Pound for pound carries no odds column: a win probability between a
    # flyweight and a heavyweight is a number about a fight nobody can make.
    champion = None if pound_for_pound else next((e for e in entries if e.champion), None)
    add_chunked_fields(
        embed,
        "Current Ratings",
        _fitted(entries, with_division=pound_for_pound, champion=champion, fading=fading),
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
                "🏆 holds the belt · 🎖️ held it once · **=** a shared rank, because ratings a "
                "few points apart are in no particular order.\n\n"
                "On a divisional board each line says what these ratings give that fighter "
                "against the champion, so the gap is in a unit that needs no key. Those "
                "percentages are measured, not assumed: across every fight on record 100 "
                "rating points is worth about 11 points of win rate. It is also why the order "
                "is a best guess rather than a measurement — within about 45 points the "
                "higher-rated fighter wins at most 55% of the time, and most of a board sits "
                "inside that. A fighter whose rating is fading reads **inactive** instead: "
                "the fade eases an absent fighter off the board and is not a measured loss of "
                "skill, so it has no business inside a win probability.\n\n"
                "Ranked here: three or more UFC fights, and a fight in the last eighteen "
                "months. After a year out a rating fades — halving what a fighter holds over "
                "the starting rating for every further year — so a number nobody is "
                "defending stops outranking the fighters competing for it.\n\n"
                "**🐐 All Time Ratings** asks a different question and scores it differently. "
                "Nothing "
                "fades and nobody is dropped for having retired, and instead of the rating a "
                "fighter carries now it uses the best they ever held, plus credit for every "
                "title they won and defended.\n\n"
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
