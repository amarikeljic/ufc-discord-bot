"""Rankings from the model's own ratings.

The rating is the one the model trains on: every fighter starts level, and a
result moves it by how surprising it was, so beating a contender is worth more
than beating a debutant. That makes it a measure of who has done the most
against the best, which is close to what a ranking is for.

It is not the UFC's ranking and will not agree with it. Nobody votes, a title
counts for nothing by itself, and a fighter who arrives from another promotion
starts level with everyone else however good they already are.

Two things stop a raw rating from reading as a ranking, and both are handled
here rather than in the rating itself:

* A rating is what a fighter has earned, and a fighter who is not fighting is
  not earning. Left alone, someone who retires keeps the number they walked
  away with and outranks everyone still competing for it. So a rating fades
  once a fighter has been out longer than any ordinary gap between bouts.
* Ratings that land on the same number are separated by who got there first,
  so the board is an order all the way down rather than a set of ties.

Both are presentation. ``Ledger.elo`` is left exactly as the fights left it, so
what the model trains and predicts on does not change.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from .career import ELO_START, Ledger
from .strength import CENTRE

# Divisions in the order they are usually listed, lightest first.
DIVISION_ORDER = (
    "Strawweight",
    "Flyweight",
    "Bantamweight",
    "Featherweight",
    "Lightweight",
    "Welterweight",
    "Middleweight",
    "Light Heavyweight",
    "Heavyweight",
    "Women's Strawweight",
    "Women's Flyweight",
    "Women's Bantamweight",
    "Women's Featherweight",
)

POUND_FOR_POUND = "Pound for pound"


def is_womens(division: str | None) -> bool:
    return bool(division and division.startswith("Women's"))

# A year covers any ordinary gap between fights, injuries included, so nothing
# happens inside it. Past that, what a fighter holds over the starting rating
# halves for every further year out.
DECAY_GRACE = timedelta(days=365)
DECAY_HALF_LIFE = timedelta(days=365)

# A fighter who has not been seen in this long is not ranked: retired, released,
# or fighting somewhere else. Eighteen months is long enough that a comeback
# from injury is still on the board and short enough that a retirement is not.
ACTIVE_WITHIN = timedelta(days=548)

# Below this many UFC fights a rating is mostly where it started.
MIN_FIGHTS = 3
# All-time asks what someone did over a career, so it asks for more of one. Five
# is still low, deliberately: an unbeaten seven-fight run belongs on the board
# that is about who was ever any good, even before it is a career.
ALL_TIME_MIN_FIGHTS = 5
# What a belt is worth on an all-time board, in rating points.
#
# A rating cannot say "beat him three times", because it is transitive: it adds
# up every result and a long career outscores a better one. Left to the rating
# alone the board had Holloway above Volkanovski, who beat him three times for
# the belt, and Du Plessis above Anderson Silva. What those boards were missing
# is the thing the sport actually settles arguments with, and the only one the
# fighters are competing for.
#
# Weighed against the gap it has to close: a divisional board spans about 150
# points, so a defence moves a fighter past roughly a tenth of it. Every pairing
# that reads wrong without this reads right with it, and none that read right
# were broken by it.
TITLE_DEFENCE_POINTS = 15
TITLE_WIN_POINTS = 5
# How many to list per division.
DEPTH = 15


@dataclass(slots=True)
class Ranked:
    rank: int
    name: str
    rating: int
    """What the board shows: the rating with the layoff fade applied."""
    record: str
    division: str | None
    raw: int = 0
    """The same rating with nothing faded off it. Never shown -- it is how the
    watcher tells a crossing caused by a result from one caused by a layoff
    ending, and it is zero on a board built before this was kept."""
    key: str = ""
    """The dataset key, which is what a board is remembered by between passes."""
    champion: bool = False
    """Whether they hold *this board's* belt. Shown, never ranked on: the rating
    is the rating, and a board that reordered itself around the belt would be the
    UFC's ranking rather than this one's."""
    defences: int = 0
    """Title defences of this board's belt -- of any belt, pound for pound.
    Shown on the all-time boards, where they are most of what the score is made
    of."""
    former_champion: bool = False
    """Held this board's belt once and does not now. On a current board it is
    most of the answer to why someone is up there; on an all-time board the
    absence of it is the interesting half, since it marks out the careers that
    never got a belt."""
    last_fight: date | None = None
    last_result: str | None = None


def rating_on(ledger: Ledger, on: date) -> int:
    """What the rating is worth today, faded for however long the fighter has been out.

    Only the margin over the starting rating fades, so a fighter who never got
    above it has nothing to lose, and nobody is ever dragged below where they
    began by sitting still.
    """
    if ledger.last_fight is None:
        return round(ledger.elo)
    idle = (on - ledger.last_fight) - DECAY_GRACE
    if idle <= timedelta(0):
        return round(ledger.elo)
    kept = 0.5 ** (idle / DECAY_HALF_LIFE)
    return round(ELO_START + (ledger.elo - ELO_START) * kept)


def is_fading(ledger: Ledger, on: date) -> bool:
    """Whether this fighter's rating is being held down by a layoff."""
    return ledger.last_fight is not None and (on - ledger.last_fight) > DECAY_GRACE


def is_eligible(ledger: Ledger, on: date) -> bool:
    """Ranked at all: enough UFC fights, and seen recently enough to still be active."""
    return _eligible(ledger, on)


def _eligible(ledger: Ledger, on: date) -> bool:
    if ledger.fights < MIN_FIGHTS or ledger.last_fight is None:
        return False
    return on - ledger.last_fight <= ACTIVE_WITHIN


def _ordered(
    ledgers: dict[str, Ledger],
    division: str | None,
    *,
    on: date,
    include_women: bool,
) -> list[Ranked]:
    """Everyone eligible, best first, with ranks shared between fighters too close to separate."""
    entries = [
        (rating_on(ledger, on), key, ledger)
        for key, ledger in ledgers.items()
        if _eligible(ledger, on)
        and (division is None or ledger.division == division)
        and (include_women or not is_womens(ledger.division))
    ]
    # Equal ratings go to whoever got there first: the fighter who has been
    # holding that number longest is the one who has not had it taken off him
    # since. Name breaks what is left, so the same board comes back the same
    # way twice running, which is what the change watcher compares against.
    entries.sort(key=lambda entry: (-entry[0], entry[2].last_fight or date.min, entry[2].name))
    return _ranked(entries, on=on, division=division)


def holds_belt(ledger: Ledger, on: date | None, division: str | None = None) -> bool:
    """Whether to show this fighter as champion of ``division``.

    ``division`` of None asks whether they hold any belt at all, which is what
    pound for pound wants: that board is not about one division, so a champion
    is a champion there.

    The data records that someone won a title fight. It never records a champion
    vacating, being stripped, or being elevated from interim, so the belt is left
    with whoever last won one however long ago that was. The one case that can be
    caught from the data is the champion who has since gone: past the eighteen
    months that take a fighter off the board, they are a former champion whatever
    the last title fight said. That is what had Jon Jones showing as heavyweight
    champion two years after he last held it.
    """
    if ledger.champion_of is None:
        return False
    if division is not None and ledger.champion_of != division:
        return False
    return on is None or _eligible(ledger, on)


def _ranked(
    entries: list[tuple[int, str, Ledger]],
    *,
    home: bool = False,
    on: date | None = None,
    division: str | None = None,
) -> list[Ranked]:
    """Rated entries, best first, one place each.

    ``home`` names each fighter by the division they fought in most rather than
    the one they finished in, which is what an all-time board is asking about.

    Places run straight through. Fighters used to share one when their ratings
    were within a few points, on the argument that a gap that small is inside
    the noise of a single result -- but the board still had to print one of
    them above the other, so it was claiming an order and disclaiming it on the
    same line. The order given is the one the sort made.
    """
    ranked: list[Ranked] = []
    for place, (rating, key, ledger) in enumerate(entries, 1):
        ranked.append(
            Ranked(
                rank=place,
                name=ledger.name,
                rating=rating,
                raw=round(ledger.elo),
                record=ledger.record,
                division=(ledger.home_division or ledger.division) if home else ledger.division,
                key=key,
                champion=holds_belt(ledger, on, division),
                former_champion=(
                    bool(ledger.belts_held)
                    if division is None
                    else division in ledger.belts_held
                )
                and not holds_belt(ledger, on, division),
                defences=ledger.defences_in(division),
                last_fight=ledger.last_fight,
                last_result=ledger.last_result,
            )
        )
    return ranked


def rank_division(
    ledgers: dict[str, Ledger],
    division: str | None,
    *,
    on: date,
    depth: int = DEPTH,
    include_women: bool = True,
) -> list[Ranked]:
    """The best-rated active fighters, in one division or across all of them.

    ``division`` of None ranks everyone, which is the pound-for-pound list, and
    is where ``include_women`` matters: a server that leaves the women's
    divisions out should not find them on that board either.
    """
    return _ordered(ledgers, division, on=on, include_women=include_women)[:depth]


def career_points(ledger: Ledger) -> float:
    """What a fighter did, on the scale the strength fit works in.

    The fitted strength rather than the rating they ended on, because a career
    judged on its last day is judged on its decline -- Anderson Silva gave back
    120 points going 1-6 at the end and finished below fighters he would have
    beaten in his sleep. And rather than any reading of the running rating,
    because that one starts everybody in the middle and takes a career to leave,
    so every summary of it pays for length. See :mod:`ufcbot.stats.strength`.

    Unrounded, and in rating points rather than the score the boards print: a
    score is six points wide, so ordering on it would tie fighters the record
    separates.
    """
    return (
        ledger.strength
        + TITLE_DEFENCE_POINTS * ledger.title_defences
        + TITLE_WIN_POINTS * ledger.title_wins
    )


# How many rating points go into one point of career score. The all-time number
# used to be on the rating scale and got read against the current board, which
# is a comparison between two different measurements: Evloev shows 1160 now and
# 1257 all time, and the 97 points between them are not 97 points of anything.
# At six to one, Jones is on 99 today and the bottom of a divisional all-time
# board is in the twenties, so nothing on either board reads as a rating.
#
# Nothing caps it, and it is not out of a hundred: whoever passes Jones scores
# over 100, which is the scale working rather than breaking. Fixed rather than
# fitted to the field, so a fighter's score does not move when somebody else
# fights.
RATING_POINTS_PER_SCORE = 6.0


def career_score(ledger: Ledger) -> int:
    """What a fighter did, as a score on its own scale. Higher is better.

    Two figures rather than four so that it is visibly not the rating printed
    above it, and open at the top: there is no best possible career to be a
    fraction of, and whoever passes Jones goes over 100.

    It is floored at zero, which is the one bound that is real. A board is cut
    to fifteen long after this is computed, and in a division thin enough that
    a losing record reaches the board, a negative reads as a broken number
    rather than as a bad career.
    """
    return max(0, round((career_points(ledger) - CENTRE) / RATING_POINTS_PER_SCORE))


def all_ranked(
    ledgers: dict[str, Ledger], division: str | None, *, on: date, include_women: bool = True
) -> list[Ranked]:
    """Everyone eligible in a division, in order, with no cut.

    The boards print the top of this. The ratings watcher reads all of it, so
    that crossing the line the boards cut at is an ordinary crossing between the
    fighters either side of it.
    """
    return _ordered(ledgers, division, on=on, include_women=include_women)


def all_time(
    ledgers: dict[str, Ledger],
    division: str | None,
    *,
    depth: int = DEPTH,
    include_women: bool = True,
    on: date | None = None,
) -> list[Ranked]:
    """The greatest careers, in one division or across all of them.

    Not the board above with the filter taken off. That board answers who is
    best now and answers it with a rating; this one answers who was ever best,
    and the running rating cannot. It is transitive, so it had Holloway above
    Volkanovski, who beat him three times for the belt; and it accumulates from a
    standing start, so it paid nearly twice as much for another ten fights as for
    another ten points of win rate. So this ranks on :func:`career_score`: how
    good a fighter was, fitted from every fight at once, plus what he won.

    Nothing is faded and nobody is dropped for not having fought lately. A
    fighter is listed in the division they fought in most rather than the one
    they finished in, or St-Pierre is a middleweight and Jones a heavyweight.
    """
    entries = [
        (career_score(ledger), key, ledger)
        for key, ledger in ledgers.items()
        if ledger.fights >= ALL_TIME_MIN_FIGHTS
        and (division is None or (ledger.home_division or ledger.division) == division)
        and (include_women or not is_womens(ledger.home_division or ledger.division))
    ]
    # Sorted on the unrounded points and printed as the score: two careers that
    # print the same number are almost never equal underneath, and the points
    # say which way round they go.
    entries.sort(key=lambda entry: (-career_points(entry[2]), entry[2].name))
    return _ranked(entries, home=True, on=on, division=division)[:depth]


def standing(ledgers: dict[str, Ledger], key: str, *, on: date) -> Ranked | None:
    """Where one fighter sits in their own division, or None when they are not ranked.

    Unbounded depth: a fighter 30th in their division is still worth telling,
    where the boards only print the top of each.
    """
    ledger = ledgers.get(key)
    if ledger is None or ledger.division is None or not _eligible(ledger, on):
        return None
    return _find(_ordered(ledgers, ledger.division, on=on, include_women=True), key)


def pound_for_pound_rank(ledgers: dict[str, Ledger], key: str, *, on: date) -> Ranked | None:
    """Where a fighter sits across every division, or None when they are not ranked."""
    ledger = ledgers.get(key)
    if ledger is None or not _eligible(ledger, on):
        return None
    return _find(_ordered(ledgers, None, on=on, include_women=True), key)


def _find(ranked: list[Ranked], key: str) -> Ranked | None:
    return next((entry for entry in ranked if entry.key == key), None)


def divisions_with_fighters(
    ledgers: dict[str, Ledger], *, on: date, include_women: bool = True
) -> list[str]:
    """Divisions that have anyone to rank, in the usual order."""
    present = {ledger.division for ledger in ledgers.values() if _eligible(ledger, on)}
    return [
        division
        for division in DIVISION_ORDER
        if division in present and (include_women or not is_womens(division))
    ]
