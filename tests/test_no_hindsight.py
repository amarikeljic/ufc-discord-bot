"""No hindsight in the rows the model trains on.

The test that matters is the first one, and it is the one asked for in so many
words: compute a fight's row, delete every later fight from the data, compute
it again, and check nothing moved. It runs that for every fight in a small
league built through the real loading code, over every column the model reads.

The second closes the first one's blind spot. Deleting later fights leaves the
fight itself in place, so a feature that read its own result or its own stat
sheet would pass. Two leaks injected on purpose -- one reading the fight's
winner, one reading its own strikes -- did exactly that, and only the second
test caught them.

Neither can see the tale of the tape, which is today's profile rather than the
fight night's; see the note on listed weight in ``features.py``.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from ufcbot.stats import dataset as ds
from ufcbot.stats.career import Ledger, build_history
from ufcbot.stats.features import FEATURE_NAMES
from ufcbot.stats.model import _design_matrix

# Six fighters, twelve fights, six cards, the main event listed first on each
# card as ufcstats lists it. Every running total gets something to do: finishes
# and decisions, a title fight, five-rounders, and one comeback from a
# knockdown (Delta, dropped by Echo at UFC 3, wins anyway).
CARDS = [
    ("UFC 1", "January 15, 2020", [
        ("Alpha One", "Bravo Two", "W/L", "Lightweight Bout", "KO/TKO", 1, "3:10", 3, (0, 1, 0)),
        ("Charlie Three", "Delta Four", "W/L", "Lightweight Bout", "Decision - Unanimous", 3, "5:00", 3, (0, 0, 0)),
    ]),
    ("UFC 2", "March 15, 2020", [
        ("Alpha One", "Charlie Three", "W/L", "UFC Lightweight Title Bout", "Decision - Unanimous", 5, "5:00", 5, (0, 0, 0)),
        ("Bravo Two", "Echo Five", "L/W", "Lightweight Bout", "Submission", 2, "1:45", 3, (0, 0, 0)),
    ]),
    ("UFC 3", "May 15, 2020", [
        ("Delta Four", "Echo Five", "W/L", "Lightweight Bout", "KO/TKO", 3, "2:20", 3, (1, 1, 0)),
        ("Foxtrot Six", "Bravo Two", "W/L", "Lightweight Bout", "Decision - Split", 3, "5:00", 3, (0, 0, 0)),
    ]),
    ("UFC 4", "July 15, 2020", [
        ("Delta Four", "Alpha One", "W/L", "Lightweight Bout", "Decision - Unanimous", 5, "5:00", 5, (0, 0, 0)),
        ("Charlie Three", "Foxtrot Six", "W/L", "Lightweight Bout", "KO/TKO", 2, "4:05", 3, (0, 1, 0)),
    ]),
    ("UFC 5", "September 15, 2020", [
        ("Echo Five", "Charlie Three", "W/L", "Lightweight Bout", "Decision - Unanimous", 3, "5:00", 3, (0, 0, 0)),
        ("Alpha One", "Foxtrot Six", "W/L", "Lightweight Bout", "Submission", 1, "2:50", 3, (0, 0, 0)),
    ]),
    ("UFC 6", "November 15, 2020", [
        ("Delta Four", "Foxtrot Six", "W/L", "Lightweight Bout", "Decision - Unanimous", 5, "5:00", 5, (0, 0, 0)),
        ("Bravo Two", "Charlie Three", "W/L", "Lightweight Bout", "KO/TKO", 1, "0:55", 3, (0, 1, 0)),
    ]),
]
FIGHTERS = ["Alpha One", "Bravo Two", "Charlie Three", "Delta Four", "Echo Five", "Foxtrot Six"]


def _of(landed: int, attempted: int) -> str:
    return f"{landed} of {attempted}"


def write_league(directory, *, until: str | None = None, rewrite: tuple[str, int] | None = None) -> None:
    """The league as the four CSVs ufcstats publishes, every card after ``until``
    deleted, and everything that happened in the bout ``rewrite`` names changed:
    who won, how, when, and every number on its stat sheet. Only its billing
    stays -- the weight class, the title, the scheduled rounds, its place on the
    card -- because that was known before it started."""
    cutoff = pd.to_datetime(until, format="%B %d, %Y") if until else None
    events, results, stats = [], [], []
    for n, (event, when, bouts) in enumerate(CARDS):
        if cutoff is not None and pd.to_datetime(when, format="%B %d, %Y") > cutoff:
            continue
        events.append({"EVENT": event, "DATE": when, "LOCATION": "Las Vegas"})
        for m, (a, b, outcome, weight, method, rnd, clock, rounds, kds) in enumerate(bouts):
            rewritten = rewrite == (event, m)
            if rewritten:
                outcome = {"W/L": "L/W", "L/W": "W/L"}[outcome]
                method = "KO/TKO" if method == "Submission" else "Submission"
                rnd, clock = (2, "0:31") if rnd == 1 else (1, "0:31")
                kds = (0, 2, 0) if kds[0] else (2, 0, 0)
            fmt = "5 Rnd (5-5-5-5-5)" if rounds == 5 else "3 Rnd (5-5-5)"
            results.append({
                "EVENT": event, "BOUT": f"{a} vs. {b}", "OUTCOME": outcome, "WEIGHTCLASS": weight,
                "METHOD": method, "ROUND": rnd, "TIME": clock, "TIME FORMAT": fmt,
                "REFEREE": "Herb Dean", "DETAILS": "", "URL": f"http://example/{n}-{m}",
            })
            kd_a, kd_b, _ = kds
            for side, (fighter, kd) in enumerate(((a, kd_a), (b, kd_b))):
                # Varied but fixed, so every rate and share is a real number.
                seed = 7 * n + 3 * m + 11 * side + (5 if rewritten else 0)
                landed, attempted = 20 + seed % 23, 45 + seed % 31
                stats.append({
                    "EVENT": event, "BOUT": f"{a} vs. {b}", "ROUND": "Round 1", "FIGHTER": fighter,
                    "KD": kd, "SIG.STR.": _of(landed, attempted), "SIG.STR. %": "50%",
                    "TOTAL STR.": _of(landed + 9, attempted + 12),
                    "TD": _of(seed % 4, 2 + seed % 5), "TD %": "40%",
                    "SUB.ATT": seed % 3, "REV.": 0, "CTRL": f"{seed % 4}:{(seed * 7) % 60:02d}",
                    "HEAD": _of(landed // 2, attempted // 2), "BODY": _of(landed // 4, attempted // 4),
                    "LEG": _of(landed - landed // 2 - landed // 4, attempted // 4),
                    "DISTANCE": _of(landed - 3, attempted - 5), "CLINCH": _of(2, 3), "GROUND": _of(1, 2),
                })
    tott = [
        {"FIGHTER": name, "HEIGHT": "5' 10\"", "WEIGHT": "155 lbs.", "REACH": f"{70 + i}\"",
         "STANCE": "Orthodox" if i % 2 else "Southpaw", "DOB": f"Jan 0{i + 1}, 1990",
         "URL": f"http://example/{i}"}
        for i, name in enumerate(FIGHTERS)
    ]
    pd.DataFrame(events).to_csv(directory / "ufc_event_details.csv", index=False)
    pd.DataFrame(results).to_csv(directory / "ufc_fight_results.csv", index=False)
    pd.DataFrame(stats).to_csv(directory / "ufc_fight_stats.csv", index=False)
    pd.DataFrame(tott).to_csv(directory / "ufc_fighter_tott.csv", index=False)


def every_row(directory) -> dict[tuple, np.ndarray]:
    """(date, fighter, fighter) -> the row the model reads for that fight."""
    dataset = ds.load(directory)
    history = build_history(dataset, keep_snapshots=True)
    rows, _y, _dates, _ = _design_matrix(history, dataset.fighters)
    return {
        (snap.on, snap.fighter_a, snap.fighter_b): rows[2 * i]
        for i, snap in enumerate(history.snapshots)
    }


def test_deleting_every_later_fight_changes_nothing_about_an_earlier_one(tmp_path):
    """For each fight in turn: its row with the whole league loaded, against its
    row with every later card deleted from the data before anything is
    computed. Opponent ratings, the model's own Elo and every running total
    have to come out the same, because none of them may know what happened
    next."""
    whole = tmp_path / "whole"
    whole.mkdir()
    write_league(whole)
    complete = every_row(whole)
    assert len(complete) == 12
    assert complete[next(iter(complete))].shape == (len(FEATURE_NAMES),)

    for _event, when, _bouts in CARDS:
        cut = tmp_path / when.replace(" ", "_").replace(",", "")
        cut.mkdir()
        write_league(cut, until=when)
        truncated = every_row(cut)
        for key, row in truncated.items():
            np.testing.assert_array_equal(
                row, complete[key], err_msg=f"{key} changed when later cards were deleted"
            )


def test_a_fight_cannot_see_its_own_result(tmp_path):
    """A row built only from earlier fights cannot care who won the fight it
    describes, or how, or what anybody landed in it."""
    whole = tmp_path / "whole"
    whole.mkdir()
    write_league(whole)
    complete = every_row(whole)

    for event, _when, bouts in CARDS:
        for m, (a, b, *_rest) in enumerate(bouts):
            changed = tmp_path / f"{event}-{m}".replace(" ", "_")
            changed.mkdir()
            write_league(changed, rewrite=(event, m))
            rows = every_row(changed)
            # Only this fight's row is held to it. Later fights rightly see the
            # rewritten one.
            key = next(k for k in rows if k[1] == a and k[2] == b)
            np.testing.assert_array_equal(
                rows[key], complete[key], err_msg=f"{key} moved when its own fight was rewritten"
            )
            # And the rewrite has to have reached something, or the check
            # above passes because nothing changed anywhere.
            later = [k for k in rows if k[0] > key[0] and {a, b} & set(k[1:])]
            if later:
                assert any(
                    not np.array_equal(rows[k], complete[k], equal_nan=True) for k in later
                ), f"rewriting {key} changed none of its fighters' later rows"


def test_a_snapshot_does_not_go_on_filling_up_after_it_is_taken():
    """snapshot() is a shallow copy and used to re-copy only the containers it
    named. The per-division ones added later were shared with the live ledger,
    so a snapshot's division_elo read ratings from fights that came after it."""
    live = Ledger(name="Fighter")
    live.division_elo["Lightweight"] = 1100.0
    live.fights_by_division["Lightweight"] = 3
    before = live.snapshot()

    live.division_elo["Lightweight"] = 1300.0
    live.division_elo["Welterweight"] = 1050.0
    live.fights_by_division["Lightweight"] = 9

    assert before.division_elo == {"Lightweight": 1100.0}
    assert before.fights_by_division == {"Lightweight": 3}
