"""Turn two fighters' histories into the numeric row the model consumes.

Every feature is computed from what was known before the fight, so training rows
never see the outcome they are predicting. Missing values stay NaN; the model
handles them natively.
"""

from __future__ import annotations

import math
from datetime import date

from .career import FighterInfo, Ledger, division_weight

NAN = float("nan")


def _share(part: float, whole: float) -> float:
    return part / whole if whole > 0 else NAN


def fighter_features(ledger: Ledger, info: FighterInfo | None, on: date) -> dict[str, float]:
    """One fighter's side of the matchup as plain numbers."""
    age = NAN
    height = reach = weight = NAN
    southpaw = switch = NAN
    if info is not None:
        if info.dob is not None:
            age = (on - info.dob).days / 365.25
        height, reach, weight = info.height_in, info.reach_in, info.weight_lb
        if info.stance:
            stance = info.stance.lower()
            southpaw = 1.0 if "southpaw" in stance else 0.0
            switch = 1.0 if "switch" in stance else 0.0

    sig = ledger.sig_landed
    last_win = NAN
    if ledger.last_result == "win":
        last_win = 1.0
    elif ledger.last_result == "loss":
        last_win = 0.0

    return {
        "age": age,
        "height": height,
        "reach": reach,
        "weight": weight,
        "southpaw": southpaw,
        "switch": switch,
        "fights": float(ledger.fights),
        "wins": float(ledger.wins),
        "losses": float(ledger.losses),
        "win_rate": ledger.win_rate,
        "win_streak": float(ledger.win_streak),
        "loss_streak": float(ledger.loss_streak),
        "last_win": last_win,
        "title_fights": float(ledger.title_fights),
        "five_round_fights": float(ledger.five_round_fights),
        "slpm": ledger.slpm,
        "str_acc": ledger.str_acc,
        "sapm": ledger.sapm,
        "str_def": ledger.str_def,
        "td_avg": ledger.td_avg,
        "td_acc": ledger.td_acc,
        "td_def": ledger.td_def,
        "sub_avg": ledger.sub_avg,
        "kd_avg": ledger.kd_avg,
        "kd_absorbed_avg": ledger.kd_absorbed_avg,
        "control_share": ledger.control_share,
        "controlled_share": ledger.controlled_share,
        "finish_rate": ledger.finish_rate,
        "ko_win_rate": _share(ledger.wins_ko, ledger.wins),
        "sub_win_rate": _share(ledger.wins_sub, ledger.wins),
        "ko_loss_rate": ledger.ko_loss_rate,
        "sub_loss_rate": _share(ledger.losses_sub, ledger.losses),
        "avg_fight_minutes": ledger.avg_fight_minutes,
        "total_minutes": ledger.seconds / 60 if ledger.seconds else 0.0,
        "days_since_last_fight": ledger.days_since_last_fight(on),
        "days_active": ledger.days_active(on),
        "head_share": _share(ledger.head_landed, sig),
        "leg_share": _share(ledger.leg_landed, sig),
        "ground_share": _share(ledger.ground_landed, sig),
        "distance_share": _share(ledger.distance_landed, sig),
        "sig_per_total": _share(ledger.sig_landed, ledger.total_landed),
        "striking_differential": _diff_or_nan(ledger.slpm, ledger.sapm),
        # Who they have been in there with. A 10-0 record against nobody and a
        # 10-0 record through contenders read the same in every feature above.
        "elo": ledger.elo,
        "opponent_elo": ledger.avg_opponent_elo,
        "beaten_elo": ledger.avg_beaten_elo,
        "lost_to_elo": ledger.avg_lost_to_elo,
        "best_win_elo": ledger.best_win,
        "elo_over_opponents": _diff_or_nan(ledger.elo, ledger.avg_opponent_elo),
        # Finishing power against the durability it has met, rated the same way.
        "finish_elo": ledger.finish_elo,
    }


def _diff_or_nan(a: float, b: float) -> float:
    if math.isnan(a) or math.isnan(b):
        return NAN
    return a - b


PER_FIGHTER = list(fighter_features(Ledger(name="_"), None, date(2000, 1, 1)).keys())

# How one fighter's offence meets the other's defence. Everything above is each
# fighter measured on their own, and the differences between them, which says
# who is the better wrestler but never whether this wrestler can take this
# opponent down. A rate against the rate that opposes it is the quantity a
# matchup actually turns on, and neither model can build it from a difference.
MATCHUPS = (
    # Strikes a minute, against how many the opponent usually avoids.
    ("strikes_expected", "slpm", "str_def", "scaled"),
    # Takedowns per 15, against takedown defence.
    ("takedowns_expected", "td_avg", "td_def", "scaled"),
    # Control time, against how much the opponent usually spends underneath.
    ("control_expected", "control_share", "controlled_share", "product"),
    # Power against chin, and submission threat against a suspect neck.
    ("ko_threat", "kd_avg", "ko_loss_rate", "product"),
    ("sub_threat", "sub_avg", "sub_loss_rate", "product"),
    # Accuracy and defence are both plain rates, so their difference means something.
    ("striking_edge", "str_acc", "str_def", "difference"),
    ("wrestling_edge", "td_acc", "td_def", "difference"),
)


def _matchup_value(kind: str, attack: float, defence: float) -> float:
    if attack != attack or defence != defence:  # NaN either side
        return NAN
    if kind == "scaled":
        return attack * (1.0 - defence)
    if kind == "product":
        return attack * defence
    return attack - defence


CONTEXT = ["title_fight", "scheduled_rounds", "division"]

FEATURE_NAMES = (
    [f"a_{name}" for name in PER_FIGHTER]
    + [f"b_{name}" for name in PER_FIGHTER]
    + [f"d_{name}" for name in PER_FIGHTER]
    + [f"m_a_{name}" for name, _a, _d, _k in MATCHUPS]
    + [f"m_b_{name}" for name, _a, _d, _k in MATCHUPS]
    + CONTEXT
)


# What each input is, in words. The model works in column names; anything shown
# to a reader needs the English.
FEATURE_LABELS = {
    "age": "age",
    "height": "height",
    "reach": "reach",
    "weight": "weight",
    "southpaw": "southpaw stance",
    "switch": "switch stance",
    "fights": "UFC experience",
    "wins": "wins",
    "losses": "losses",
    "win_rate": "win rate",
    "win_streak": "winning streak",
    "loss_streak": "losing streak",
    "last_win": "won last time out",
    "title_fights": "title fights",
    "five_round_fights": "five-round fights",
    "slpm": "strikes landed a minute",
    "str_acc": "striking accuracy",
    "sapm": "strikes absorbed a minute",
    "str_def": "striking defence",
    "td_avg": "takedowns per 15 minutes",
    "td_acc": "takedown accuracy",
    "td_def": "takedown defence",
    "sub_avg": "submission attempts",
    "kd_avg": "knockdowns scored",
    "kd_absorbed_avg": "knockdowns taken",
    "control_share": "control time",
    "controlled_share": "time spent underneath",
    "finish_rate": "finish rate",
    "ko_win_rate": "wins by knockout",
    "sub_win_rate": "wins by submission",
    "ko_loss_rate": "losses by knockout",
    "sub_loss_rate": "losses by submission",
    "avg_fight_minutes": "average fight length",
    "total_minutes": "career fight time",
    "days_since_last_fight": "layoff",
    "days_active": "time in the UFC",
    "head_share": "strikes to the head",
    "leg_share": "strikes to the legs",
    "ground_share": "strikes on the ground",
    "distance_share": "strikes at distance",
    "sig_per_total": "share of strikes that count",
    "striking_differential": "striking differential",
    "elo": "rating",
    "opponent_elo": "quality of opposition",
    "beaten_elo": "quality of wins",
    "lost_to_elo": "quality of losses",
    "best_win_elo": "best win",
    "elo_over_opponents": "rating above their opposition",
    "finish_elo": "finishing rating",
}


def describe_feature(name: str) -> str:
    """"d_td_def" -> "takedown defence". Falls back to the column name, tidied."""
    bare = name[2:] if name[:2] in ("a_", "b_", "d_") else name
    return FEATURE_LABELS.get(bare, bare.replace("_", " "))


def matchup_row(
    a: dict[str, float],
    b: dict[str, float],
    *,
    title_fight: bool,
    scheduled_rounds: int,
    weight_class: str | None = None,
) -> list[float]:
    """Both sides plus their differences, in FEATURE_NAMES order."""
    row: list[float] = []
    row.extend(a[name] for name in PER_FIGHTER)
    row.extend(b[name] for name in PER_FIGHTER)
    row.extend(_diff_or_nan(a[name], b[name]) for name in PER_FIGHTER)
    row.extend(_matchup_value(kind, a[attack], b[defence]) for _n, attack, defence, kind in MATCHUPS)
    row.extend(_matchup_value(kind, b[attack], a[defence]) for _n, attack, defence, kind in MATCHUPS)
    row.append(1.0 if title_fight else 0.0)
    row.append(float(scheduled_rounds))
    row.append(division_weight(weight_class))
    return row
