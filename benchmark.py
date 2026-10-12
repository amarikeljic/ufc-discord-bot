"""Score the winner model against the sportsbook, not against a baseline.

Run with:  python benchmark.py
           python benchmark.py --from 2024 --block 10

Every other evaluation in this project asks whether one version of the model
beats another. This asks the only question with an outside answer in it: does
the model know anything the market does not? The market is thousands of people
with money at stake, so what comes back is a ceiling made of something other
than our own opinion.

Needs ``python odds.py`` to have run. Reads the same dataset and builds the same
design matrix as ``evaluate.py``, under the same rolling-origin protocol: every
event scored once by a model fitted only on fights before it, and the
hyperparameters for a block chosen on a slice the block never joins.

What it found when it was written, on 910 fights across 85 cards from 2024:

    model          0.6254 nats   64.8%
    opening line   0.6001 nats   67.7%
    closing line   0.5931 nats   69.7%

    model minus closing  +0.0323 nats  95% [+0.0132, +0.0513]

The market wins, conclusively, and a blend of the two at any weight is
indistinguishable from the market alone, so the model holds nothing the price
does not already carry. The deficit is all in lopsided fights -- +0.0726 nats
95% [+0.0392, +0.1040] where the market is past 70% -- because the model will
not commit: it sits 0.124 from even money on average where the market sits
0.175. Neither a wider regularisation grid nor Platt scaling moved that, so what
is missing is information rather than tuning.

Two numbers are worth re-running for. Backing the model's pick at the opening
price returned +2.29% over those fights where backing the favourite returned
-1.91%, the only positive figure here and still not distinguishable from zero:
95% [-3.21%, +7.90%]. And the model leans the way the line later moves on 54.4%
of the fights where it moves at all, 95% [50.5%, 58.5%], which is a real signal
and a small one.

The practical finding is a negative. An expected-value pick -- staking wherever
the model disagrees with the price -- loses 5 to 7% and is right on 38% of the
fights the two disagree about. Pick'em should not offer one.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import time
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

import numpy as np
from dotenv import load_dotenv

from odds import ODDS_FILE, PRE_FIGHT

K_VALUES = (32.0, 64.0, 128.0)
C_VALUES = (0.005, 0.02, 0.08)
STAKE = 100.0
"""What pick'em risks on a pick, so the units below are its points."""

log = logging.getLogger("benchmark")


# -- the market ---------------------------------------------------------------


def _american(side: dict | None, which: str) -> int | None:
    """``side[which]['moneyLine']['american']``, which is where ESPN keeps it.

    Nested two deep, and the first pass at this read one level and concluded
    the archive had no prices in it at all.
    """
    got = side.get(which) if isinstance(side, dict) else None
    if not isinstance(got, dict):
        return None
    line = got.get("moneyLine")
    raw = line.get("american") if isinstance(line, dict) else line
    if raw in (None, "", "EVEN", "even"):
        return None
    try:
        return int(str(raw).replace("+", "").strip())
    except (TypeError, ValueError):
        return None


def _athlete(side: dict | None) -> str | None:
    """Which fighter a side of the market is about.

    Out of the odds payload rather than the competition's corners, because ESPN
    does not fill in home and away for MMA and both corners come back as one.
    """
    ref = ((side or {}).get("athlete") or {}).get("$ref") or ""
    return ref.rstrip("/").split("/")[-1].split("?")[0] if ref else None


def _implied(moneyline: int) -> float:
    """A moneyline as a probability, the book's margin still in it."""
    return 100 / (moneyline + 100) if moneyline > 0 else -moneyline / (-moneyline + 100)


def market(archive: dict, which: str) -> dict:
    """Bout key -> a probability and a raw moneyline per athlete id.

    De-vigged by normalising the pair to sum to one. The margin is the book's
    fee rather than its opinion, and leaving it in would make the market look
    badly calibrated when it is only charging; it runs about 4.5% here. Profit
    further down uses the raw moneyline instead, because the fee is what you
    actually pay.
    """
    out = {}
    for key, bout in archive.items():
        book = next(
            (b for name in PRE_FIGHT for b in bout["books"] if b["provider"] == name), None
        )
        if book is None:
            continue
        home, away = book.get("home") or {}, book.get("away") or {}
        ids = (_athlete(home), _athlete(away))
        lines = (_american(home, which), _american(away, which))
        if not all(ids) or any(line is None for line in lines):
            continue
        raw = [_implied(line) for line in lines]
        total = sum(raw)
        if not total:
            continue
        out[key] = {
            "date": bout["date"],
            "p": {ids[i]: raw[i] / total for i in (0, 1)},
            "ml": {ids[i]: lines[i] for i in (0, 1)},
        }
    return out


# -- the model ----------------------------------------------------------------


def designs_by_k(data_dir: Path, ks=K_VALUES):
    """One design matrix per candidate K, plus the bout behind each row pair."""
    from ufcbot.stats import career as career_mod
    from ufcbot.stats import dataset as ds
    from ufcbot.stats import graph as graph_mod
    from ufcbot.stats.career import build_history
    from ufcbot.stats.model import _design_matrix
    from ufcbot.stats.rolling import Design
    from ufcbot.util import normalise

    dataset = ds.load(data_dir)
    out, keys = {}, None
    for k in ks:
        started = time.time()
        career_mod.ELO_K = k
        # A graph per pass, never shared. Its cursor only moves forward, so a
        # second pass over a used one applies nothing and hands every fight the
        # rating from the end of the data -- its own result included.
        history = build_history(
            dataset, keep_snapshots=True, graph=graph_mod.load(data_dir, normalise=normalise)
        )
        x, y, dates, _ = _design_matrix(history, dataset.fighters)
        out[k] = Design(x=x, y=y, dates=dates)
        if keys is None:
            keys = [
                (snap.on, normalise(snap.fighter_a), normalise(snap.fighter_b))
                for snap in history.snapshots
            ]
        print(f"  K={k:<6g} {x.shape[0]:>6} rows x {x.shape[1]} features   "
              f"{time.time() - started:.0f}s")
    career_mod.ELO_K = 32.0
    return out, keys


def walk(designs, keys, *, start: date, block_events: int):
    """Out-of-sample probabilities, keyed by the bout rather than the row.

    Each fight is scored from both corners and averaged, which is what the bot
    does, so that swapping the two names cannot change the answer.
    """
    from ufcbot.stats.model import _linear
    from ufcbot.stats.rolling import (
        MIN_TRAIN_FIGHTS,
        VALIDATION_DAYS,
        Candidate,
        _select,
    )

    def fit(x, y, c):
        return _linear(c).fit(x, y)

    candidates = [Candidate(k, c) for k in K_VALUES for c in C_VALUES]
    dates = designs[candidates[0].k].dates
    events = np.unique(dates[dates >= start])
    out, chose = {}, []
    for first in range(0, len(events), block_events):
        block = events[first : first + block_events]
        test = np.isin(dates, block)
        window = dates < block[0]
        if window.sum() < MIN_TRAIN_FIGHTS:
            continue
        cutoff = block[0] - timedelta(days=VALIDATION_DAYS)
        slice_, inner = window & (dates >= cutoff), window & (dates < cutoff)
        if slice_.any() and inner.sum() >= MIN_TRAIN_FIGHTS:
            chosen, _ = _select(designs, candidates, inner, slice_, fit)
        else:
            chosen = candidates[0]
        chose.append(chosen)
        design = designs[chosen.k]
        model = fit(design.x[window], design.y[window], chosen.c)
        probabilities = model.predict_proba(design.x[test])[:, 1]
        rows = np.where(test)[0]
        got = dict(zip(rows.tolist(), probabilities.tolist()))
        for row in rows.tolist():
            if row % 2 or row + 1 not in got:
                continue
            on, a, b = keys[row // 2]
            out[(on, frozenset((a, b)))] = {
                "p_a": (got[row] + (1 - got[row + 1])) / 2,
                "a": a,
                "b": b,
                "y_a": int(design.y[row]),
                "on": on,
            }
    return out, chose


# -- scoring ------------------------------------------------------------------


def nats(y, p):
    p = np.clip(p, 1e-15, 1 - 1e-15)
    return -(y * np.log(p) + (1 - y) * np.log(1 - p))


def paired(a, b, events, draws=4000, seed=0):
    """Bootstrap a difference, resampling whole cards.

    Bouts on one card share conditions and a card can go eleven for twelve on
    favourites, so resampling bouts would narrow the interval by pretending
    they are independent.
    """
    rng = np.random.default_rng(seed)
    pools = defaultdict(list)
    for event, x, z in zip(events, a, b):
        pools[event].append(x - z)
    arrays = [np.array(v) for v in pools.values()]
    point = float(np.mean(np.concatenate(arrays)))
    out = np.empty(draws)
    for i in range(draws):
        pick = rng.integers(0, len(arrays), len(arrays))
        out[i] = np.mean(np.concatenate([arrays[j] for j in pick]))
    lo, hi = np.percentile(out, [2.5, 97.5])
    return point, float(lo), float(hi)


def mean_interval(per, events, draws=20000, seed=0):
    """The same clustered bootstrap, on one series rather than a difference."""
    rng = np.random.default_rng(seed)
    pools = defaultdict(list)
    for event, value in zip(events, per):
        pools[event].append(value)
    arrays = [np.array(v) for v in pools.values()]
    out = np.empty(draws)
    for i in range(draws):
        pick = rng.integers(0, len(arrays), len(arrays))
        out[i] = np.mean(np.concatenate([arrays[j] for j in pick]))
    lo, hi = np.percentile(out, [2.5, 97.5])
    return float(lo), float(hi)


def says(point, lo, hi, better, worse):
    if hi < 0:
        return f"{better} is better"
    if lo > 0:
        return f"{worse} is better"
    return "no difference established"


def payout(moneyline: int) -> float:
    """What a 100 unit bet wins. The same arithmetic as pickem's points_for."""
    return STAKE * moneyline / 100 if moneyline > 0 else STAKE * 100 / -moneyline


def line_up(archive: dict, predicted: dict, data_dir: Path) -> list[dict]:
    """Fights where the model, an opening line and a closing line all exist.

    The join is by date and the pair of normalised names, because the odds come
    from ESPN and the results from ufcstats and the two share no identifier. A
    couple of days of slack on the date: the archive carries the bout's own
    start in UTC and the dataset carries the card's local date.
    """
    from ufcbot.util import normalise  # noqa: F401  (documents the key's shape)

    careers = json.loads((data_dir / "espn_careers.json").read_text(encoding="utf-8"))
    by_id = {str(v["espn_id"]): k for k, v in careers.items() if v and v.get("espn_id")}
    closing, opening = market(archive, "close"), market(archive, "open")

    rows = []
    for key, bout in closing.items():
        early = opening.get(key)
        if early is None:
            continue
        names = {by_id[i]: p for i, p in bout["p"].items() if i in by_id}
        first = {by_id[i]: p for i, p in early["p"].items() if i in by_id}
        if len(names) != 2 or len(first) != 2:
            continue
        when = date.fromisoformat(bout["date"])
        pair = frozenset(names)
        got = next(
            (predicted[(when + timedelta(days=d), pair)]
             for d in (0, -1, 1, -2, 2) if (when + timedelta(days=d), pair) in predicted),
            None,
        )
        if got is None or got["a"] not in first:
            continue
        a, b = got["a"], got["b"]
        by_name = {by_id[i]: i for i in bout["p"] if i in by_id}
        rows.append({
            "on": got["on"],
            "y_a": float(got["y_a"]),
            "model": got["p_a"],
            "open": first[a],
            "close": names[a],
            "open_a": early["ml"][by_name[a]],
            "open_b": early["ml"][by_name[b]],
            "close_a": bout["ml"][by_name[a]],
            "close_b": bout["ml"][by_name[b]],
        })
    return rows


def main(start_year: int, block: int) -> int:
    load_dotenv()
    data_dir = Path(os.getenv("DATA_DIR", "data"))
    path = data_dir / ODDS_FILE
    if not path.exists():
        print(f"No {path}. Run `python odds.py` first.")
        return 1
    archive = json.loads(path.read_text(encoding="utf-8"))
    print(f"{len(archive)} priced bouts in {path}")

    print("\nBuilding a design matrix per candidate K:")
    designs, keys = designs_by_k(data_dir)
    start = date(start_year, 1, 1)

    print(f"\nWalking from {start}, {block} events a block:")
    started = time.time()
    predicted, chose = walk(designs, keys, start=start, block_events=block)
    print(f"  {len(predicted)} fights predicted out of sample   {time.time() - started:.0f}s")
    counts: dict[tuple[float, float], int] = defaultdict(int)
    for candidate in chose:
        counts[(candidate.k, candidate.c)] += 1
    print("  chose: " + ", ".join(
        f"K={k:g} C={c}: {n}" for (k, c), n in sorted(counts.items())
    ))

    rows = line_up(archive, predicted, data_dir)
    if len(rows) < 50:
        print(f"\nOnly {len(rows)} fights line up, which is not enough to report on.")
        return 1

    y = np.array([r["y_a"] for r in rows])
    events = [r["on"] for r in rows]
    series = {k: np.array([r[k] for r in rows]) for k in ("model", "open", "close")}
    print(f"\n{len(rows)} fights, {len(set(events))} cards, {min(events)} to {max(events)}")

    print("\n" + "=" * 70)
    print("WHOSE PROBABILITIES ARE BETTER".center(70))
    print("=" * 70)
    print(f"{'':<18}{'log loss':>12}{'accuracy':>12}{'mean |p-.5|':>14}")
    for label in ("model", "open", "close"):
        p = series[label]
        print(f"{label:<18}{nats(y, p).mean():>12.5f}"
              f"{np.mean((p >= 0.5) == (y == 1)):>12.1%}{np.mean(np.abs(p - 0.5)):>14.4f}")
    for a, b in (("model", "open"), ("model", "close"), ("open", "close")):
        point, lo, hi = paired(nats(y, series[a]), nats(y, series[b]), events)
        print(f"\n  {a} vs {b:<10}{point:+.5f} nats  95% [{lo:+.5f}, {hi:+.5f}]"
              f"  -> {says(point, lo, hi, a, b)}")

    print("\n" + "=" * 70)
    print("DOES THE MODEL ADD ANYTHING THE PRICE DOES NOT HAVE?".center(70))
    print("=" * 70)

    def logit(p):
        p = np.clip(p, 1e-6, 1 - 1e-6)
        return np.log(p / (1 - p))

    alone = nats(y, series["close"])
    for weight in (0.1, 0.2, 0.3, 0.5):
        blended = 1 / (1 + np.exp(-((1 - weight) * logit(series["close"])
                                    + weight * logit(series["model"]))))
        point, lo, hi = paired(nats(y, blended), alone, events)
        print(f"  {f'{weight:.0%} model, {1 - weight:.0%} closing line':<34}"
              f"{point:+.5f} nats  95% [{lo:+.5f}, {hi:+.5f}]  "
              f"-> {says(point, lo, hi, 'the blend', 'the line alone')}")

    print("\n" + "=" * 70)
    print("MONEY, AT 100 A PICK".center(70))
    print("=" * 70)
    print("  Pick'em pays the price a pick was made at, and its board opens days")
    print("  before a card is sharply priced, so the opening rows are the game.\n")

    def profit(pick_a, which):
        out = np.empty(len(rows))
        for i, on_a in enumerate(pick_a):
            line = rows[i][f"{which}_a"] if on_a else rows[i][f"{which}_b"]
            right = (y[i] == 1) if on_a else (y[i] == 0)
            out[i] = payout(line) if right else -STAKE
        return out

    for which in ("open", "close"):
        print(f"  --- at the {which} price ---")
        for label, pick in (("the model's pick", series["model"] >= 0.5),
                            ("the favourite", series[which] >= 0.5)):
            per = profit(pick, which)
            lo, hi = mean_interval(per, events)
            verdict = ("profitable" if lo > 0 else
                       "losing" if hi < 0 else "cannot tell from zero")
            print(f"    {label:<20}{per.sum():>+8.0f} units  {per.mean() / STAKE:>+7.2%} ROI  "
                  f"95% [{lo / STAKE:+.2%}, {hi / STAKE:+.2%}]  {verdict}")
        print()

    # The expected-value rule this script mainly exists to rule out.
    print("  --- staking wherever the model disagrees with the closing price ---")
    raw_a = np.array([_implied(r["close_a"]) for r in rows])
    raw_b = np.array([_implied(r["close_b"]) for r in rows])
    edge_a, edge_b = series["model"] - raw_a, (1 - series["model"]) - raw_b
    on_a = edge_a >= edge_b
    best = np.where(on_a, edge_a, edge_b)
    staked = profit(on_a, "close")
    hit = np.where(on_a, y == 1, y == 0)
    for threshold in (0.0, 0.05, 0.10):
        mask = best > threshold
        if mask.sum() < 30:
            continue
        print(f"    {f'edge over {threshold:.0%}':<20}{int(mask.sum()):>5} bets  "
              f"{np.mean(hit[mask]):>6.1%} right  "
              f"{staked[mask].mean() / STAKE:>+7.2%} ROI")

    print("\n" + "=" * 70)
    drift = series["close"] - series["open"]
    moved = np.abs(drift) > 0.03
    if moved.sum() > 30:
        toward = (np.sign(drift) == np.sign(series["model"] - series["open"]))[moved]
        lo, hi = mean_interval(toward.astype(float), [e for e, m in zip(events, moved) if m])
        print(f"  The line moved more than 3 points on {int(moved.sum())} of {len(rows)} fights,")
        print(f"  and the model leaned the way it went on {np.mean(toward):.1%} of those,")
        print(f"  95% [{lo:.1%}, {hi:.1%}]. Above half is what it holds that the opening")
        print("  price does not, and it is the one thing here the model wins.")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--from", dest="start_year", type=int, default=2024,
                        help="first year of events to score (default 2024)")
    parser.add_argument("--block", type=int, default=10,
                        help="events predicted per step (default 10)")
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    raise SystemExit(main(args.start_year, args.block))
