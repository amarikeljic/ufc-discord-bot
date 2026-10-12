"""Fetch the sportsbook archive from ESPN into the data directory.

Run with:  python odds.py            (fetch whatever is missing)
           python odds.py --from 2024

The bot prices a live card from Polymarket, which has no usable history once a
market resolves. ESPN keeps one: an opening and a closing moneyline per corner,
for cards that have already happened. That is what makes it possible to ask
whether the model knows anything the market does not -- see ``benchmark.py``,
which is the only thing that reads this file.

Coverage is a matter of who ESPN was in business with, so it is uneven and the
gaps are not recoverable. ESPN BET priced 2024 and 2025 at close to every bout;
it shut down, and there is nothing from January to August 2026; DraftKings
appears from September 2026. Contender Series is never priced by anybody.

Stored as the raw payload per bout rather than as parsed numbers. The first
pass at this parsed one level too shallow, read every line as missing, and the
only way to find out was to crawl the whole thing again.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path

from dotenv import load_dotenv

ODDS_FILE = "odds.json"

# Books that price a fight before it starts. "ESPN Bet - Live Odds" prices one
# in progress, which is not an estimate of who wins it: it reads -2000 on a man
# who is three minutes from losing.
PRE_FIGHT = ("espn bet", "draftkings")

log = logging.getLogger("odds")


def _athlete(side: dict | None) -> str | None:
    """The ESPN athlete id a side of the market refers to."""
    ref = ((side or {}).get("athlete") or {}).get("$ref") or ""
    return ref.rstrip("/").split("/")[-1].split("?")[0] if ref else None


async def _month(http, data, year: int, month: int, archive: dict) -> int:
    from ufcbot.sources.espn import LEAGUE

    start = datetime(year, month, 1, tzinfo=UTC)
    end = datetime(year + (month == 12), month % 12 + 1, 1, tzinfo=UTC)
    if start > datetime.now(UTC):
        return 0
    try:
        refs = await data._event_refs(start, end)
    except Exception as exc:  # noqa: BLE001 - one bad month must not stop the crawl
        log.warning("Could not list %d-%02d: %r", year, month, exc)
        return 0

    added = 0
    for ref in refs:
        try:
            payload = await http.get_json(ref)
        except Exception as exc:  # noqa: BLE001
            log.debug("event failed: %r", exc)
            continue
        for comp in payload.get("competitions") or []:
            # No odds object at all is how an unpriced card reads, and asking
            # the endpoint anyway is a request per bout for nothing.
            if "odds" not in comp:
                continue
            key = f"{payload['id']}:{comp['id']}"
            if key in archive:
                continue
            url = f"{LEAGUE}/events/{payload['id']}/competitions/{comp['id']}/odds"
            try:
                got = await http.get_json(url)
            except Exception as exc:  # noqa: BLE001
                log.debug("odds failed for %s: %r", key, exc)
                continue
            books = [
                {
                    "provider": name,
                    "home": item.get("homeAthleteOdds"),
                    "away": item.get("awayAthleteOdds"),
                }
                for item in got.get("items") or []
                if (name := ((item.get("provider") or {}).get("name") or "").strip().lower())
                in PRE_FIGHT
            ]
            if not books:
                continue
            archive[key] = {
                "event": payload.get("name"),
                "date": (comp.get("date") or payload.get("date") or "")[:10],
                "books": books,
            }
            added += 1
    return added


async def main(first_year: int) -> int:
    from ufcbot.sources.espn import UFCData
    from ufcbot.sources.http import HttpClient

    load_dotenv()
    data_dir = Path(os.getenv("DATA_DIR", "data"))
    path = data_dir / ODDS_FILE

    archive: dict = {}
    if path.exists():
        try:
            archive = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            log.warning("%s was unreadable; starting again.", ODDS_FILE)
    print(f"{len(archive)} bouts already priced")

    http = HttpClient()
    await http.start()
    data = UFCData(http)
    try:
        for year in range(first_year, datetime.now(UTC).year + 1):
            for month in range(1, 13):
                added = await _month(http, data, year, month, archive)
                if added:
                    data_dir.mkdir(parents=True, exist_ok=True)
                    tmp = path.with_suffix(path.suffix + ".part")
                    tmp.write_text(json.dumps(archive), encoding="utf-8")
                    tmp.replace(path)
                print(f"  {year}-{month:02d}  +{added:<4} total {len(archive)}", flush=True)
    finally:
        await http.close()

    print(f"\n{len(archive)} bouts written to {path}")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--from", dest="first_year", type=int, default=2024,
                        help="first year to ask about (default 2024, before which ESPN has none)")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    raise SystemExit(asyncio.run(main(args.first_year)))
