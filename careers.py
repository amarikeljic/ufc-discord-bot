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

Three sources, because the first two cannot see a debutant. Everyone with a UFC
record, then the regional opponents who keep coming up, then everyone booked on
a card that has not happened yet -- that last one by id straight off the card,
since a crawl built out of past fights only learns a new signing exists once his
debut is over, which is when a rating for him stops being any use. Run it in the
week before a card and the debuts on it have ratings.

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
# How far ahead to read announced cards, and how many of them. The UFC
# announces a bout a few weeks out and books three months ahead, so this
# reaches past everything booked without asking for pages that do not exist.
UPCOMING_DAYS = 120
UPCOMING_CARDS = 12
AT_ONCE = 4
CHUNK = 50

log = logging.getLogger("careers")


async def _search(data, name: str, key: str) -> str | None:
    """The ESPN id for a name, or None when nothing comes back matching it.

    Exact on the normalised name on purpose. A near miss here does not give a
    worse career, it gives somebody else's."""
    from ufcbot.util import normalise

    try:
        matches = await data.search_fighters(name, limit=3)
    except Exception as exc:  # noqa: BLE001 - one bad name must not stop the crawl
        log.debug("search failed for %r: %r", name, exc)
        return None
    for stub in matches:
        if normalise(stub.display_name) == key:
            return stub.id
    return None


async def _one(data, name: str, espn_id: str | None, sem: asyncio.Semaphore):
    from ufcbot.util import normalise

    key = normalise(name)
    async with sem:
        if espn_id is None:
            espn_id = await _search(data, name, key)
            if espn_id is None:
                return key, None
        try:
            history = await data.fighter_history(espn_id)
        except Exception as exc:  # noqa: BLE001
            log.debug("history failed for %r: %r", name, exc)
            return key, None
        return key, {
            "espn_id": espn_id,
            "name": name,
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


async def _announced(data) -> dict[str, tuple[str, str]]:
    """Everyone booked on a card that has not happened yet, with their ESPN id.

    The rest of this crawl is built out of fights that already happened, so a
    fighter signed from outside the UFC is missing from it until their debut is
    over -- which is exactly when a rating for them stops being any use. These
    are the people the professional graph was built for.

    By id straight off the card rather than by name: a fighter nobody has
    published before is also the kind of name a search does not match, and the
    card has already told us who he is."""
    from ufcbot.stats.graph import is_placeholder
    from ufcbot.util import normalise

    found: dict[str, tuple[str, str]] = {}
    for summary in await data.upcoming_events(days=UPCOMING_DAYS, limit=UPCOMING_CARDS):
        event = await data.get_event(summary.id)
        if event is None:
            continue
        for bout in event.bouts:
            for fighter in bout.fighters:
                if not fighter.display_name or not fighter.id:
                    continue
                key = normalise(fighter.display_name)
                if is_placeholder(key):
                    # A corner still to be filled. ESPN gives it an athlete id
                    # and a history like anybody else, and fetching one is how a
                    # card's placeholder becomes a fighter with a rating.
                    continue
                found.setdefault(key, (fighter.display_name, fighter.id))
    return found


# The most fighters the nightly top-up will ask about in one pass. A card
# announcement brings a handful; this is here so that a bad schedule read or
# an empty cache cannot turn the bot's housekeeping into the full crawl.
BOOKED_AT_MOST = 40


async def top_up_booked(data, data_dir, *, at_most: int = BOOKED_AT_MOST) -> int:
    """Fetch careers for fighters booked on a card who have none cached.

    The part of this crawl that cannot wait. Everything else here is built
    out of fights that already happened and is as true next month as tonight,
    but a fighter signed from outside the UFC is missing until his debut is
    over, which is when a rating for him stops being any use.

    Bounded and incremental, so it belongs on a timer where the full crawl
    does not: a warm cache makes this a handful of requests a week.

    Returns how many careers were added. A non-zero answer changes the
    dataset fingerprint, so the next refresh rebuilds and the newcomers
    reach the bot before the card rather than after it.
    """
    from ufcbot.stats.graph import CAREERS_FILE

    path = Path(data_dir) / CAREERS_FILE
    cached: dict = {}
    if path.exists():
        try:
            cached = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            log.warning("%s was unreadable; leaving the nightly top-up alone.", CAREERS_FILE)
            return 0

    booked = await _announced(data)
    wanted = [pair for key, pair in booked.items() if not cached.get(key)][:at_most]
    if not wanted:
        return 0

    sem = asyncio.Semaphore(AT_ONCE)
    added = 0
    for key, got in await asyncio.gather(
        *(_one(data, name, espn_id, sem) for name, espn_id in wanted)
    ):
        cached[key] = got
        added += bool(got)

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".part")
    tmp.write_text(json.dumps(cached), encoding="utf-8")
    # Replaced rather than written in place: the refresh reads this file and
    # must never see half of it.
    tmp.replace(path)
    log.info("Nightly top-up: asked about %d booked fighters, found %d", len(wanted), added)
    return added


async def main(refresh: bool) -> int:
    from ufcbot.sources.espn import UFCData
    from ufcbot.sources.http import HttpClient
    from ufcbot.stats import dataset as ds
    from ufcbot.stats.career import build_history
    from ufcbot.stats.graph import CAREERS_FILE
    from ufcbot.util import normalise

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
    # Name -> (name, ESPN id or None). Keyed so the people who turn up from more
    # than one direction are asked for once.
    todo: dict[str, tuple[str, str | None]] = {
        normalise(name): (name, None)
        for name in _wanted(cached, history, refresh=refresh)
    }

    http = HttpClient()
    await http.start()
    data = UFCData(http)
    sem = asyncio.Semaphore(AT_ONCE)
    try:
        try:
            announced = await _announced(data)
        except Exception as exc:  # noqa: BLE001 - a bad schedule read is not fatal
            log.warning("Could not read the upcoming cards: %r", exc)
            announced = {}
        booked = 0
        for key, pair in announced.items():
            if cached.get(key):
                continue
            if key not in todo:
                booked += 1
            # The id wins even where a name search was already queued: it cannot
            # fetch the wrong man, and it retries whoever the search failed on.
            todo[key] = pair

        jobs = list(todo.values())
        print(f"{len(cached)} cached, {len(jobs)} to fetch ({booked} booked but unseen)")
        if not jobs:
            return 0
        for start in range(0, len(jobs), CHUNK):
            for key, got in await asyncio.gather(
                *(_one(data, name, espn_id, sem) for name, espn_id in jobs[start : start + CHUNK])
            ):
                cached[key] = got
            tmp = path.with_suffix(path.suffix + ".part")
            tmp.write_text(json.dumps(cached), encoding="utf-8")
            tmp.replace(path)
            print(f"  {min(start + CHUNK, len(jobs))}/{len(jobs)}", flush=True)
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
