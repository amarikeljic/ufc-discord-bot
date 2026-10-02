"""Spotting how a ratings board moved, and saying why."""

from __future__ import annotations

from datetime import date, timedelta

from ufcbot.embeds import ratings_changes_embed
from ufcbot.features.ratings import DOWN, ENTERED, INACTIVE, LEFT, PUSHED, UP, diff
from ufcbot.records import RankedState
from ufcbot.stats.career import Ledger

TODAY = date(2026, 9, 18)
RECENT = TODAY - timedelta(days=30)
OLD = TODAY - timedelta(days=1000)


def ledger(name: str, elo: float, *, last: date | None = RECENT, result: str = "win") -> Ledger:
    entry = Ledger(name=name)
    entry.elo = elo
    entry.fights = 8
    entry.division = "Lightweight"
    entry.last_fight = last
    entry.last_result = result
    return entry


def ranked(key: str, rank: int, rating: int, last: date | None = RECENT):
    """A board row in the shape the diff compares against."""
    return RankedState(key, rank, rating, last)


class Row:
    """What rank_division hands back, reduced to what the diff reads."""

    def __init__(self, key, rank, rating, last=RECENT, result="win", name=None):
        self.key, self.rank, self.rating = key, rank, rating
        self.last_fight, self.last_result, self.name = last, result, name or key.title()


def test_a_board_that_has_not_moved_reports_nothing():
    previous = {"a": ranked("a", 1, 1200), "b": ranked("b", 2, 1150)}
    current = [Row("a", 1, 1200), Row("b", 2, 1150)]
    assert diff(previous, current, {}, on=TODAY) == []


def test_climbing_after_a_win_says_so():
    previous = {"a": ranked("a", 1, 1200), "b": ranked("b", 2, 1150, last=OLD)}
    current = [Row("b", 1, 1260, last=RECENT, result="win"), Row("a", 2, 1200)]

    changes = {c.name: c for c in diff(previous, current, {}, on=TODAY)}

    assert changes["B"].kind == UP and (changes["B"].was, changes["B"].now) == (2, 1)
    assert changes["B"].reason == "a win"
    # The fighter they passed did not fight; it was not their doing.
    assert changes["A"].kind == DOWN and changes["A"].reason == PUSHED


def test_sliding_after_a_loss_says_so():
    previous = {"a": ranked("a", 1, 1200, last=OLD)}
    current = [Row("a", 1, 1100, last=RECENT, result="loss")]
    # Same rank, so nothing is announced; the reason only matters when they move.
    assert diff(previous, current, {}, on=TODAY) == []

    current = [Row("b", 1, 1300, last=OLD, result="win"), Row("a", 2, 1100, last=RECENT, result="loss")]
    changes = {c.name: c for c in diff(previous, current, {}, on=TODAY)}
    assert changes["A"].kind == DOWN and changes["A"].reason == "a loss"


def test_a_new_entrant_is_reported_with_how_they_got_there():
    """Their last fight is later than anything the board knew about, so a card
    has happened since it was published and that is how they got there."""
    previous = {"a": ranked("a", 1, 1200, last=OLD)}
    current = [Row("a", 1, 1200, last=OLD), Row("newcomer", 2, 1150, last=RECENT, result="win")]

    change = next(c for c in diff(previous, current, {}, on=TODAY) if c.kind == ENTERED)

    assert change.name == "Newcomer" and change.now == 2 and change.reason == "a win"


def test_drifting_up_into_the_board_is_not_an_entrance():
    """Nobody has fought since it was last published. The fighter below fifteenth
    did not arrive; the fighter above them faded."""
    previous = {"a": ranked("a", 1, 1200, last=OLD)}
    current = [Row("a", 1, 1200, last=OLD), Row("drifter", 2, 1150, last=OLD)]

    assert diff(previous, current, {"a": ledger("A", 1200, last=OLD)}, on=TODAY) == []


def test_dropping_off_through_inactivity_says_so():
    previous = {"a": ranked("a", 1, 1200), "gone": ranked("gone", 2, 1150, last=OLD)}
    current = [Row("a", 1, 1200)]
    ledgers = {"gone": ledger("Gone Fighter", 1150, last=OLD)}

    change = next(c for c in diff(previous, current, ledgers, on=TODAY) if c.kind == LEFT)

    assert change.name == "Gone Fighter" and change.was == 2
    assert change.reason == INACTIVE


def test_being_pushed_off_the_bottom_is_not_called_inactivity():
    """Pushed off by somebody's result, so it is reported -- and as being pushed,
    not as having gone inactive."""
    previous = {"a": ranked("a", 2, 1150, last=OLD), "pushed": ranked("pushed", 1, 1200, last=OLD)}
    current = [Row("a", 1, 1290, last=RECENT, result="win")]
    ledgers = {"pushed": ledger("Still Active", 1150, last=RECENT)}

    change = next(c for c in diff(previous, current, ledgers, on=TODAY) if c.kind == LEFT)

    assert change.reason == PUSHED


def test_slipping_off_the_bottom_with_nobody_fighting_is_not_announced():
    """The fade moves a rating every day, so the fifteenth place changes hands
    on its own. That is the same non-event as slipping a place inside the board,
    reaching it through the end instead of the middle."""
    previous = {"a": ranked("a", 1, 1200, last=OLD), "slipped": ranked("slipped", 2, 1150, last=OLD)}
    current = [Row("a", 1, 1200, last=OLD)]
    ledgers = {"slipped": ledger("Still Active", 1150, last=RECENT)}

    assert diff(previous, current, ledgers, on=TODAY) == []


def test_the_embed_reads_as_what_happened():
    previous = {"a": ranked("a", 3, 1200, last=OLD), "gone": ranked("gone", 5, 1100, last=OLD)}
    current = [Row("a", 1, 1290, last=RECENT, result="win", name="Islam Makhachev")]
    ledgers = {"gone": ledger("Old Timer", 1100, last=OLD)}

    value = ratings_changes_embed(
        "Lightweight", diff(previous, current, ledgers, on=TODAY)
    ).fields[0].value.replace("\xa0", " ")

    assert "Islam Makhachev" in value and "3 → 1" in value and "after a win" in value
    assert "Old Timer" in value and "out, was **5**" in value


def test_a_board_that_only_got_a_day_older_has_not_moved():
    """The displayed rating fades by the day a fighter is idle, so a board drifts
    with nobody fighting. Over 180 days that produced 130 reported moves, 112 of
    which were nobody passing anybody. A fighter slipping a place because the
    calendar advanced is not news."""
    from ufcbot.features.ratings import diff
    from ufcbot.records import RankedState
    from ufcbot.stats.rankings import Ranked

    fought_on = date(2026, 1, 10)
    before = {
        "a": RankedState("a", 1, 1100, fought_on),
        "b": RankedState("b", 2, 1098, fought_on),
    }
    # They swap, and neither has fought since.
    after = [
        Ranked(rank=1, name="B", rating=1099, division="Lightweight", record="9-1-0",
               key="b", last_fight=fought_on),
        Ranked(rank=2, name="A", rating=1097, division="Lightweight", record="9-2-0",
               key="a", last_fight=fought_on),
    ]
    assert diff(before, after, {}, on=TODAY) == []


def test_a_board_that_moved_because_somebody_fought_is_reported():
    from ufcbot.features.ratings import diff
    from ufcbot.records import RankedState
    from ufcbot.stats.rankings import Ranked

    before = {
        "a": RankedState("a", 1, 1100, date(2026, 1, 10)),
        "b": RankedState("b", 2, 1098, date(2026, 1, 10)),
    }
    after = [
        Ranked(rank=1, name="B", rating=1130, division="Lightweight", record="10-1-0",
               key="b", last_fight=date(2026, 3, 1), last_result="win"),
        Ranked(rank=2, name="A", rating=1100, division="Lightweight", record="9-2-0",
               key="a", last_fight=date(2026, 1, 10)),
    ]
    moves = diff(before, after, {}, on=TODAY)

    assert [(c.name, c.was, c.now) for c in moves] == [("B", 2, 1), ("A", 1, 2)]


def test_ageing_off_the_board_is_still_reported_without_a_fight():
    """Eighteen months without a fight is a change of state rather than drift,
    and it is the one thing about an idle fighter worth saying."""
    from ufcbot.features.ratings import LEFT, diff
    from ufcbot.records import RankedState

    before = {"gone": RankedState("gone", 3, 1050, date(2024, 1, 1))}

    moves = diff(before, [], {}, on=TODAY)

    assert [c.kind for c in moves] == [LEFT]


def test_one_fight_does_not_release_a_week_of_drift():
    """The trap in gating per pass rather than per move.

    The UFC runs most weekends, so a gate that opens whenever anybody fought
    holds a week of fade and then lets all of it out on the first pass after a
    card -- where it reads as a consequence of that card rather than of the
    calendar. Only the pair who actually swapped around the fight should appear.
    """
    previous = {
        "winner": ranked("winner", 4, 1100, last=OLD),
        "beaten": ranked("beaten", 3, 1120, last=OLD),
        "drift_a": ranked("drift_a", 1, 1200, last=OLD),
        "drift_b": ranked("drift_b", 2, 1190, last=OLD),
    }
    current = [
        # These two swapped because the fade moved them, with no fight between.
        Row("drift_b", 1, 1188, last=OLD),
        Row("drift_a", 2, 1187, last=OLD),
        # And these two swapped because one of them won on Saturday.
        Row("winner", 3, 1140, last=RECENT, result="win"),
        Row("beaten", 4, 1118, last=RECENT, result="loss"),
    ]
    moved = {c.name: c for c in diff(previous, current, {}, on=TODAY)}

    assert set(moved) == {"Winner", "Beaten"}, "the drifting pair rode out on the card"
    assert moved["Winner"].kind == UP and moved["Winner"].reason == "a win"
    assert moved["Beaten"].kind == DOWN


def test_a_fighter_pushed_down_by_someone_elses_win_is_still_reported():
    """The knock-on is the half worth keeping: they did not fight, but the
    fighter who passed them did."""
    previous = {
        "winner": ranked("winner", 2, 1100, last=OLD),
        "passed": ranked("passed", 1, 1120, last=OLD),
    }
    current = [
        Row("winner", 1, 1150, last=RECENT, result="win"),
        Row("passed", 2, 1120, last=OLD),
    ]
    moved = {c.name: c for c in diff(previous, current, {}, on=TODAY)}

    assert moved["Passed"].kind == DOWN, "passed by somebody who fought"
    assert moved["Passed"].reason == PUSHED


def test_a_fighter_returning_from_a_layoff_cannot_climb_by_losing():
    """The fade comes off the moment someone fights, so a returning fighter gets
    their layoff back and pays for the result out of it. Carrying more than half
    of K they come back from a loss with a higher number than they left with --
    Poirier is carrying 21 points of it, Dos Anjos 38 -- and the board moves them
    up. There is no sentence about a man losing and climbing that is not wrong."""
    previous = {
        "returning": ranked("returning", 5, 1137, last=OLD),   # shown faded
        "steady": ranked("steady", 4, 1140, last=OLD),
    }
    current = [
        Row("steady", 5, 1140, last=OLD),
        Row("returning", 4, 1142, last=RECENT, result="loss"),  # fade back, minus the loss
    ]
    moved = {c.name: c for c in diff(previous, current, {}, on=TODAY)}

    assert "Returning" not in moved, "lost, and would have been announced as up"
    assert moved["Steady"].kind == DOWN, "passed by somebody who fought, so still news"


def test_a_fighter_who_wins_and_climbs_is_still_announced():
    previous = {"a": ranked("a", 2, 1100, last=OLD), "b": ranked("b", 1, 1120, last=OLD)}
    current = [
        Row("a", 1, 1150, last=RECENT, result="win"),
        Row("b", 2, 1120, last=OLD),
    ]
    moved = {c.name: c for c in diff(previous, current, {}, on=TODAY)}

    assert moved["A"].kind == UP and moved["A"].reason == "a win"
