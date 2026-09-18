"""Download and parse the ufcstats.com dataset.

The CSVs come from https://github.com/Greco1899/scrape_ufc_stats, which re-scrapes
ufcstats.com after each card. Everything the site shows on a fighter page can be
recomputed from these files, which is how the exact ufcstats-style numbers are
produced without scraping the site directly.

This module is part of the refresh job, which runs in its own process, and is
the only place pandas is used. Nothing the bot imports reaches it.
"""

from __future__ import annotations

import json
import logging
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import pandas as pd

from ..util import normalise
from .career import FighterInfo
from .techniques import method_detail, technique_from_ufcstats

log = logging.getLogger(__name__)

RAW_BASE = "https://raw.githubusercontent.com/Greco1899/scrape_ufc_stats/main/"
FILES = (
    "ufc_event_details.csv",
    "ufc_fight_results.csv",
    "ufc_fight_stats.csv",
    "ufc_fighter_tott.csv",
)
ETAG_FILE = "etags.json"
USER_AGENT = "ufc-discord-bot/1.0 (+https://github.com/Greco1899/scrape_ufc_stats dataset consumer)"

# "3 Rnd (5-5-5)", "1 Rnd + OT (12-3)", "1 Rnd + 2OT (15-3-3)" -> the bracketed minutes.
_ROUND_LENGTHS = re.compile(r"\(([\d\-]+)\)")
_OF = re.compile(r"^\s*(\d+)\s+of\s+(\d+)\s*$")
_BOUT_SPLIT = re.compile(r"\s+vs\.?\s+")


# -- download ---------------------------------------------------------------


def download(directory: Path, *, staging: Path | None = None) -> bool:
    """Fetch any CSV that changed upstream. Returns True when something new arrived.

    Files land in ``staging`` when given, so the caller can validate them before
    they replace the live copies. Requests carry the ETag of the current file, so
    an unchanged file costs one small round trip instead of a download.

    Blocking; call from a thread when inside the event loop.
    """
    target = staging or directory
    target.mkdir(parents=True, exist_ok=True)
    etag_path = directory / ETAG_FILE
    try:
        etags: dict[str, str] = json.loads(etag_path.read_text())
    except (OSError, ValueError):
        etags = {}

    changed = False
    for name in FILES:
        headers = {"User-Agent": USER_AGENT}
        live = directory / name
        if live.exists() and name in etags:
            headers["If-None-Match"] = etags[name]

        request = urllib.request.Request(RAW_BASE + name, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                payload = response.read()
                etag = response.headers.get("ETag")
        except urllib.error.HTTPError as exc:
            if exc.code == 304:
                if staging is not None:
                    # Unchanged upstream, so the staged set reuses the live file.
                    (target / name).write_bytes(live.read_bytes())
                continue
            raise

        # Write to a temp file first so a failed download never leaves a torn CSV.
        tmp = target / (name + ".part")
        tmp.write_bytes(payload)
        tmp.replace(target / name)
        if etag:
            etags[name] = etag
        changed = True
        log.info("Downloaded %s (%d bytes)", name, len(payload))

    if changed:
        etag_path.write_text(json.dumps(etags))
    return changed


def promote(staging: Path, directory: Path) -> None:
    """Move validated staged files over the live ones."""
    for name in FILES:
        source = staging / name
        if source.exists():
            source.replace(directory / name)


# -- parsing helpers --------------------------------------------------------


def _landed_attempted(value) -> tuple[float, float]:
    match = _OF.match(str(value)) if pd.notna(value) else None
    if not match:
        return float("nan"), float("nan")
    return float(match.group(1)), float(match.group(2))


def _mmss_to_seconds(value) -> float:
    if pd.isna(value):
        return float("nan")
    text = str(value).strip()
    if ":" not in text:
        return float("nan")
    minutes, seconds = text.split(":", 1)
    try:
        return int(minutes) * 60 + int(seconds)
    except ValueError:
        return float("nan")


def _round_lengths_minutes(time_format: str) -> list[float]:
    text = (time_format or "").strip()
    if text.startswith("No Time Limit"):
        return [float("inf")]
    match = _ROUND_LENGTHS.search(text)
    if not match:
        return [5.0, 5.0, 5.0]
    return [float(part) for part in match.group(1).split("-") if part]


def _total_fight_seconds(time_format: str, end_round: int, end_time_seconds: float) -> float:
    lengths = _round_lengths_minutes(time_format)
    if pd.isna(end_time_seconds) or end_round < 1:
        return float("nan")
    completed = lengths[: max(0, end_round - 1)]
    if any(length == float("inf") for length in completed):
        return float("nan")
    return sum(completed) * 60 + end_time_seconds


def _method_class(method: str) -> str:
    text = (method or "").lower()
    if "decision" in text:
        return "dec"
    if "submission" in text:
        return "sub"
    if "ko" in text or "tko" in text:
        return "ko"
    return "other"


def _inches(value) -> float:
    """``5' 11"`` -> 71 ; ``66"`` -> 66 ; ``--`` -> NaN."""
    if pd.isna(value):
        return float("nan")
    text = str(value).strip()
    feet = re.match(r"^(\d+)'\s*(\d+)?\"?$", text)
    if feet:
        return int(feet.group(1)) * 12 + int(feet.group(2) or 0)
    plain = re.match(r"^(\d+)\"$", text)
    if plain:
        return float(plain.group(1))
    return float("nan")


def _pounds(value) -> float:
    if pd.isna(value):
        return float("nan")
    match = re.match(r"^(\d+)", str(value).strip())
    return float(match.group(1)) if match else float("nan")


# -- the dataset ------------------------------------------------------------


@dataclass(slots=True)
class Dataset:
    fights: pd.DataFrame
    """One row per fight, oldest first. Columns include fight_id, date, fighter_a,
    fighter_b, winner ('a', 'b' or None), method_class, total_seconds, title_fight."""

    fight_stats: pd.DataFrame
    """One row per (fight_id, side) with that fighter's totals across all rounds."""

    fighters: dict[str, FighterInfo]
    """Tale of the tape keyed by normalised name."""

    newest_event: date | None

    @property
    def fight_count(self) -> int:
        return len(self.fights)


def load(directory: Path) -> Dataset:
    """Parse the CSVs into tidy frames. Blocking, a couple of seconds."""
    events = pd.read_csv(directory / "ufc_event_details.csv")
    results = pd.read_csv(directory / "ufc_fight_results.csv")
    stats = pd.read_csv(directory / "ufc_fight_stats.csv")
    tott = pd.read_csv(directory / "ufc_fighter_tott.csv")

    fights = _build_fights(events, results)
    fight_stats = _build_fight_stats(fights, stats)
    fighters = _build_fighters(tott)

    newest = fights["date"].max()
    return Dataset(
        fights=fights,
        fight_stats=fight_stats,
        fighters=fighters,
        newest_event=newest.date() if pd.notna(newest) else None,
    )


def _build_fights(events: pd.DataFrame, results: pd.DataFrame) -> pd.DataFrame:
    events = events.assign(
        event=events["EVENT"].str.strip(),
        date=pd.to_datetime(events["DATE"], format="%B %d, %Y", errors="coerce"),
    )[["event", "date"]].drop_duplicates("event")

    frame = results.assign(
        event=results["EVENT"].str.strip(),
        bout=results["BOUT"].str.strip(),
    ).drop_duplicates(["event", "bout"])

    frame = frame.merge(events, on="event", how="left")

    # The results file lists cards newest first. A card missing from the event
    # table takes the date of the card listed just above it, which is at most a
    # week off and keeps the fight in the right place in every fighter's history.
    frame["date"] = frame["date"].bfill().ffill()

    sides = frame["bout"].apply(lambda b: _BOUT_SPLIT.split(b, maxsplit=1))
    frame["fighter_a"] = sides.apply(lambda s: s[0].strip() if len(s) == 2 else None)
    frame["fighter_b"] = sides.apply(lambda s: s[1].strip() if len(s) == 2 else None)
    frame = frame.dropna(subset=["fighter_a", "fighter_b"])

    outcome = frame["OUTCOME"].str.strip()
    frame["winner"] = outcome.map({"W/L": "a", "L/W": "b"})
    frame["outcome"] = outcome.map(
        {"W/L": "win", "L/W": "win", "D/D": "draw", "NC/NC": "nc"}
    ).fillna("nc")

    frame["weight_class"] = frame["WEIGHTCLASS"].str.strip()
    frame["title_fight"] = frame["weight_class"].str.contains("Title", case=False, na=False)
    frame["method"] = frame["METHOD"].str.strip()
    frame["method_class"] = frame["method"].apply(_method_class)
    frame["method_detail"] = frame["method"].apply(method_detail)
    frame["technique"] = [
        technique_from_ufcstats(detail, method, details)
        for detail, method, details in zip(
            frame["method_detail"], frame["method"], frame["DETAILS"].fillna("").astype(str)
        )
    ]
    frame["end_round"] = pd.to_numeric(frame["ROUND"], errors="coerce").fillna(1).astype(int)
    frame["end_time_seconds"] = frame["TIME"].apply(_mmss_to_seconds)
    frame["time_format"] = frame["TIME FORMAT"].fillna("").str.strip()
    frame["scheduled_rounds"] = frame["time_format"].apply(
        lambda f: len(_round_lengths_minutes(f))
    )
    frame["total_seconds"] = [
        _total_fight_seconds(f, r, t)
        for f, r, t in zip(frame["time_format"], frame["end_round"], frame["end_time_seconds"])
    ]

    # Oldest first, so a chronological pass builds each fighter's history in order.
    # Fights within a card carry no ordering, so keep file order reversed for them.
    frame = frame.reset_index(drop=True)
    frame["file_order"] = -frame.index
    frame = frame.sort_values(["date", "file_order"], kind="stable").reset_index(drop=True)
    frame["fight_id"] = frame.index

    return frame[
        [
            "fight_id", "event", "date", "bout", "fighter_a", "fighter_b", "winner",
            "outcome", "weight_class", "title_fight", "method", "method_class",
            "method_detail", "technique",
            "end_round", "end_time_seconds", "scheduled_rounds", "total_seconds",
        ]
    ]


def _build_fight_stats(fights: pd.DataFrame, stats: pd.DataFrame) -> pd.DataFrame:
    frame = stats.assign(
        event=stats["EVENT"].str.strip(),
        bout=stats["BOUT"].str.strip(),
        fighter=stats["FIGHTER"].str.strip(),
    ).dropna(subset=["fighter"])

    pairs = {
        "sig": "SIG.STR.",
        "tot": "TOTAL STR.",
        "td": "TD",
        "head": "HEAD",
        "body": "BODY",
        "leg": "LEG",
        "dist": "DISTANCE",
        "clinch": "CLINCH",
        "ground": "GROUND",
    }
    for prefix, column in pairs.items():
        parsed = frame[column].apply(_landed_attempted)
        frame[f"{prefix}_l"] = [p[0] for p in parsed]
        frame[f"{prefix}_a"] = [p[1] for p in parsed]

    frame["kd"] = pd.to_numeric(frame["KD"], errors="coerce")
    frame["sub_att"] = pd.to_numeric(frame["SUB.ATT"], errors="coerce")
    frame["rev"] = pd.to_numeric(frame["REV."], errors="coerce")
    frame["ctrl_s"] = frame["CTRL"].apply(_mmss_to_seconds)

    numeric = [c for c in frame.columns if c.endswith(("_l", "_a"))] + [
        "kd", "sub_att", "rev", "ctrl_s"
    ]

    # Sum every round into one line per fighter per fight. A fight with no stats
    # on ufcstats.com has NaN throughout and stays NaN, which is what we want:
    # it must not count toward fight time or any rate.
    totals = (
        frame.groupby(["event", "bout", "fighter"], sort=False)[numeric]
        .sum(min_count=1)
        .reset_index()
    )

    keyed = fights[["fight_id", "event", "bout", "fighter_a", "fighter_b"]]
    merged = totals.merge(keyed, on=["event", "bout"], how="inner")

    # Names in the stats table occasionally differ from the bout title by
    # punctuation or accents, so compare their normalised forms.
    norm = merged["fighter"].map(normalise)
    is_a = norm == merged["fighter_a"].map(normalise)
    is_b = norm == merged["fighter_b"].map(normalise)
    merged["side"] = pd.Series(pd.NA, index=merged.index, dtype="object")
    merged.loc[is_a, "side"] = "a"
    merged.loc[is_b & ~is_a, "side"] = "b"
    merged = merged.dropna(subset=["side"])
    merged = merged.drop_duplicates(["fight_id", "side"])

    return merged[["fight_id", "side", "fighter"] + numeric].reset_index(drop=True)


def _build_fighters(tott: pd.DataFrame) -> dict[str, FighterInfo]:
    fighters: dict[str, FighterInfo] = {}
    dobs = pd.to_datetime(tott["DOB"], format="%b %d, %Y", errors="coerce")

    for row, dob in zip(tott.itertuples(index=False), dobs):
        name = str(row.FIGHTER).strip()
        if not name or name == "nan":
            continue
        key = normalise(name)
        info = FighterInfo(
            name=name,
            height_in=_inches(row.HEIGHT),
            weight_lb=_pounds(row.WEIGHT),
            reach_in=_inches(row.REACH),
            stance=str(row.STANCE).strip() if pd.notna(row.STANCE) else None,
            dob=dob.date() if pd.notna(dob) else None,
            ufcstats_url=str(row.URL) if pd.notna(row.URL) else None,
        )
        # Two fighters can share a name. Keep the entry with the most detail,
        # which is the best that can be done with a name-keyed source.
        existing = fighters.get(key)
        if existing is None or _detail_score(info) > _detail_score(existing):
            fighters[key] = info
    return fighters


def _detail_score(info: FighterInfo) -> int:
    return sum(
        1
        for value in (info.height_in, info.reach_in, info.dob, info.stance)
        if value is not None and not (isinstance(value, float) and pd.isna(value))
    )
