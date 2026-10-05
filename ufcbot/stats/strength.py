"""How good a fighter was, fitted from every fight at once.

The rating the boards show is a running one: everybody starts at 1000 and each
result moves them a little. That is the right shape for "where does he stand
today", and the wrong shape for "how good was he", because a rating that begins
at the middle takes a career to leave it. A fighter who wins 70% of the time
settles 147 points up, and at 32 points a fight he cannot get there in five.

So the all-time boards pay for length. Holding the win rate and the quality of
opposition fixed, ten more fights were worth +25 rating points where ten points
of win rate were worth +14 -- length counted nearly twice what winning did, on
the board that is supposed to be about how good somebody was. Every summary of
the running rating has that in it, because they all inherit the starting point:
the career maximum, the best run of three or five, the 90th percentile and the
plain average all came out between +14 and +27.

This has no starting point. Every fighter gets one number, chosen so that the
whole record is as likely as possible: fighter A beating fighter B is evidence
that A's number is higher, and the fit settles all of them together. A fighter
with five fights is not halfway to his number, he is at it, with a wide error
bar that the regularisation stands in for -- the pull toward the middle is what
stops an unbeaten five-fight record running away to infinity, and it fades as
the evidence mounts.

Measured the same way, this pays +13 for ten fights against +33 for ten points
of win rate. Winning is worth two and a half times fighting often, which is the
way round it should be.
"""

from __future__ import annotations

import logging
import math

log = logging.getLogger(__name__)

# How hard to pull a fighter's number toward the middle.
#
# Without it an unbeaten record has no finite answer -- nothing in the results
# contradicts "infinitely good" -- so this is what makes a short perfect career
# merely very good. At 1.0 the top fifteen all time hold no record shorter than
# nine fights and the best unbeaten five-fight career sits 34th, so it is not
# letting anybody in on a handful of wins; at 3.0 the whole scale compresses
# without changing the order much.
SHRINKAGE = 1.0

# The rating scale the boards are drawn on, so this can be read beside them: a
# tenfold change in the odds of winning is 400 points, as in the running rating.
POINTS_PER_DECADE = 400.0
CENTRE = 1000.0

# The fit is iterative; these are where it stops.
MAX_ROUNDS = 400
SETTLED = 1e-7


def fit(bouts: list[tuple[str, str]], *, shrinkage: float = SHRINKAGE) -> dict[str, float]:
    """One number per fighter, from ``(winner, loser)`` pairs.

    Returns ratings on the board's scale. Gradient descent on the penalised log
    likelihood, which is convex, so where it stops is the answer rather than an
    answer. scikit-learn would do this as a logistic regression with one column
    per fighter, and is a heavier dependency than the loop is long.
    """
    if not bouts:
        return {}

    index: dict[str, int] = {}
    pairs: list[tuple[int, int]] = []
    for winner, loser in bouts:
        for name in (winner, loser):
            if name not in index:
                index[name] = len(index)
        pairs.append((index[winner], index[loser]))

    strength = [0.0] * len(index)
    # Each fighter's gradient is bounded by how many fights they had, so a step
    # that is safe for the busiest is safe for everybody.
    busiest = max(_appearances(pairs, len(index)))
    step = 1.0 / (busiest / 4.0 + 2.0 * shrinkage)

    for _ in range(MAX_ROUNDS):
        gradient = [2.0 * shrinkage * s for s in strength]
        for won, lost in pairs:
            # Chance the fit currently gives to the result that happened.
            expected = 1.0 / (1.0 + math.exp(-(strength[won] - strength[lost])))
            gradient[won] -= 1.0 - expected
            gradient[lost] += 1.0 - expected
        biggest = 0.0
        for i, g in enumerate(gradient):
            strength[i] -= step * g
            biggest = max(biggest, abs(g))
        if biggest < SETTLED:
            break
    else:
        log.info("Strength fit stopped at the round limit; it is close but not settled.")

    scale = POINTS_PER_DECADE / math.log(10)
    return {name: CENTRE + strength[i] * scale for name, i in index.items()}


def _appearances(pairs: list[tuple[int, int]], count: int) -> list[int]:
    seen = [0] * count
    for a, b in pairs:
        seen[a] += 1
        seen[b] += 1
    return seen or [1]
