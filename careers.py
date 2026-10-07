"""Fetch professional fight careers from ESPN into the data directory.

Run with:  python careers.py            (fetch whoever is missing)
           python careers.py --refresh  (also re-fetch anyone who fought lately)

ufcstats publishes UFC fights. ESPN publishes whole careers, and the model
trains better for having them -- see ``ufcbot.stats.graph`` for what it is worth
and what it costs. This is a separate job rather than part of the refresh
because it is thousands of requests, and the refresh runs on a timer.

Incremental: the cache is keyed by fighter and only the missing ones are asked
for. A first run over an empty cache is a few thousand requests and the better
part of an hour; afterwards it is a handful per card.

The bot runs perfectly well without the file. Its absence costs the two graph
features, which go constant and carry nothing.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
from datetime import date, timedelta
from pathlib import Path

from dotenv import load_dotenv

# Opponents faced this often are worth a career of their own: it is the regional
# circuit a prospect came through, and it is what lets the graph tell a record
# built on contenders from one built on journeymen. Below it the long tail is
# 14,000 fighters seen once each.
OPPONENT_SEEN = 3
# Anyone who fought this recently is re-fetched with --refresh, since their
# career has grown since it was cached.
RECENT = timedelta(days=120)
AT_ONCE = 4
CHUNK = 50

log = logging.getLogger("careers")


async def _one(data, name: str, sem: asyncio.Semaphore):
    from ufcbot.util import normalise

    key = normalise(name)
    async with sem:
        try:
            matches = await data.search_fighters(name, limit=3)
        except Exception as exc:  # noqa: BLE001 - one bad name must not stop the crawl
            log.debug("search failed for %r: %r", name, exc)
            return key, None
        for stub in matches:
            if normalise(stub.display_name) != key:
                continue
            try:
                history = await data.fighter_history(stub.id)
            except Exception as exc:  # noqa: BLE001
                log.debug("history failed for %r: %r", name, exc)
                return key, None
            return key, {
                "espn_id": stub.id,
                "name": stub.display_name,
                "fights": [
                    {
                        "on": e.on.date().isoformat() if e.on else None,
                        "result": e.result,
                        "opponent": e.opponent,
                        "event": e.event,
                    }
                    for e in history if e.on
                ],
            }
    return key, None


def _wanted(cached: dict, history, *, refresh: bool) -> list[str]:
    """Fighters to ask ESPN about: everyone unseen, plus the lately active."""
    from collections import Counter

    from ufcbot.util import normalise

    names: dict[str, str] = {}
    for key, led in history.ledgers.items():
        names[key] = led.name

    todo = [n for k, n in names.items() if k not in cached]
    if refresh:
        recent_from = date.today() - RECENT
        todo += [
            n for k, n in names.items()
            if k in cached and history.ledgers[k].last_fight
            and history.ledgers[k].last_fight >= recent_from
        ]

    # And the regional opponents who keep coming up, so a win out there can be
    # weighed rather than counted.
    seen: Counter[str] = Counter()
    opponents: dict[str, str] = {}
    for got in cached.values():
        if not got:
            continue
        for fight in got.get("fights", ()):
            if fight.get("opponent"):
                key = normalise(fight["opponent"])
                seen[key] += 1
                opponents.setdefault(key, fight["opponent"])
    todo += [opponents[k] for k, n in seen.items() if n >= OPPONENT_SEEN and k not in cached]
    return list(dict.fromkeys(todo))


async def main(refresh: bool) -> int:
    from ufcbot.sources.espn import UFCData
    from ufcbot.sources.http import HttpClient
    from ufcbot.stats import dataset as ds
    from ufcbot.stats.career import build_history
    from ufcbot.stats.graph import CAREERS_FILE

    load_dotenv()
    data_dir = Path(os.getenv("DATA_DIR", "data"))
    path = data_dir / CAREERS_FILE

    cached: dict = {}
    if path.exists():
        try:
            cached = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            log.warning("%s was unreadable; starting again.", CAREERS_FILE)

    history = build_history(ds.load(data_dir), keep_snapshots=False)
    todo = _wanted(cached, history, refresh=refresh)
    print(f"{len(cached)} cached, {len(todo)} to fetch")
    if not todo:
        return 0

    http = HttpClient()
    await http.start()
    data = UFCData(http)
    sem = asyncio.Semaphore(AT_ONCE)
    try:
        for start in range(0, len(todo), CHUNK):
            for key, got in await asyncio.gather(
                *(_one(data, n, sem) for n in todo[start : start + CHUNK])
            ):
                cached[key] = got
            tmp = path.with_suffix(path.suffix + ".part")
            tmp.write_text(json.dumps(cached), encoding="utf-8")
            tmp.replace(path)
            print(f"  {min(start + CHUNK, len(todo))}/{len(todo)}", flush=True)
    finally:
        await http.close()

    found = sum(1 for v in cached.values() if v)
    fights = sum(len(v["fights"]) for v in cached.values() if v)
    print(f"\n{found} careers, {fights} fights, written to {path}")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--refresh", action="store_true",
                        help="also re-fetch fighters who have fought recently")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    raise SystemExit(asyncio.run(main(args.refresh)))
