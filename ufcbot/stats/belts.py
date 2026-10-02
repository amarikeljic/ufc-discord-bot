"""Belt changes the fight data cannot record.

ufcstats says who won a title fight. It never says a champion vacated, was
stripped, retired, or was elevated from interim, because none of those happen in
a cage. The derived rule -- whoever won the division's most recent title fight
holds it -- is right until one of those happens, and then it is wrong until the
next title fight, which can be a year.

Two cases from the last fifteen months, both at heavyweight. Aspinall and Gane
was a no contest, so the belt did not move; Aspinall then vacated and Gane was
elevated, and neither is a fight. On the derived rule alone the heavyweight belt
sat with Jon Jones from November 2024, two years after he last held it.

So this file carries the exceptions, and nothing else. It is small on purpose:
every entry here is a claim no data supports, and the only thing keeping it true
is somebody remembering to change it.

Format -- a list of objects in ``belts.json`` in the data directory::

    [
      {
        "division": "Women's Flyweight",
        "fighter": "Valentina Shevchenko",
        "status": "vacated",
        "on": "2026-09-30",
        "source": "https://www.ufc.com/news/..."
      }
    ]

``status`` is one of ``undisputed`` (they hold it), ``interim`` (they hold the
interim belt), or ``vacated`` (nobody holds it, and ``fighter`` names whoever
gave it up). ``source`` is where it was read, so a later reader can check it
rather than trust it.

``on`` is **the date of the administrative change** -- the announcement that the
belt was vacated, stripped or handed over -- and never the date of the fight
that led to it. Dating an entry to the fight makes it no later than that fight,
and a later fight supersedes an entry, so it would be ignored the moment it was
written.

An entry is superseded by any title fight in the data dated strictly after it,
and survives one on the same day: a belt announced vacant on the morning of a
card is vacant for that card, and the card's own result writes the next champion
anyway. That supersession is the guard against this file going stale. A
forgotten entry stops mattering the moment somebody fights for the belt, rather
than pinning a champion forever and recreating the problem it was written to fix
with a person as the source.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date
from pathlib import Path

log = logging.getLogger(__name__)

BELTS_FILE = "belts.json"

UNDISPUTED = "undisputed"
INTERIM = "interim"
VACATED = "vacated"
STATUSES = (UNDISPUTED, INTERIM, VACATED)


@dataclass(frozen=True, slots=True)
class BeltChange:
    """One thing that happened to a belt outside a cage."""

    division: str
    fighter: str
    status: str
    on: date
    source: str = ""

    @property
    def holder(self) -> str | None:
        """Who holds the belt after this, or None where it was given up."""
        return None if self.status == VACATED else self.fighter


def load(data_dir: Path) -> list[BeltChange]:
    """Read the overrides, or an empty list where there are none.

    A malformed file is logged and ignored rather than raised. It is a hand-kept
    file of exceptions; a typo in it should cost the belt markers, not the bot.
    """
    path = Path(data_dir) / BELTS_FILE
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log.warning("Could not read %s (%r); the derived champions stand", path.name, exc)
        return []

    changes = []
    for entry in raw if isinstance(raw, list) else []:
        try:
            status = str(entry["status"]).lower()
            if status not in STATUSES:
                raise ValueError(f"status must be one of {STATUSES}, not {status!r}")
            changes.append(
                BeltChange(
                    division=str(entry["division"]),
                    fighter=str(entry["fighter"]),
                    status=status,
                    on=date.fromisoformat(str(entry["on"])),
                    source=str(entry.get("source", "")),
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            log.warning("Skipping a belt entry in %s (%r)", path.name, exc)
    return changes


def apply(
    derived: dict[str, str],
    last_title_fight: dict[str, date],
    changes: list[BeltChange],
    *,
    resolve,
) -> tuple[dict[str, str], list[str]]:
    """The champions, with the overrides laid over them.

    ``derived`` is division -> fighter key from the fight data, and
    ``last_title_fight`` is when that division last had one. ``resolve`` turns a
    written name into a dataset key, so the file can be written in English.

    Returns the champions and a list of complaints: entries that name a fighter
    nobody can find, and entries a later title fight has overtaken. The second is
    not an error -- it is the file being allowed to go stale safely -- but it is
    worth saying out loud, because an entry nobody has to delete is an entry
    nobody notices is wrong.
    """
    champions = dict(derived)
    warnings: list[str] = []

    for change in sorted(changes, key=lambda c: c.on):
        fought_since = last_title_fight.get(change.division)
        if fought_since is not None and fought_since > change.on:
            warnings.append(
                f"{change.division}: the {change.status} entry for {change.fighter} "
                f"({change.on}) is older than the title fight on {fought_since}, so the "
                "data decides it now and the entry can go"
            )
            continue
        if change.holder is None:
            champions.pop(change.division, None)
            continue
        key = resolve(change.fighter)
        if key is None:
            warnings.append(
                f"{change.division}: no fighter found for {change.fighter!r}, so the belt "
                "is left where the data put it"
            )
            continue
        champions[change.division] = key

    return champions, warnings
