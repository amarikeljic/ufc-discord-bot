"""Prediction results and the technique blend.

Everything here is plain Python: the bot process needs these types to render
picks and read back stored ones, and must not pull in pandas or scikit-learn to
do it. Training lives in :mod:`ufcbot.stats.model`, which does use them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from .career import Ledger
from .techniques import FINISHES

# How many fights' worth of weight the league-wide technique base rate carries.
TECHNIQUE_PRIOR_STRENGTH = 6.0
# A loser's history of being finished a certain way counts half as much as the winner's finishes.
LOSER_TECHNIQUE_WEIGHT = 0.5
IGNORED_TECHNIQUES = frozenset({"other", "strikes"})
DEFAULT_DRAW_RATES = {3: 0.006, 5: 0.010}


@dataclass(slots=True)
class Evaluation:
    """Held-out performance on fights after ``holdout_from``."""

    holdout_from: date
    fights: int
    accuracy: float
    log_loss: float
    baseline_accuracy: float
    """Always picking the fighter with the better win rate going in."""

    method_accuracy: float | None = None
    """Right method, when told who won."""
    method_baseline: float | None = None
    """Always guessing the most common method."""
    exact_accuracy: float | None = None
    """Right winner and right method together."""
    technique_accuracy: float | None = None
    """Right technique, when told who won and that it was that kind of finish."""
    technique_baseline: float | None = None

    def summary(self) -> str:
        return (
            f"{self.accuracy:.1%} accuracy on {self.fights} fights since {self.holdout_from:%b %Y} "
            f"(log loss {self.log_loss:.3f}, record-only baseline {self.baseline_accuracy:.1%})"
        )

    def method_summary(self) -> str | None:
        if self.method_accuracy is None:
            return None
        text = (
            f"method {self.method_accuracy:.1%} when the winner is known "
            f"(always-decision baseline {self.method_baseline:.1%}), "
            f"winner and method together {self.exact_accuracy:.1%}"
        )
        if self.technique_accuracy is not None:
            text += (
                f", finishing technique {self.technique_accuracy:.1%} "
                f"(base-rate guess {self.technique_baseline:.1%})"
            )
        return text


@dataclass(slots=True)
class Prediction:
    fighter_a: str
    fighter_b: str
    prob_a: float
    """Probability A wins, given the fight has a winner."""

    draw: float = 0.0
    """Rough chance of a draw, from the league-wide rate."""
    methods_a: dict[str, float] = field(default_factory=dict)
    """Probability A wins by each method; the values sum to ``prob_a``."""
    methods_b: dict[str, float] = field(default_factory=dict)
    techniques_a: dict[str, list[tuple[str, float]]] = field(default_factory=dict)
    """Finish method -> techniques and their probability, given A wins that way."""
    techniques_b: dict[str, list[tuple[str, float]]] = field(default_factory=dict)

    @property
    def prob_b(self) -> float:
        return 1 - self.prob_a

    @property
    def favourite_side(self) -> str:
        return "a" if self.prob_a >= 0.5 else "b"

    @property
    def favourite(self) -> str:
        return self.fighter_a if self.prob_a >= 0.5 else self.fighter_b

    @property
    def confidence(self) -> float:
        return max(self.prob_a, self.prob_b)

    @property
    def has_methods(self) -> bool:
        return bool(self.methods_a or self.methods_b)

    def outcomes(self, side: str | None = None) -> list[tuple[str, str, str | None, float]]:
        """(side, method, likeliest technique, probability), most likely first."""
        rows = []
        for this_side, methods, techniques in (
            ("a", self.methods_a, self.techniques_a),
            ("b", self.methods_b, self.techniques_b),
        ):
            if side and this_side != side:
                continue
            for method, prob in methods.items():
                technique = None
                if method in FINISHES and techniques.get(method):
                    technique = techniques[method][0][0]
                rows.append((this_side, method, technique, prob))
        rows.sort(key=lambda row: row[3], reverse=True)
        return rows

    @property
    def pick_outcome(self) -> tuple[str, str | None, float] | None:
        """The favourite's likeliest way to win: (method, technique, probability)."""
        rows = self.outcomes(self.favourite_side)
        if not rows:
            return None
        _, method, technique, prob = rows[0]
        return method, technique, prob

    def to_dict(self) -> dict:
        return {
            "prob_a": self.prob_a,
            "draw": self.draw,
            "methods_a": self.methods_a,
            "methods_b": self.methods_b,
            "techniques_a": {m: [[t, p] for t, p in v] for m, v in self.techniques_a.items()},
            "techniques_b": {m: [[t, p] for t, p in v] for m, v in self.techniques_b.items()},
        }

    @classmethod
    def from_dict(cls, fighter_a: str, fighter_b: str, payload: dict) -> Prediction:
        return cls(
            fighter_a=fighter_a,
            fighter_b=fighter_b,
            prob_a=float(payload.get("prob_a", 0.5)),
            draw=float(payload.get("draw", 0.0)),
            methods_a={k: float(v) for k, v in (payload.get("methods_a") or {}).items()},
            methods_b={k: float(v) for k, v in (payload.get("methods_b") or {}).items()},
            techniques_a={m: [(t, float(p)) for t, p in v] for m, v in (payload.get("techniques_a") or {}).items()},
            techniques_b={m: [(t, float(p)) for t, p in v] for m, v in (payload.get("techniques_b") or {}).items()},
        )


def technique_distribution(
    method: str,
    winner: Ledger,
    loser: Ledger,
    priors: dict[str, dict[str, float]],
) -> list[tuple[str, float]]:
    """Likely techniques for ``winner`` finishing ``loser`` by ``method``, most likely first.

    A smoothed blend: the league-wide rate acts as a few fights of prior evidence,
    then the winner's own finishes and the loser's past losses by that method
    shift it. A striker with three head-kick knockouts moves well off the base rate.
    """
    counts = {tech: TECHNIQUE_PRIOR_STRENGTH * share for tech, share in (priors.get(method) or {}).items()}
    prefix = f"{method}:"
    for key, n in winner.win_techniques.items():
        if key.startswith(prefix):
            tech = key[len(prefix):]
            if tech not in IGNORED_TECHNIQUES:
                counts[tech] = counts.get(tech, 0.0) + n
    for key, n in loser.loss_techniques.items():
        if key.startswith(prefix):
            tech = key[len(prefix):]
            if tech not in IGNORED_TECHNIQUES:
                counts[tech] = counts.get(tech, 0.0) + LOSER_TECHNIQUE_WEIGHT * n
    total = sum(counts.values())
    if total <= 0:
        return []
    return sorted(((tech, c / total) for tech, c in counts.items()), key=lambda kv: kv[1], reverse=True)
