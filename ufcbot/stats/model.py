"""Train and evaluate the fight-outcome models, then compile them for the bot.

Two models work together. The winner model gives each fighter's probability of
winning. The method model, trained only on how winners won, splits that into
KO/TKO, submission, unanimous decision and split decision. A finishing technique
is then estimated from the winner's own finishes, the loser's history of being
finished, and the league-wide base rate.

Training happens in the refresh job's own process, so scikit-learn and pandas
stay out of the bot. What comes back is a :class:`~ufcbot.stats.scorer.CompiledModel`:
the same trees and coefficients written as plain numbers, checked here against
the fitted estimators before it is saved.
"""

from __future__ import annotations

import logging
from array import array
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime

import numpy as np
import pandas as pd
from sklearn.base import BaseEstimator, ClassifierMixin
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import accuracy_score, brier_score_loss, log_loss
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from ..util import normalise
from .career import FighterInfo, FightSnapshot, History
from .features import FEATURE_NAMES, fighter_features, matchup_row
from .prediction import (
    DEFAULT_DRAW_RATES,
    IGNORED_TECHNIQUES,
    Evaluation,
    technique_distribution,
)
from .scorer import Blend, Boost, CompiledModel, Linear, Tree
from .techniques import FINISHES, METHODS, same_technique

log = logging.getLogger(__name__)

# Fights before unified rules finished very differently; leave them out of the method model.
METHOD_TRAINING_START = date(2001, 1, 1)
# Technique base rates come from the modern era only.
PRIOR_START = date(2015, 1, 1)
# A compiled model must agree with the estimator it came from this closely.
PARITY_TOLERANCE = 1e-9
# How many held-out rows the parity check scores through both paths.
PARITY_ROWS = 400


def _boost(seed: int, *, iterations: int = 500, min_leaf: int = 100) -> HistGradientBoostingClassifier:
    return HistGradientBoostingClassifier(
        learning_rate=0.05,
        max_iter=iterations,
        max_leaf_nodes=6,
        min_samples_leaf=min_leaf,
        l2_regularization=3.0,
        early_stopping=False,
        random_state=seed,
    )


def _linear():
    return make_pipeline(
        SimpleImputer(strategy="median"),
        StandardScaler(),
        LogisticRegression(C=0.02, max_iter=3000),
    )


class BlendModel(ClassifierMixin, BaseEstimator):
    """Who wins: gradient boosting blended with logistic regression.

    On held-out fights boosting is the more accurate of the two and logistic
    regression the better calibrated. Averaging their probabilities beat both
    on accuracy, log loss and Brier score, and produced probabilities that
    track real win rates bin for bin.
    """

    def __init__(self, boost_weight: float = 0.35, seed: int = 7) -> None:
        self.boost_weight = boost_weight
        self.seed = seed

    def fit(self, X, y):
        self.classes_ = np.array([0, 1])
        self.boost_ = _boost(self.seed).fit(X, y)
        self.linear_ = _linear().fit(X, y)
        return self

    def predict_proba(self, X):
        boosted = self.boost_.predict_proba(X)[:, 1]
        linear = self.linear_.predict_proba(X)[:, 1]
        positive = self.boost_weight * boosted + (1 - self.boost_weight) * linear
        return np.column_stack([1 - positive, positive])

    def predict(self, X):
        return (self.predict_proba(X)[:, 1] >= 0.5).astype(int)


class MethodModel(ClassifierMixin, BaseEstimator):
    """How the winner wins, given the matchup row ordered winner first.

    Classes follow ``METHODS``: KO/TKO, submission, unanimous and split decision.
    """

    def __init__(self, boost_weight: float = 0.35, seed: int = 7) -> None:
        self.boost_weight = boost_weight
        self.seed = seed

    def fit(self, X, y):
        self.classes_ = np.arange(len(METHODS))
        self.boost_ = _boost(self.seed, iterations=300, min_leaf=80).fit(X, y)
        self.linear_ = _linear().fit(X, y)
        return self

    @staticmethod
    def _aligned(model, X) -> np.ndarray:
        probs = model.predict_proba(X)
        out = np.zeros((probs.shape[0], len(METHODS)))
        out[:, np.asarray(model.classes_, dtype=int)] = probs
        return out

    def predict_proba(self, X):
        return self.boost_weight * self._aligned(self.boost_, X) + (1 - self.boost_weight) * self._aligned(
            self.linear_, X
        )

    def predict(self, X):
        return self.predict_proba(X).argmax(axis=1)


# -- compiling a fitted model ---------------------------------------------------


class CompileError(RuntimeError):
    """A fitted model could not be turned into a compiled one."""


def _tree_from_nodes(nodes) -> Tree:
    if nodes["is_categorical"].any():
        raise CompileError("categorical splits are not supported by the compiled model")
    return Tree(
        feature=array("i", (int(v) for v in nodes["feature_idx"])),
        threshold=array("d", (float(v) for v in nodes["num_threshold"])),
        left=array("I", (int(v) for v in nodes["left"])),
        right=array("I", (int(v) for v in nodes["right"])),
        value=array("d", (float(v) for v in nodes["value"])),
        leaf=bytes(int(v) for v in nodes["is_leaf"]),
        missing_left=bytes(int(v) for v in nodes["missing_go_to_left"]),
    )


def _compile_boost(estimator: HistGradientBoostingClassifier) -> Boost:
    baseline = [float(value) for value in np.asarray(estimator._baseline_prediction).ravel()]
    stages = [[_tree_from_nodes(tree.nodes) for tree in stage] for stage in estimator._predictors]
    classes = [int(c) for c in estimator.classes_]
    # Binary boosting keeps one score for two classes; everything else is one per class.
    if len(baseline) != (1 if len(classes) == 2 else len(classes)):
        raise CompileError("unexpected boosting shape")
    return Boost(baseline=baseline, stages=stages, classes=classes)


def _compile_linear(pipeline) -> Linear:
    imputer, scaler, logistic = (step for _, step in pipeline.steps)
    width = len(FEATURE_NAMES)
    if len(imputer.statistics_) != width or len(scaler.mean_) != width or logistic.coef_.shape[1] != width:
        # SimpleImputer drops a feature that was missing in every training row.
        raise CompileError("the fitted pipeline does not cover every feature")
    return Linear(
        medians=[float(v) for v in imputer.statistics_],
        means=[float(v) for v in scaler.mean_],
        scales=[float(v) for v in scaler.scale_],
        coefficients=[[float(c) for c in row] for row in logistic.coef_],
        intercepts=[float(v) for v in logistic.intercept_],
        classes=[int(c) for c in logistic.classes_],
    )


def _compile_blend(model: BlendModel | MethodModel, width: int) -> Blend:
    return Blend(
        boost_weight=float(model.boost_weight),
        boost=_compile_boost(model.boost_),
        linear=_compile_linear(model.linear_),
        width=width,
    )


def _check_parity(name: str, model, blend: Blend, X: np.ndarray) -> None:
    """Score the same rows both ways and refuse a compiled model that disagrees."""
    if not len(X):
        return
    sample = X[:: max(1, len(X) // PARITY_ROWS)][:PARITY_ROWS]
    reference = model.predict_proba(sample)
    worst = 0.0
    for row, expected in zip(sample.tolist(), reference):
        got = blend.probabilities(row)
        worst = max(worst, max(abs(a - b) for a, b in zip(got, expected)))
    if worst > PARITY_TOLERANCE:
        raise CompileError(f"compiled {name} model differs from the fitted one by {worst:.2e}")
    log.info("Compiled %s model matches the fitted one (worst difference %.2e)", name, worst)


# -- training -------------------------------------------------------------------


def _design_matrix(
    history: History, fighters: dict[str, FighterInfo]
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Rows for every decided fight, mirrored so the model sees both corners.

    Returns X, y, fight dates and a baseline pick (1 if A had the better win rate).
    """
    rows: list[list[float]] = []
    labels: list[int] = []
    dates: list[date] = []
    baseline: list[float] = []

    for snap in history.snapshots:
        info_a = fighters.get(normalise(snap.fighter_a))
        info_b = fighters.get(normalise(snap.fighter_b))
        fa = fighter_features(snap.before_a, info_a, snap.on)
        fb = fighter_features(snap.before_b, info_b, snap.on)
        y = 1 if snap.winner == "a" else 0
        ctx = {"title_fight": snap.title_fight, "scheduled_rounds": snap.scheduled_rounds}

        rows.append(matchup_row(fa, fb, **ctx))
        labels.append(y)
        rows.append(matchup_row(fb, fa, **ctx))
        labels.append(1 - y)
        dates.extend([snap.on, snap.on])

        wa, wb = snap.before_a.win_rate, snap.before_b.win_rate
        if np.isnan(wa) or np.isnan(wb) or wa == wb:
            pick = 0.5
        else:
            pick = 1.0 if wa > wb else 0.0
        baseline.extend([pick, 1 - pick])

    return (
        np.array(rows, dtype=float),
        np.array(labels, dtype=int),
        np.array(dates),
        np.array(baseline, dtype=float),
    )


@dataclass(slots=True)
class _MethodRows:
    winner_first: np.ndarray
    a_first: np.ndarray
    b_first: np.ndarray
    labels: np.ndarray
    dates: np.ndarray
    winner_side: np.ndarray
    snapshots: list[FightSnapshot]


def _method_rows(history: History, fighters: dict[str, FighterInfo]) -> _MethodRows:
    """One row per decided fight with a known method, ordered winner first."""
    winner_first, a_first, b_first, labels, dates, sides, snaps = [], [], [], [], [], [], []
    index = {m: i for i, m in enumerate(METHODS)}

    for snap in history.snapshots:
        if snap.method_detail not in index:
            continue
        fa = fighter_features(snap.before_a, fighters.get(normalise(snap.fighter_a)), snap.on)
        fb = fighter_features(snap.before_b, fighters.get(normalise(snap.fighter_b)), snap.on)
        ctx = {"title_fight": snap.title_fight, "scheduled_rounds": snap.scheduled_rounds}
        row_ab = matchup_row(fa, fb, **ctx)
        row_ba = matchup_row(fb, fa, **ctx)
        winner_first.append(row_ab if snap.winner == "a" else row_ba)
        a_first.append(row_ab)
        b_first.append(row_ba)
        labels.append(index[snap.method_detail])
        dates.append(snap.on)
        sides.append(snap.winner)
        snaps.append(snap)

    return _MethodRows(
        winner_first=np.array(winner_first, dtype=float),
        a_first=np.array(a_first, dtype=float),
        b_first=np.array(b_first, dtype=float),
        labels=np.array(labels, dtype=int),
        dates=np.array(dates),
        winner_side=np.array(sides),
        snapshots=snaps,
    )


def _technique_priors(snapshots: list[FightSnapshot], *, before: date | None) -> dict[str, dict[str, float]]:
    counts: dict[str, Counter] = {method: Counter() for method in FINISHES}
    for snap in snapshots:
        if snap.on < PRIOR_START or (before is not None and snap.on >= before):
            continue
        if snap.method_detail in counts and snap.technique and snap.technique not in IGNORED_TECHNIQUES:
            counts[snap.method_detail][snap.technique] += 1
    priors = {}
    for method, counter in counts.items():
        total = sum(counter.values())
        if total:
            priors[method] = {tech: n / total for tech, n in counter.items()}
    return priors


def _draw_rates(fights: pd.DataFrame | None) -> dict[int, float]:
    if fights is None or fights.empty:
        return dict(DEFAULT_DRAW_RATES)
    recent = fights[fights["date"] >= pd.Timestamp(2010, 1, 1)]
    rates = {}
    for rounds in (3, 5):
        subset = recent[recent["scheduled_rounds"] == rounds]
        rates[rounds] = float((subset["outcome"] == "draw").mean()) if len(subset) else DEFAULT_DRAW_RATES[rounds]
    return rates


def train(
    history: History,
    fighters: dict[str, FighterInfo],
    *,
    dataset_newest: date | None,
    holdout_from: date | None = None,
    compute_importances: bool = True,
    fights: pd.DataFrame | None = None,
) -> CompiledModel:
    """Fit both models on every decided fight and compile them for the bot.

    When ``holdout_from`` is given, fights from that date on are first held out to
    measure accuracy honestly, then the final models are refit on everything.
    """
    X, y, dates, baseline = _design_matrix(history, fighters)
    method_rows = _method_rows(history, fighters)
    log.info("Training matrix: %d rows x %d features", X.shape[0], X.shape[1])

    evaluation = None
    importances: list[tuple[str, float]] = []

    if holdout_from is not None:
        test = dates >= holdout_from
        train_mask = ~test
        if test.sum() >= 50 and train_mask.sum() >= 500:
            model = BlendModel().fit(X[train_mask], y[train_mask])
            probs = model.predict_proba(X[test])[:, 1]
            # Each fight appears twice; the mirrored rows simply double-count,
            # which leaves every metric unchanged.
            evaluation = Evaluation(
                holdout_from=holdout_from,
                fights=int(test.sum() // 2),
                accuracy=float(accuracy_score(y[test], probs >= 0.5)),
                log_loss=float(log_loss(y[test], probs)),
                brier=float(brier_score_loss(y[test], probs)),
                baseline_accuracy=float(np.mean((baseline[test] >= 0.5) == (y[test] == 1))),
            )
            log.info("Holdout: %s", evaluation.summary())

            _evaluate_methods(evaluation, model, method_rows, holdout_from)
            if evaluation.method_accuracy is not None:
                log.info("Holdout: %s", evaluation.method_summary())

            if compute_importances:
                importances = _permutation_importance(model, X[test], y[test])

    final = BlendModel().fit(X, y)
    eligible = method_rows.dates >= METHOD_TRAINING_START
    method_model = MethodModel().fit(method_rows.winner_first[eligible], method_rows.labels[eligible])

    winner_blend = _compile_blend(final, width=2)
    method_blend = _compile_blend(method_model, width=len(METHODS))
    _check_parity("winner", final, winner_blend, X)
    _check_parity("method", method_model, method_blend, method_rows.winner_first)

    return CompiledModel(
        winner=winner_blend,
        method=method_blend,
        feature_names=list(FEATURE_NAMES),
        trained_at=datetime.now(),
        training_fights=int(len(y) // 2),
        dataset_newest=dataset_newest,
        evaluation=evaluation,
        importances=importances,
        technique_priors=_technique_priors(history.snapshots, before=None),
        draw_rates=_draw_rates(fights),
    )


def _evaluate_methods(
    evaluation: Evaluation, winner_model: BlendModel, rows: _MethodRows, holdout_from: date
) -> None:
    train_mask = (rows.dates < holdout_from) & (rows.dates >= METHOD_TRAINING_START)
    test_mask = rows.dates >= holdout_from
    if test_mask.sum() < 50 or train_mask.sum() < 500:
        return

    method_model = MethodModel().fit(rows.winner_first[train_mask], rows.labels[train_mask])
    labels = rows.labels[test_mask]
    a_wins = rows.winner_side[test_mask] == "a"

    # Method, told who won.
    given_winner = method_model.predict_proba(rows.winner_first[test_mask])
    evaluation.method_fights = int(test_mask.sum())
    evaluation.method_accuracy = float(np.mean(given_winner.argmax(axis=1) == labels))
    most_common = Counter(rows.labels[train_mask]).most_common(1)[0][0]
    evaluation.method_baseline = float(np.mean(labels == most_common))

    # Winner and method together, the way picks are actually made.
    a_rows, b_rows = rows.a_first[test_mask], rows.b_first[test_mask]
    pa = (winner_model.predict_proba(a_rows)[:, 1] + 1 - winner_model.predict_proba(b_rows)[:, 1]) / 2
    joint = np.hstack(
        [pa[:, None] * method_model.predict_proba(a_rows), (1 - pa)[:, None] * method_model.predict_proba(b_rows)]
    )
    truth = labels + np.where(a_wins, 0, len(METHODS))
    evaluation.exact_accuracy = float(np.mean(joint.argmax(axis=1) == truth))

    # Technique, told the winner and that it was that kind of finish.
    priors = _technique_priors(rows.snapshots, before=holdout_from)
    hits = base_hits = scored = 0
    test_snaps = [s for s, keep in zip(rows.snapshots, test_mask) if keep]
    for snap in test_snaps:
        if snap.method_detail not in FINISHES or not snap.technique or snap.technique in IGNORED_TECHNIQUES:
            continue
        winner, loser = (snap.before_a, snap.before_b) if snap.winner == "a" else (snap.before_b, snap.before_a)
        distribution = technique_distribution(snap.method_detail, winner, loser, priors)
        base = sorted((priors.get(snap.method_detail) or {}).items(), key=lambda kv: kv[1], reverse=True)
        if not distribution or not base:
            continue
        scored += 1
        hits += bool(same_technique(distribution[0][0], snap.technique))
        base_hits += bool(same_technique(base[0][0], snap.technique))
    if scored:
        evaluation.technique_accuracy = hits / scored
        evaluation.technique_baseline = base_hits / scored


def _permutation_importance(
    model: BlendModel, X: np.ndarray, y: np.ndarray, top: int = 15
) -> list[tuple[str, float]]:
    """Which difference features move the log loss most when scrambled."""
    from sklearn.inspection import permutation_importance

    diff_columns = [i for i, name in enumerate(FEATURE_NAMES) if name.startswith("d_")]
    result = permutation_importance(
        model, X, y, scoring="neg_log_loss", n_repeats=3, random_state=7, n_jobs=1
    )
    scored = [(FEATURE_NAMES[i], float(result.importances_mean[i])) for i in diff_columns]
    scored.sort(key=lambda kv: kv[1], reverse=True)
    return scored[:top]


__all__ = ["BlendModel", "CompileError", "CompiledModel", "MethodModel", "train"]
