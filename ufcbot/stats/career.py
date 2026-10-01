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
from collections import Counter, defaultdict
from dataclasses import dataclass, field, replace
from datetime import date
from typing import TYPE_CHECKING

from ..util import normalise
from .techniques import METHODS

if TYPE_CHECKING:  # pandas is a training dependency; the bot never imports it
    from .dataset import Dataset


# Every fighter's rating starts here and moves with results, so a rating says
# how a fighter has done against the fighters they were in there with rather
# than how many times they won. K is how far one fight can move it.
ELO_START = 1000.0
ELO_K = 32.0
# A finish says more than a decision, so it moves the rating a little further.
ELO_FINISH_BONUS = 1.15


# Divisions by their weight limit. Heavyweights finish each other far more often
# than flyweights do and their fights turn on one punch, so which division a
# fight is in says something no per-fighter number does. Both sources name
# divisions differently ("Lightweight Bout" against "Lightweight"), and a number
# is the one form they agree on.
_DIVISIONS = (
    ("strawweight", 115.0),
    ("flyweight", 125.0),
    ("bantamweight", 135.0),
    ("featherweight", 145.0),
    ("lightweight", 155.0),
    ("welterweight", 170.0),
    ("middleweight", 185.0),
    ("light heavyweight", 205.0),
    ("heavyweight", 265.0),
)


def _division(weight_class: str | None) -> tuple[str, float] | None:
    """Match a division however the source words it. Longest name first, so
    "light heavyweight" is never read as "heavyweight"."""
    text = (weight_class or "").casefold()
    if not text:
        return None
    for name, limit in sorted(_DIVISIONS, key=lambda kv: -len(kv[0])):
        if name in text:
            return name, limit
    return None


def division_weight(weight_class: str | None) -> float:
    """The division's limit in pounds, or NaN for catchweight and open weight."""
    found = _division(weight_class)
    return found[1] if found else float("nan")


def division_name(weight_class: str | None) -> str | None:
    """The division as it is spoken, or None when the fight is at a catchweight.

    The women's divisions are kept separate from the men's: they share a limit
    but never share a cage, so a ranking that mixed them would be nonsense.
    """
    found = _division(weight_class)
    if not found:
        return None
    name = found[0].title()
    return f"Women's {name}" if "women" in (weight_class or "").casefold() else name


def _share(part: float, whole: float) -> float:
    return part / whole if whole > 0 else float("nan")


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

    # Who they have been in there with. Every one of these is folded in after the
    # fight it describes, so a snapshot only ever knows about earlier fights.
    elo: float = ELO_START
    finish_elo: float = ELO_START
    """A second rating, fitted only on whether fights ended early.

    Winning says nothing about *how*. This one scores a stoppage as a win, being
    stopped as a loss and anything that reaches the judges as a draw, so it reads
    as finishing power against the durability it met.
    """
    opponent_elo_sum: float = 0.0
    opponent_elo_count: int = 0
    beaten_elo_sum: float = 0.0
    beaten_count: int = 0
    lost_to_elo_sum: float = 0.0
    lost_to_count: int = 0
    best_win_elo: float = 0.0
    """The highest-rated fighter they have beaten, rated as they stood that night."""

    first_fight: date | None = None
    last_fight: date | None = None
    last_result: str | None = None
    division: str | None = None
    """The division of their most recent fight at a division's limit.

    A catchweight leaves it alone: it says where the fight was made, not where
    the fighter belongs.
    """
    home_division: str | None = None
    """The division they fought in most, which is not always the last one.

    St-Pierre finished at middleweight after twenty-one welterweight fights and
    Jones at heavyweight after twenty-two at light heavyweight. For a board about
    what someone did over a career, the last fight is the wrong one to ask."""

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

    # -- who they have faced -------------------------------------------------

    @property
    def avg_opponent_elo(self) -> float:
        """How good the fighters they have faced were, on average, at the time."""
        return _ratio(self.opponent_elo_sum, self.opponent_elo_count)

    @property
    def avg_beaten_elo(self) -> float:
        return _ratio(self.beaten_elo_sum, self.beaten_count)

    @property
    def avg_lost_to_elo(self) -> float:
        """Losing to good fighters is a different record from losing to poor ones."""
        return _ratio(self.lost_to_elo_sum, self.lost_to_count)

    @property
    def best_win(self) -> float:
        return self.best_win_elo if self.beaten_count else float("nan")

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
        weight_class: str | None = None,
        own: dict | None = None,
        opp: dict | None,
        method_detail: str = "other",
        technique: str | None = None,
        opponent_elo: float | None = None,
        opponent_finish_elo: float | None = None,
    ) -> None:
        """Fold one fight into the totals. ``result`` is win, loss, draw or nc.

        ``opponent_elo`` is the opponent's rating as it stood *before* this fight,
        so both fighters must be handed each other's rating from before either is
        updated.
        """
        self.fights += 1
        if opponent_elo is not None:
            self._rate(result, opponent_elo, method_class)
        if opponent_finish_elo is not None:
            self._rate_finishing(result, opponent_finish_elo, method_class)
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
        division = division_name(weight_class)
        if division:
            self.division = division
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

    def _rate(self, result: str, opponent_elo: float, method_class: str) -> None:
        """Move the rating by this result, and remember how good the opponent was.

        Standard Elo: beating someone rated above you moves it further than
        beating someone below, which is what makes the rating carry strength of
        schedule rather than just a win count. A no contest rates nothing.
        """
        if result == "nc":
            return

        self.opponent_elo_sum += opponent_elo
        self.opponent_elo_count += 1
        if result == "win":
            self.beaten_elo_sum += opponent_elo
            self.beaten_count += 1
            self.best_win_elo = max(self.best_win_elo, opponent_elo)
        elif result == "loss":
            self.lost_to_elo_sum += opponent_elo
            self.lost_to_count += 1

        expected = 1.0 / (1.0 + 10.0 ** ((opponent_elo - self.elo) / 400.0))
        score = {"win": 1.0, "loss": 0.0}.get(result, 0.5)
        k = ELO_K * (ELO_FINISH_BONUS if method_class in ("ko", "sub") else 1.0)
        self.elo += k * (score - expected)

    def _rate_finishing(self, result: str, opponent_elo: float, method_class: str) -> None:
        """Rate how the fight ended rather than who won.

        A stoppage scores a win, being stopped scores a loss, and a fight that
        reaches the judges scores half for both, which is what it was: neither
        could put the other away.
        """
        if result == "nc":
            return
        stopped = method_class in ("ko", "sub")
        if result == "win" and stopped:
            score = 1.0
        elif result == "loss" and stopped:
            score = 0.0
        else:
            score = 0.5
        expected = 1.0 / (1.0 + 10.0 ** ((opponent_elo - self.finish_elo) / 400.0))
        self.finish_elo += ELO_K * (score - expected)


@dataclass(frozen=True, slots=True)
class Rematch:
    """What has passed between two fighters, from the first one's side.

    A rematch is not a fresh fight. Whoever won last time tends to win again,
    and the one avenging a loss usually does it by a different route, so the
    model is handed the previous meetings rather than being left to read two
    records that look like strangers meeting.
    """

    meetings: int = 0
    wins: int = 0
    losses: int = 0
    days_since: float = float("nan")

    def swapped(self) -> Rematch:
        """The same history read from the other fighter's side."""
        return Rematch(self.meetings, self.losses, self.wins, self.days_since)


NO_REMATCH = Rematch()


@dataclass(slots=True)
class Meeting:
    """Every time one pair of fighters has met, keyed by their sorted names."""

    fights: int = 0
    first_wins: int = 0
    """Won by whichever key sorts first, so the record reads the same either way."""
    second_wins: int = 0
    last_on: date | None = None


def pair_key(key_a: str, key_b: str) -> tuple[str, str]:
    return (key_a, key_b) if key_a <= key_b else (key_b, key_a)


def rematch_between(
    meetings: dict[tuple[str, str], Meeting], key_a: str, key_b: str, on: date
) -> Rematch:
    """What A and B have already done to each other, from A's side."""
    met = meetings.get(pair_key(key_a, key_b))
    if met is None or met.fights == 0:
        return NO_REMATCH
    a_first = key_a <= key_b
    wins = met.first_wins if a_first else met.second_wins
    losses = met.second_wins if a_first else met.first_wins
    days = float((on - met.last_on).days) if met.last_on else float("nan")
    return Rematch(meetings=met.fights, wins=wins, losses=losses, days_since=days)


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
    rematch: Rematch = NO_REMATCH
    """Their previous meetings as they stood walking in, from A's side."""


@dataclass(slots=True)
class History:
    ledgers: dict[str, Ledger] = field(default_factory=dict)
    """Final career totals keyed by normalised name."""

    snapshots: list[FightSnapshot] = field(default_factory=list)
    """Pre-fight state for every fight with a decided winner, oldest first."""

    meetings: dict[tuple[str, str], Meeting] = field(default_factory=dict)
    """Who has fought whom, keyed by the sorted pair.

    Held once per pair rather than once per fighter: the question is only ever
    asked about two names at a time, and a pair costs half what two opponent
    lists would."""


def build_history(dataset: Dataset, *, keep_snapshots: bool = True) -> History:
    """Replay every fight in order, collecting pre-fight snapshots and final totals."""
    stats_by_fight: dict[tuple[int, str], dict] = {
        (int(row.fight_id), row.side): row._asdict()
        for row in dataset.fight_stats.itertuples(index=False)
    }

    history = History()
    # Counted here rather than on the ledgers: it is only needed to decide one
    # field, and a counter per fighter would outlive the build inside what the
    # bot then holds in memory.
    division_fights: dict[str, Counter[str]] = defaultdict(Counter)

    for fight in dataset.fights.itertuples(index=False):
        key_a, key_b = normalise(fight.fighter_a), normalise(fight.fighter_b)
        ledger_a = history.ledgers.setdefault(key_a, Ledger(name=fight.fighter_a))
        ledger_b = history.ledgers.setdefault(key_b, Ledger(name=fight.fighter_b))
        on = fight.date.date()
        # Missing techniques come back from pandas as NaN, not None.
        technique = fight.technique if isinstance(fight.technique, str) else None
        # Read before this fight is added, so a snapshot only ever sees meetings
        # that had already happened.
        rematch = rematch_between(history.meetings, key_a, key_b, on)

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
                    rematch=rematch,
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
            "weight_class": str(fight.weight_class),
            "method_detail": str(fight.method_detail),
            "technique": technique,
        }
        # Both ratings are read before either moves, so each fighter is rated
        # against the opponent as they stood walking in.
        elo_a, elo_b = ledger_a.elo, ledger_b.elo
        finish_a, finish_b = ledger_a.finish_elo, ledger_b.finish_elo
        ledger_a.record_fight(
            result=result_a, own=own_a, opp=own_b, opponent_elo=elo_b, opponent_finish_elo=finish_b, **common
        )
        ledger_b.record_fight(
            result=result_b, own=own_b, opp=own_a, opponent_elo=elo_a, opponent_finish_elo=finish_a, **common
        )

        division = division_name(str(fight.weight_class))
        if division:
            division_fights[key_a][division] += 1
            division_fights[key_b][division] += 1

        met = history.meetings.setdefault(pair_key(key_a, key_b), Meeting())
        met.fights += 1
        met.last_on = on
        if fight.winner in ("a", "b"):
            a_first = key_a <= key_b
            if (fight.winner == "a") == a_first:
                met.first_wins += 1
            else:
                met.second_wins += 1

    for key, counts in division_fights.items():
        history.ledgers[key].home_division = counts.most_common(1)[0][0]

    return history
