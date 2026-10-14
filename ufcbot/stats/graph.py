"""Every professional fight, not just the UFC ones, as one rating.

The ratings the model trains on only ever saw UFC fights -- 8,935 of them --
because that is all ufcstats publishes. A fighter arriving from anywhere else
starts level with everyone, and on a quarter of all fights the model is
predicting someone it knows nothing about.

ESPN publishes whole professional careers. Walked as one Elo in date order this
is 71,958 decided fights, and a debutant arrives with a rating earned against
real opponents rather than at 1000. It propagates, too: beating someone who beat
a UFC fighter carries through the graph, which no summary of a record can say.

Measured on rolling origin over 446 events, against the model without it:

    debuts        -0.01114  95% [-0.01731, -0.00517]  100% of draws
    established   -0.00229  95% [-0.00565, +0.00119]   91%
    overall       -0.00498  95% [-0.00764, -0.00250]  100%

That split was measured when the cache held 56,078 decided fights. The crawl has
since run to closure at 71,958, and the overall figure re-measured there is
-0.00547, 95% [-0.00918, -0.00183], 99.9% of draws -- the same answer. The
fighters the extra passes reach are four and five steps out from the UFC, so
they barely touch the rating of anybody the model is asked about.

Eight designs that summarised the same careers into columns all failed, every
one of them helping debutants and hurting everybody else by about as much. The
difference here is that the rating system walks the fights instead of being told
about them afterwards.

**The graph is selected on its outcome, and the bias is real.** A regional fight
is in this data because one of the two later reached the UFC, and fighters reach
the UFC by winning, so arrivals are selected for luck as well as skill. Measured
on UFC debuts, the graph over-predicts the winner by 3.4 points, and it is worst
for the longest regional careers rather than the shortest:

    graph fights behind them      n  predicted   actual      gap
                         0-5    396      0.434    0.399   +0.035
                        5-10    861      0.478    0.472   +0.006
                       10-20    795      0.506    0.467   +0.040
                       20-99    217      0.534    0.415   +0.120

Elo accumulates and regional opponents sit near the starting rating, so the
longer somebody fought out there the more inflation they bring with them. That
is why :attr:`Ledger.graph_fights` is a feature beside the rating and not an
implementation detail -- it is what lets the model discount the rating in
proportion to how much of it was earned outside, and fit the correction rather
than have one imposed.

Nothing here reaches the boards. They say UFC fights only and that stays true.
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

log = logging.getLogger(__name__)

CAREERS_FILE = "espn_careers.json"

# The same K the boards walk, for no better reason than that it is the one this
# codebase is calibrated around. The model's own K lives in career.py and is a
# separate question; this rating is a feature, not a board, so a sweep over it
# belongs with the other model tuning rather than here.
GRAPH_K = 32.0
GRAPH_START = 1000.0
GRAPH_DIVISOR = 400.0

# ESPN dates a card in UTC and ufcstats dates it locally, so one fight can carry
# two dates a day apart. Without a margin a bout could count as preceding
# itself, which would leak its own result into the features predicting it --
# and on exactly the fights being scored. Nobody fights twice inside three days.
SAFE_GAP = timedelta(days=3)

# Professional fights a newcomer needs before the bot bothers to remember
# them. At one it is 32,050 fighters; at three it is 9,549, which is 0.3 MB in
# the cache, and nobody arrives in the UFC on a 2-0 record.
NEWCOMER_FLOOR = 3

# What a card calls a corner nobody has filled yet. ESPN hands these out with an
# athlete id and a fight history like anybody else, so left alone they collect
# results from every card they appear on and become a rated fighter called TBA --
# who the bot would then pick, by name, to beat somebody real. There is one in
# the cache already on two fights, which is under the floor above by luck rather
# than design.
PLACEHOLDERS = frozenset({"tba", "tbd"})


def is_placeholder(key: str) -> bool:
    """Whether a normalised name is a card saying nobody yet.

    On whole words: a corner is "TBA" or "Opponent TBA", never a man whose name
    happens to contain the letters."""
    return bool(set(key.split()) & PLACEHOLDERS) or key in (
        "to be announced",
        "to be determined",
    )


@dataclass(slots=True)
class Graph:
    """Professional fights in date order, and the ratings they imply.

    Walked forward once as the career replay advances, never backwards: the
    caller hands it ever-later cutoffs and it applies whatever is now in the
    past. Reading a rating never applies anything, so a lookup cannot see the
    fight it is about to be used for.
    """

    fights: list[tuple[date, str, str]]
    """``(on, winner, loser)``, sorted, deduplicated."""

    rating: dict[str, float] = field(default_factory=lambda: defaultdict(lambda: GRAPH_START))
    seen: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    _next: int = 0
    _cursor: date = date.min
    """How far ``advance_to`` has been asked to go. Only ever forward."""

    def advance_to(self, cutoff: date) -> None:
        """Apply every fight before ``cutoff``. Cheap to call per bout.

        The cursor only moves forward, so asking for an earlier cutoff than the
        last one cannot be served: the ratings already hold later fights and
        there is no going back. That is a mistake worth a crash rather than an
        answer, because the answer is every fight rated by its own result.

        It happens when one graph is handed to two passes over the dataset --
        building a design matrix per candidate K, say. The second pass finds the
        graph already at the end of the data, every advance a no-op, and trains
        on the future. The numbers that come out are not slightly optimistic;
        they beat the closing line by eleven points of accuracy."""
        if cutoff < self._cursor:
            raise ValueError(
                f"graph already advanced to {self._cursor}, cannot rewind to {cutoff}; "
                "build a fresh Graph per pass rather than reusing one"
            )
        self._cursor = cutoff
        while self._next < len(self.fights):
            on, winner, loser = self.fights[self._next]
            if on >= cutoff:
                return
            self._apply(winner, loser)
            self._next += 1

    def _apply(self, winner: str, loser: str) -> None:
        won, lost = self.rating[winner], self.rating[loser]
        expected = 1.0 / (1.0 + 10.0 ** ((lost - won) / GRAPH_DIVISOR))
        self.rating[winner] = won + GRAPH_K * (1.0 - expected)
        self.rating[loser] = lost - GRAPH_K * (1.0 - expected)
        self.seen[winner] += 1
        self.seen[loser] += 1

    def standing(self, key: str) -> tuple[float, int]:
        """This fighter's rating and how many graph fights are behind it."""
        return self.rating[key], self.seen[key]

    def newcomers(self, known: set[str], *, floor: int = NEWCOMER_FLOOR) -> dict[str, tuple[float, int]]:
        """What is known about fighters who have never been in the UFC.

        Carried into the bot so a debut can be called at all: a fighter with
        no UFC record has no ledger to read, and without this the only honest
        answer was no answer. The floor keeps the regional journeymen out --
        the UFC does not sign anybody at two professional fights, and every
        one of them kept is weight the bot carries for the life of a run."""
        return {
            key: (value, self.seen[key])
            for key, value in self.rating.items()
            if key not in known and self.seen[key] >= floor
        }


def load(data_dir: Path | str, *, normalise) -> Graph | None:
    """Read the cached careers, or None when there are none to read.

    Optional by design. The file is built by a crawl that cannot run on the
    refresh timer, so the bot has to work without it: every fighter then keeps
    the starting rating and a count of nought, and the two features built from
    them are constant and carry nothing.
    """
    path = Path(data_dir) / CAREERS_FILE
    if not path.exists():
        log.info("No %s; the model will train without professional careers.", CAREERS_FILE)
        return None
    try:
        careers = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log.warning("Could not read %s (%s); carrying on without it.", CAREERS_FILE, exc)
        return None
    return Graph(fights=_fights_from(careers, normalise=normalise))


def _fights_from(careers: dict, *, normalise) -> list[tuple[date, str, str]]:
    """One entry per bout, winner first.

    A fight between two cached fighters is listed on both their cards, so it is
    deduplicated on the pair and the date. Draws and no contests are dropped:
    this rating only reads wins and losses, the same as the one the boards use.
    """
    seen: set[tuple[str, str, date]] = set()
    fights: list[tuple[date, str, str]] = []
    for key, got in (careers or {}).items():
        if not got:
            continue
        for bout in got.get("fights", ()):
            opponent, when = bout.get("opponent"), bout.get("on")
            result = (bout.get("result") or "").strip().upper()
            if not opponent or not when or result not in ("W", "L"):
                continue
            try:
                on = date.fromisoformat(when)
            except ValueError:
                continue
            other = normalise(opponent)
            if is_placeholder(key) or is_placeholder(other):
                # Not a fight. Dropped rather than rated, both for the phantom's
                # sake and for the real fighter credited with beating one.
                continue
            pair = (*sorted((key, other)), on)
            if pair in seen:
                continue
            seen.add(pair)
            fights.append((on, key, other) if result == "W" else (on, other, key))
    fights.sort()
    log.info("Professional graph: %d decided fights", len(fights))
    return fights
