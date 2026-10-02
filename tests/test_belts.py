"""Belt changes the fight data cannot record.

A vacancy, a stripping, a retirement and an elevation all happen outside a cage,
so no fight records them and the derived champion stays where the last title
fight left it. The file that carries them is hand-kept, which is the whole risk:
these tests are mostly about it being allowed to go stale without doing harm.
"""

from __future__ import annotations

import json
from datetime import date

from ufcbot.stats.belts import INTERIM, UNDISPUTED, VACATED, BeltChange, apply, load

DERIVED = {"Heavyweight": "jon jones", "Lightweight": "justin gaethje"}
LAST_FIGHT = {"Heavyweight": date(2024, 11, 16), "Lightweight": date(2026, 2, 1)}


def keys(name: str) -> str | None:
    return {"Tom Aspinall": "tom aspinall", "Ciryl Gane": "ciryl gane"}.get(name)


def test_an_elevation_puts_the_belt_where_no_fight_did():
    """Aspinall vacated and Gane was elevated. Neither is a fight, so the data
    still has the heavyweight belt with Jon Jones from two years earlier."""
    changes = [BeltChange("Heavyweight", "Ciryl Gane", UNDISPUTED, date(2025, 6, 14))]

    champions, warnings = apply(DERIVED, LAST_FIGHT, changes, resolve=keys)

    assert champions["Heavyweight"] == "ciryl gane"
    assert champions["Lightweight"] == "justin gaethje", "untouched divisions stay derived"
    assert warnings == []


def test_a_vacated_belt_is_held_by_nobody():
    changes = [BeltChange("Heavyweight", "Tom Aspinall", VACATED, date(2025, 6, 14))]

    champions, _ = apply(DERIVED, LAST_FIGHT, changes, resolve=keys)

    assert "Heavyweight" not in champions


def test_a_title_fight_after_an_entry_overrules_it():
    """The guard against this file going stale. An entry nobody deletes stops
    mattering the moment somebody fights for the belt, rather than pinning a
    champion forever and recreating the problem it was written to fix."""
    changes = [BeltChange("Lightweight", "Ciryl Gane", UNDISPUTED, date(2025, 1, 1))]

    champions, warnings = apply(DERIVED, LAST_FIGHT, changes, resolve=keys)

    assert champions["Lightweight"] == "justin gaethje", "the cage decides it now"
    assert len(warnings) == 1 and "can go" in warnings[0]


def test_an_entry_naming_nobody_is_said_out_loud_and_ignored():
    changes = [BeltChange("Heavyweight", "Nobody At All", UNDISPUTED, date(2025, 6, 14))]

    champions, warnings = apply(DERIVED, LAST_FIGHT, changes, resolve=keys)

    assert champions["Heavyweight"] == "jon jones", "left where the data put it"
    assert len(warnings) == 1 and "no fighter found" in warnings[0]


def test_later_entries_win_over_earlier_ones():
    changes = [
        BeltChange("Heavyweight", "Tom Aspinall", INTERIM, date(2023, 11, 11)),
        BeltChange("Heavyweight", "Ciryl Gane", UNDISPUTED, date(2026, 6, 14)),
    ]

    champions, _ = apply(DERIVED, LAST_FIGHT, changes, resolve=keys)

    assert champions["Heavyweight"] == "ciryl gane"


def test_a_file_that_will_not_parse_costs_the_markers_and_not_the_bot(tmp_path):
    (tmp_path / "belts.json").write_text("{ not json", encoding="utf-8")
    assert load(tmp_path) == []


def test_an_entry_missing_a_field_is_skipped_and_the_rest_are_read(tmp_path):
    (tmp_path / "belts.json").write_text(
        json.dumps(
            [
                {"division": "Heavyweight", "fighter": "Ciryl Gane"},  # no status, no date
                {
                    "division": "Heavyweight",
                    "fighter": "Ciryl Gane",
                    "status": "undisputed",
                    "on": "2025-06-14",
                    "source": "https://example.invalid/news",
                },
                {"division": "X", "fighter": "Y", "status": "king", "on": "2025-01-01"},
            ]
        ),
        encoding="utf-8",
    )
    read = load(tmp_path)

    assert [c.fighter for c in read] == ["Ciryl Gane"]
    assert read[0].on == date(2025, 6, 14) and read[0].source.startswith("https://")


def test_no_file_means_the_data_decides_everything(tmp_path):
    assert load(tmp_path) == []
    assert apply(DERIVED, LAST_FIGHT, [], resolve=keys) == (DERIVED, [])
