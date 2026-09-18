"""The trained model in a form the bot can run on its own.

Training needs scikit-learn, which drags in scipy and pandas: about 150 MB of
resident memory for a job that runs once a day. Scoring a fight needs none of
it, so training exports the fitted models as plain numbers -- tree nodes,
coefficients, means and medians -- and this module walks them directly.

The arithmetic here is the arithmetic scikit-learn performs, so a compiled model
returns what the fitted estimator returned, to the last decimal place.
``ufcbot/stats/model.py`` checks that on real fights every time it trains, and
refuses to save a model whose compiled form disagrees.
"""

from __future__ import annotations

import math
import pickle
from array import array
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from .career import Ledger
from .features import FEATURE_NAMES, matchup_row
from .prediction import DEFAULT_DRAW_RATES, Evaluation, Prediction, technique_distribution
from .techniques import FINISHES, METHODS

MODEL_VERSION = 4
MODEL_FILE = "ufc_model.pkl"


def _sigmoid(z: float) -> float:
    # Split by sign so neither branch can overflow exp().
    if z >= 0.0:
        return 1.0 / (1.0 + math.exp(-z))
    scaled = math.exp(z)
    return scaled / (1.0 + scaled)


def _softmax(raw: list[float]) -> list[float]:
    top = max(raw)
    weights = [math.exp(value - top) for value in raw]
    total = sum(weights)
    return [weight / total for weight in weights]


@dataclass(slots=True)
class Tree:
    """One boosted tree, as parallel arrays of node fields.

    ``array`` holds these as compactly as the C arrays they came from while
    still handing back plain floats and ints on indexing, which is what makes
    the walk below fast without numpy.
    """

    feature: array
    threshold: array
    left: array
    right: array
    value: array
    leaf: bytes
    missing_left: bytes

    def leaf_value(self, row: list[float]) -> float:
        """Walk from the root to a leaf. A missing value takes the branch training chose."""
        node = 0
        leaf, feature, threshold = self.leaf, self.feature, self.threshold
        left, right, missing_left = self.left, self.right, self.missing_left
        while not leaf[node]:
            value = row[feature[node]]
            if value != value:  # NaN
                node = left[node] if missing_left[node] else right[node]
            elif value <= threshold[node]:
                node = left[node]
            else:
                node = right[node]
        return self.value[node]


@dataclass(slots=True)
class Boost:
    """Gradient boosting: a baseline per class, plus one tree per class per iteration."""

    baseline: list[float]
    stages: list[list[Tree]]
    classes: list[int]
    """Which outcomes this model was fitted on, in the order it scores them."""

    def raw(self, row: list[float]) -> list[float]:
        totals = list(self.baseline)
        for stage in self.stages:
            for index, tree in enumerate(stage):
                totals[index] += tree.leaf_value(row)
        return totals


@dataclass(slots=True)
class Linear:
    """Median imputation, standard scaling and logistic regression, folded together."""

    medians: list[float]
    means: list[float]
    scales: list[float]
    coefficients: list[list[float]]
    intercepts: list[float]
    classes: list[int]
    """Which outcomes this model was fitted on, in the order it scores them."""

    def raw(self, row: list[float]) -> list[float]:
        medians, means, scales = self.medians, self.means, self.scales
        scaled = [
            ((medians[i] if value != value else value) - means[i]) / scales[i]
            for i, value in enumerate(row)
        ]
        return [
            intercept + sum(weight * x for weight, x in zip(weights, scaled))
            for weights, intercept in zip(self.coefficients, self.intercepts)
        ]


@dataclass(slots=True)
class Blend:
    """The two models' probabilities, averaged the way they were when fitted."""

    boost_weight: float
    boost: Boost
    linear: Linear
    width: int
    """How many outcomes the blend reports, whether or not both models saw them all."""

    def probabilities(self, row: list[float]) -> list[float]:
        boosted = self._spread(self._normalise(self.boost.raw(row)), self.boost.classes)
        linear = self._spread(self._normalise(self.linear.raw(row)), self.linear.classes)
        weight = self.boost_weight
        return [weight * a + (1 - weight) * b for a, b in zip(boosted, linear)]

    @staticmethod
    def _normalise(raw: list[float]) -> list[float]:
        """Raw scores to probabilities: a sigmoid for two classes, softmax for more."""
        if len(raw) == 1:
            positive = _sigmoid(raw[0])
            return [1 - positive, positive]
        return _softmax(raw)

    def _spread(self, probabilities: list[float], classes: list[int]) -> list[float]:
        """Place each probability under the outcome it belongs to, zero elsewhere.

        A model fitted on data where some outcome never occurred scores fewer
        columns than the blend reports, so it contributes nothing to that one.
        """
        if len(probabilities) == self.width and classes == list(range(self.width)):
            return probabilities
        spread = [0.0] * self.width
        for index, label in enumerate(classes):
            spread[label] = probabilities[index]
        return spread


@dataclass(slots=True)
class CompiledModel:
    """Everything needed to score a fight, and nothing else."""

    winner: Blend
    feature_names: list[str]
    trained_at: datetime
    training_fights: int
    dataset_newest: date | None
    evaluation: Evaluation | None = None
    importances: list[tuple[str, float]] = field(default_factory=list)
    method: Blend | None = None
    technique_priors: dict[str, dict[str, float]] = field(default_factory=dict)
    draw_rates: dict[int, float] = field(default_factory=lambda: dict(DEFAULT_DRAW_RATES))
    version: int = MODEL_VERSION

    # -- persistence -------------------------------------------------------

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".part")
        tmp.write_bytes(pickle.dumps(self, protocol=pickle.HIGHEST_PROTOCOL))
        tmp.replace(path)

    @classmethod
    def load(cls, path: Path) -> CompiledModel:
        model = pickle.loads(path.read_bytes())
        if not isinstance(model, cls):
            raise ValueError("That file does not hold a compiled model; retrain it.")
        if model.version != MODEL_VERSION or model.feature_names != FEATURE_NAMES:
            raise ValueError("Saved model was built with a different version; retrain it.")
        return model

    # -- inference ---------------------------------------------------------

    def predict(
        self,
        a: dict[str, float],
        b: dict[str, float],
        *,
        title_fight: bool = False,
        scheduled_rounds: int = 3,
        name_a: str = "A",
        name_b: str = "B",
        ledger_a: Ledger | None = None,
        ledger_b: Ledger | None = None,
    ) -> Prediction:
        """Probability that A beats B, and how.

        The winner model is scored from both corners and averaged, so swapping
        the fighters always gives exactly complementary probabilities.
        """
        context = {"title_fight": title_fight, "scheduled_rounds": scheduled_rounds}
        row_ab = matchup_row(a, b, **context)
        row_ba = matchup_row(b, a, **context)
        forward = self.winner.probabilities(row_ab)[1]
        reverse = self.winner.probabilities(row_ba)[1]
        prob_a = (forward + (1 - reverse)) / 2

        prediction = Prediction(
            fighter_a=name_a,
            fighter_b=name_b,
            prob_a=prob_a,
            draw=self.draw_rates.get(5 if scheduled_rounds >= 5 else 3, 0.0),
        )

        if self.method is not None:
            methods_a = self.method.probabilities(row_ab)
            methods_b = self.method.probabilities(row_ba)
            prediction.methods_a = {m: prob_a * methods_a[i] for i, m in enumerate(METHODS)}
            prediction.methods_b = {m: (1 - prob_a) * methods_b[i] for i, m in enumerate(METHODS)}
            if ledger_a is not None and ledger_b is not None:
                for method in FINISHES:
                    prediction.techniques_a[method] = technique_distribution(
                        method, ledger_a, ledger_b, self.technique_priors
                    )[:5]
                    prediction.techniques_b[method] = technique_distribution(
                        method, ledger_b, ledger_a, self.technique_priors
                    )[:5]
        return prediction
