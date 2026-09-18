"""Running career totals per fighter, and the ufcstats.com metrics derived from them.

ufcstats.com defines its fighter-page numbers as:

    SLpM      significant strikes landed per minute of fight time
    Str. Acc. significant strikes landed / attempted
    SApM      significant strikes absorbed per minute
    Str. Def. 1 - opponent's significant strike accuracy
    TD Avg.   takedowns landed per 15 minutes
    TD Acc.   takedowns landed / attempted
    TD Def.   1 - opponent's takedown accuracy
    Sub. Avg. submission attempts per 15 minutes

Fight time only counts fights that have statistics recorded, matching the site.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from datetime import date
from typing import TYPE_CHECKING

from ..util import normalise
from .techniques import METHODS

if TYPE_CHECKING:  # pandas is a training dependency; the bot never imports it
    from .dataset import Dataset


def _missing(value) -> bool:
    """True for None and NaN, whether it came from Python or from a data frame."""
    return value is None or (isinstance(value, float) and math.isnan(value))


def _nan_to_zero(value) -> float:
    return 0.0 if _missing(value) else float(value)


@dataclass(slots=True)
class FighterInfo:
    """Tale of the tape for one fighter, as ufcstats.com publishes it."""

    name: str
    height_in: float
    weight_lb: float
    reach_in: float
    stance: str | None
    dob: date | None
    ufcstats_url: str | None


def _ratio(numerator: float, denominator: float) -> float:
    return numerator / denominator if denominator > 0 else float("nan")


def _per_minute(count: float, seconds: float, minutes: float = 1.0) -> float:
    return count / (seconds / 60) * minutes if seconds > 0 else float("nan")


@dataclass(slots=True)
class Ledger:
    """Everything known about a fighter up to, but not including, a point in time."""

    name: str
    fights: int = 0
    wins: int = 0
    losses: int = 0
    draws: int = 0
    no_contests: int = 0
    win_streak: int = 0
    loss_streak: int = 0
    wins_ko: int = 0
    wins_sub: int = 0
    wins_dec: int = 0
    losses_ko: int = 0
    losses_sub: int = 0
    losses_dec: int = 0
    title_fights: int = 0
    five_round_fights: int = 0

    # Totals over fights that have statistics recorded.
    stat_fights: int = 0
    seconds: float = 0.0
    sig_landed: float = 0.0
    sig_attempted: float = 0.0
    sig_absorbed: float = 0.0
    sig_faced: float = 0.0
    total_landed: float = 0.0
    td_landed: float = 0.0
    td_attempted: float = 0.0
    td_absorbed: float = 0.0
    td_faced: float = 0.0
    sub_attempts: float = 0.0
    knockdowns: float = 0.0
    knockdowns_absorbed: float = 0.0
    control_seconds: float = 0.0
    controlled_seconds: float = 0.0
    head_landed: float = 0.0
    leg_landed: float = 0.0
    distance_landed: float = 0.0
    ground_landed: float = 0.0

    first_fight: date | None = None
    last_fight: date | None = None
    last_result: str | None = None

    # How fights were won and lost: method -> count, and "method:technique" -> count.
    win_methods: dict[str, int] = field(default_factory=dict)
    loss_methods: dict[str, int] = field(default_factory=dict)
    win_techniques: dict[str, int] = field(default_factory=dict)
    loss_techniques: dict[str, int] = field(default_factory=dict)

    def snapshot(self) -> Ledger:
        copy = replace(self)
        # replace() is shallow; the counters must not be shared with the live ledger.
        copy.win_methods = dict(self.win_methods)
        copy.loss_methods = dict(self.loss_methods)
        copy.win_techniques = dict(self.win_techniques)
        copy.loss_techniques = dict(self.loss_techniques)
        return copy

    # -- ufcstats.com fighter page numbers ----------------------------------

    @property
    def record(self) -> str:
        text = f"{self.wins}-{self.losses}-{self.draws}"
        if self.no_contests:
            text += f" ({self.no_contests} NC)"
        return text

    @property
    def slpm(self) -> float:
        return _per_minute(self.sig_landed, self.seconds)

    @property
    def str_acc(self) -> float:
        return _ratio(self.sig_landed, self.sig_attempted)

    @property
    def sapm(self) -> float:
        return _per_minute(self.sig_absorbed, self.seconds)

    @property
    def str_def(self) -> float:
        faced = _ratio(self.sig_absorbed, self.sig_faced)
        return 1 - faced if not math.isnan(faced) else float("nan")

    @property
    def td_avg(self) -> float:
        return _per_minute(self.td_landed, self.seconds, 15)

    @property
    def td_acc(self) -> float:
        return _ratio(self.td_landed, self.td_attempted)

    @property
    def td_def(self) -> float:
        faced = _ratio(self.td_absorbed, self.td_faced)
        return 1 - faced if not math.isnan(faced) else float("nan")

    @property
    def sub_avg(self) -> float:
        return _per_minute(self.sub_attempts, self.seconds, 15)

    # -- extra rates used by the model --------------------------------------

    @property
    def kd_avg(self) -> float:
        return _per_minute(self.knockdowns, self.seconds, 15)

    @property
    def kd_absorbed_avg(self) -> float:
        return _per_minute(self.knockdowns_absorbed, self.seconds, 15)

    @property
    def control_share(self) -> float:
        return _ratio(self.control_seconds, self.seconds)

    @property
    def controlled_share(self) -> float:
        return _ratio(self.controlled_seconds, self.seconds)

    @property
    def win_rate(self) -> float:
        decided = self.wins + self.losses
        return _ratio(self.wins, decided)

    @property
    def finish_rate(self) -> float:
        return _ratio(self.wins_ko + self.wins_sub, self.wins)

    @property
    def ko_loss_rate(self) -> float:
        return _ratio(self.losses_ko, self.losses)

    @property
    def avg_fight_minutes(self) -> float:
        return _ratio(self.seconds / 60, self.stat_fights)

    def days_since_last_fight(self, on: date) -> float:
        if self.last_fight is None:
            return float("nan")
        return float((on - self.last_fight).days)

    def days_active(self, on: date) -> float:
        if self.first_fight is None:
            return float("nan")
        return float((on - self.first_fight).days)

    # -- update --------------------------------------------------------------

    def record_fight(
        self,
        *,
        on: date,
        result: str,
        method_class: str,
        title_fight: bool,
        scheduled_rounds: int,
        total_seconds: float,
        own: dict | None,
        opp: dict | None,
        method_detail: str = "other",
        technique: str | None = None,
    ) -> None:
        """Fold one fight into the totals. ``result`` is win, loss, draw or nc."""
        self.fights += 1
        if result in ("win", "loss") and method_detail in METHODS:
            methods = self.win_methods if result == "win" else self.loss_methods
            methods[method_detail] = methods.get(method_detail, 0) + 1
            if technique:
                techniques = self.win_techniques if result == "win" else self.loss_techniques
                key = f"{method_detail}:{technique}"
                techniques[key] = techniques.get(key, 0) + 1
        if self.first_fight is None:
            self.first_fight = on
        self.last_fight = on
        self.last_result = result
        if title_fight:
            self.title_fights += 1
        if scheduled_rounds >= 5:
            self.five_round_fights += 1

        if result == "win":
            self.wins += 1
            self.win_streak += 1
            self.loss_streak = 0
            if method_class == "ko":
                self.wins_ko += 1
            elif method_class == "sub":
                self.wins_sub += 1
            elif method_class == "dec":
                self.wins_dec += 1
        elif result == "loss":
            self.losses += 1
            self.loss_streak += 1
            self.win_streak = 0
            if method_class == "ko":
                self.losses_ko += 1
            elif method_class == "sub":
                self.losses_sub += 1
            elif method_class == "dec":
                self.losses_dec += 1
        elif result == "draw":
            self.draws += 1
            self.win_streak = 0
            self.loss_streak = 0
        else:
            self.no_contests += 1

        # Statistics only exist for fights ufcstats.com has data for.
        if own is None or _missing(own.get("sig_a")) or _missing(total_seconds):
            return

        self.stat_fights += 1
        self.seconds += float(total_seconds)
        self.sig_landed += _nan_to_zero(own.get("sig_l"))
        self.sig_attempted += _nan_to_zero(own.get("sig_a"))
        self.total_landed += _nan_to_zero(own.get("tot_l"))
        self.td_landed += _nan_to_zero(own.get("td_l"))
        self.td_attempted += _nan_to_zero(own.get("td_a"))
        self.sub_attempts += _nan_to_zero(own.get("sub_att"))
        self.knockdowns += _nan_to_zero(own.get("kd"))
        self.control_seconds += _nan_to_zero(own.get("ctrl_s"))
        self.head_landed += _nan_to_zero(own.get("head_l"))
        self.leg_landed += _nan_to_zero(own.get("leg_l"))
        self.distance_landed += _nan_to_zero(own.get("dist_l"))
        self.ground_landed += _nan_to_zero(own.get("ground_l"))

        if opp is not None:
            self.sig_absorbed += _nan_to_zero(opp.get("sig_l"))
            self.sig_faced += _nan_to_zero(opp.get("sig_a"))
            self.td_absorbed += _nan_to_zero(opp.get("td_l"))
            self.td_faced += _nan_to_zero(opp.get("td_a"))
            self.knockdowns_absorbed += _nan_to_zero(opp.get("kd"))
            self.controlled_seconds += _nan_to_zero(opp.get("ctrl_s"))


@dataclass(slots=True)
class FightSnapshot:
    """Both fighters' histories as they stood going into one fight."""

    fight_id: int
    on: date
    fighter_a: str
    fighter_b: str
    before_a: Ledger
    before_b: Ledger
    winner: str | None
    title_fight: bool
    scheduled_rounds: int
    weight_class: str
    method_detail: str = "other"
    technique: str | None = None


@dataclass(slots=True)
class History:
    ledgers: dict[str, Ledger] = field(default_factory=dict)
    """Final career totals keyed by normalised name."""

    snapshots: list[FightSnapshot] = field(default_factory=list)
    """Pre-fight state for every fight with a decided winner, oldest first."""


def build_history(dataset: Dataset, *, keep_snapshots: bool = True) -> History:
    """Replay every fight in order, collecting pre-fight snapshots and final totals."""
    stats_by_fight: dict[tuple[int, str], dict] = {
        (int(row.fight_id), row.side): row._asdict()
        for row in dataset.fight_stats.itertuples(index=False)
    }

    history = History()

    for fight in dataset.fights.itertuples(index=False):
        key_a, key_b = normalise(fight.fighter_a), normalise(fight.fighter_b)
        ledger_a = history.ledgers.setdefault(key_a, Ledger(name=fight.fighter_a))
        ledger_b = history.ledgers.setdefault(key_b, Ledger(name=fight.fighter_b))
        on = fight.date.date()
        # Missing techniques come back from pandas as NaN, not None.
        technique = fight.technique if isinstance(fight.technique, str) else None

        if keep_snapshots and fight.winner in ("a", "b"):
            history.snapshots.append(
                FightSnapshot(
                    fight_id=int(fight.fight_id),
                    on=on,
                    fighter_a=fight.fighter_a,
                    fighter_b=fight.fighter_b,
                    before_a=ledger_a.snapshot(),
                    before_b=ledger_b.snapshot(),
                    winner=fight.winner,
                    title_fight=bool(fight.title_fight),
                    scheduled_rounds=int(fight.scheduled_rounds),
                    weight_class=str(fight.weight_class),
                    method_detail=str(fight.method_detail),
                    technique=technique,
                )
            )

        own_a = stats_by_fight.get((int(fight.fight_id), "a"))
        own_b = stats_by_fight.get((int(fight.fight_id), "b"))

        if fight.outcome == "win":
            result_a = "win" if fight.winner == "a" else "loss"
            result_b = "win" if fight.winner == "b" else "loss"
        else:
            result_a = result_b = fight.outcome

        common = {
            "on": on,
            "method_class": fight.method_class,
            "title_fight": bool(fight.title_fight),
            "scheduled_rounds": int(fight.scheduled_rounds),
            "total_seconds": fight.total_seconds,
            "method_detail": str(fight.method_detail),
            "technique": technique,
        }
        ledger_a.record_fight(result=result_a, own=own_a, opp=own_b, **common)
        ledger_b.record_fight(result=result_b, own=own_b, opp=own_a, **common)

    return history
