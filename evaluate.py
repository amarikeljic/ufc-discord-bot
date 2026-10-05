"""Score the winner model by rolling origin, and choose its Elo K while doing it.

Run with:  python evaluate.py
           python evaluate.py --from 2016 --block 10

A single held-out tail could not settle the questions being asked of it: 117
events gave a 95% interval of about +/-0.005 nats, and the candidates being
chosen between differ by a few thousandths. This walks the origin forward
instead, scoring every event once with a model that never saw it, so the
bootstrap has four times the events to work with.

Nothing here runs in the bot. It reads the same dataset and builds the same
design matrix, then reports; see ``ufcbot.stats.rolling`` for the protocol and
the two rules that keep it honest.
"""

from __future__ import annotations

import argparse
import logging
import os
import time
from datetime import date
from pathlib import Path

import numpy as np
from dotenv import load_dotenv

# Elo learning rates to choose between. Predictions depend only on K/D, so with
# the divisor fixed at 400 this one grid covers the whole ridge. 32 is what the
# boards use and what the model has always used.
K_VALUES = (32.0, 64.0, 128.0)

# How hard to hold the logistic half back. Coarse and on a log scale: a fine
# grid searched on a validation slice buys nothing but more chances to pick
# noise.
C_VALUES = (0.005, 0.02, 0.08)


def designs_by_k(data_dir: Path, ks=K_VALUES):
    """One design matrix per candidate K, built from the same fights.

    A fighter's rating at a fight depends only on earlier fights, so a full pass
    at each K is leakage-free under any later split by date. That is what makes
    K an ordinary thing to select over rather than a rebuild per step.
    """
    from ufcbot.stats import career as career_mod
    from ufcbot.stats import dataset as ds
    from ufcbot.stats.career import build_history
    from ufcbot.stats.model import _design_matrix
    from ufcbot.stats.rolling import Design

    dataset = ds.load(data_dir)
    out = {}
    for k in ks:
        started = time.time()
        career_mod.ELO_K = k
        history = build_history(dataset, keep_snapshots=True)
        x, y, dates, _ = _design_matrix(history, dataset.fighters)
        out[k] = Design(x=x, y=y, dates=dates)
        print(f"  K={k:<6g} {x.shape[0]:>6} rows x {x.shape[1]} features   {time.time() - started:.0f}s")
    career_mod.ELO_K = 32.0
    return out


def main(start_year: int, block: int) -> int:
    from ufcbot.stats.model import _linear
    from ufcbot.stats.rolling import Candidate, compare, walk

    load_dotenv()
    data_dir = Path(os.getenv("DATA_DIR", "data"))

    def fit(x, y, c):
        return _linear(c).fit(x, y)

    print("Building a design matrix per candidate K:")
    designs = designs_by_k(data_dir)
    start = date(start_year, 1, 1)

    every = [Candidate(k, c) for k in K_VALUES for c in C_VALUES]
    fixed = [Candidate(32.0, c) for c in C_VALUES]

    print(f"\nWalking from {start}, {block} events a block, {len(every)} candidates:")
    started = time.time()
    selected = walk(designs, start=start, candidates=every, block_events=block, fit=fit)
    print(f"  selecting K too   {time.time() - started:.0f}s")
    started = time.time()
    held = walk(designs, start=start, candidates=fixed, block_events=block, fit=fit)
    print(f"  K held at 32      {time.time() - started:.0f}s")

    print(f"\n{len(np.unique(selected.events))} events, {selected.fights} fights, "
          f"{len(selected.steps)} steps, every event scored exactly once")
    print(f"\n  K selected per window   log loss {selected.log_loss():.5f}   "
          f"accuracy {selected.accuracy():.4f}")
    print(f"  K held at 32            log loss {held.log_loss():.5f}   "
          f"accuracy {held.accuracy():.4f}")

    point, lo, hi, share = compare(selected, held)
    print(f"\n  paired by event: {point:+.5f} nats   95% [{lo:+.5f}, {hi:+.5f}]")
    print(f"  draws favouring a selected K: {share:.1%}")
    print(f"  {'conclusive at 95%' if (lo < 0 and hi < 0) or (lo > 0 and hi > 0) else 'straddles zero'}")

    print("\nWhat the windows chose, most often first:")
    for name, count in selected.chosen_counts().items():
        print(f"  {name:<20} {count:>3} of {len(selected.steps)}")

    chose_k = {}
    for step in selected.steps:
        chose_k[step.chosen.k] = chose_k.get(step.chosen.k, 0) + 1
    print("\n  by K alone:")
    for k in sorted(chose_k):
        print(f"    K={k:<6g} {chose_k[k]:>3} of {len(selected.steps)}")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--from", dest="start_year", type=int, default=2016,
                        help="first year of events to score (default 2016)")
    parser.add_argument("--block", type=int, default=10,
                        help="events predicted per step (default 10)")
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    raise SystemExit(main(args.start_year, args.block))
