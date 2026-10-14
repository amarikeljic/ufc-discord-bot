"""Reading the ufcstats CSVs, where one fighter can arrive as two people.

ufcstats writes the current name into the bout line and leaves the old one on
the fighter page, so a fighter it has renamed mid-career splits in half: two
names, half a record each, and whichever the next card happens to use is the one
the model reads. Nothing about that looks wrong from the outside.
"""

from __future__ import annotations

import pandas as pd

from ufcbot.stats.dataset import (
    _build_fighters,
    _build_fights,
    _orphan_names,
    canonical,
)


def events(*rows) -> pd.DataFrame:
    return pd.DataFrame(
        [{"EVENT": name, "DATE": date} for name, date in rows]
    )


def results(*rows) -> pd.DataFrame:
    return pd.DataFrame([
        {
            "EVENT": event, "BOUT": bout, "OUTCOME": "W/L", "WEIGHTCLASS": "Lightweight",
            "METHOD": "Decision - Unanimous", "ROUND": 3, "TIME": "5:00",
            "TIME FORMAT": "3 Rnd (5-5-5)", "REFEREE": "Herb Dean", "DETAILS": "",
            "URL": "http://example/x",
        }
        for event, bout in rows
    ])


def tott(*names) -> pd.DataFrame:
    return pd.DataFrame([
        {
            "FIGHTER": name, "HEIGHT": "5' 6\"", "WEIGHT": "145 lbs.", "REACH": "67\"",
            "STANCE": "Orthodox", "DOB": "Jul 07, 1987", "URL": f"http://example/{name}",
        }
        for name in names
    ])


# -- the alias map ------------------------------------------------------------


def test_a_renamed_fighter_is_filed_under_one_name():
    assert canonical("Patricio Freire") == "Patricio Pitbull"
    assert canonical("patricio freire") == "Patricio Pitbull", "on the normalised name"


def test_everybody_else_is_left_exactly_as_written():
    for name in ("Arnold Allen", "Matt Hughes", "Mark Hughes", "Jake Ellenberger"):
        assert canonical(name) == name


# -- what the map is for ------------------------------------------------------


def test_a_rename_no_longer_splits_a_record_in_two():
    """Four fights under one man rather than two apiece under two."""
    frame = _build_fights(
        events(("UFC 1", "April 12, 2025"), ("UFC 2", "September 19, 2026")),
        results(
            ("UFC 1", "Yair Rodriguez vs. Patricio Freire"),
            ("UFC 2", "Patricio Pitbull vs. Dooho Choi"),
        ),
    )

    appearances = [
        name for column in ("fighter_a", "fighter_b") for name in frame[column]
    ]

    assert appearances.count("Patricio Pitbull") == 2
    assert "Patricio Freire" not in appearances


def test_the_tale_of_the_tape_follows_the_fights():
    """Merging the record and leaving the height behind would fix half of it:
    the page is under the old name and the bouts are under the new one."""
    fighters = _build_fighters(tott("Patricio Freire"))

    assert "patricio pitbull" in fighters
    assert "patricio freire" not in fighters
    assert fighters["patricio pitbull"].height_in == 66


# -- and the detector that finds the next one ---------------------------------


def test_a_fighter_with_no_page_is_reported():
    """Most are names upstream has no page for, which costs their physical
    features. The rest are a rename, which costs half a record. Neither can be
    told from the other without looking, so the job here is to say they exist."""
    frame = _build_fights(
        events(("UFC 1", "April 12, 2025")),
        results(("UFC 1", "Arnold Allen vs. Someone Unlisted")),
    )

    orphans = _orphan_names(frame, _build_fighters(tott("Arnold Allen")))

    assert orphans == ["someone unlisted"]


def test_the_most_recent_are_reported_first():
    """A rename happens to somebody still fighting; a gap in 2013 is nobody's
    problem now."""
    frame = _build_fights(
        events(("UFC 1", "April 12, 2013"), ("UFC 2", "September 19, 2026")),
        results(
            ("UFC 1", "Arnold Allen vs. Long Forgotten"),
            ("UFC 2", "Arnold Allen vs. Lately Renamed"),
        ),
    )

    orphans = _orphan_names(frame, _build_fighters(tott("Arnold Allen")))

    assert orphans == ["lately renamed", "long forgotten"]


def test_nobody_is_reported_when_every_fighter_has_a_page():
    frame = _build_fights(
        events(("UFC 1", "April 12, 2025")),
        results(("UFC 1", "Arnold Allen vs. Aaron Pico")),
    )

    assert _orphan_names(frame, _build_fighters(tott("Arnold Allen", "Aaron Pico"))) == []
