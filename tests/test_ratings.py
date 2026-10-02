"""Spotting how a ratings board moved, and saying why."""

from __future__ import annotations

from datetime import date, timedelta

from ufcbot.embeds import ratings_changes_embed
from ufcbot.features.ratings import (
    DOWN,
    ENTERED,
    INACTIVE,
    LEFT,
    PUSHED,
    RETURNED,
    UP,
    diff,
)
from ufcbot.records import RankedState
from ufcbot.stats.career import Ledger

TODAY = date(2026, 9, 18)
RECENT = TODAY - timedelta(days=30)
# A fight before the one in the test, close enough that nothing is fading. The
# diff reads "did they fight" from last_fight changing, so a fixture that uses
# the same date on both sides of a pass is a fighter who did not fight.
BEFORE = TODAY - timedelta(days=90)
OLD = TODAY - timedelta(days=1000)


def ledger(name: str, elo: float, *, last: date | None = RECENT, result: str = "win") -> Ledger:
    entry = Ledger(name=name)
    entry.elo = elo
    entry.fights = 8
    entry.division = "Lightweight"
    entry.last_fight = last
    entry.last_result = result
    return entry


def ranked(key: str, rank: int, rating: int, last: date | None = RECENT, raw: int | None = None):
    """A board row in the shape the diff compares against.

    ``raw`` is the rating before the layoff fade. It defaults to the shown
    rating, which is what a fighter who is not fading has.
    """
    return RankedState(key, rank, rating, last, rating if raw is None else raw)


def fought_on(
    was: RankedState, card: date, rank: int, rating: int, *, result: str, name: str | None = None
):
    """A fighter who fought on ``card``, which is after the last time they did.

    The date is the card's, not the old one nudged forward, because the rest of
    the bot reads it as a date and not only as a thing that changed: a fighter
    idle 440 days whose "new" fight is dated day 441 is still 439 days idle at
    the pass, and would age out and read as inactive while supposedly having
    just fought. The assertion is what stops a fixture claiming a result while
    holding the date still, which several tests here once did -- and passed,
    testing nothing.
    """
    assert was.last_fight is not None, "cannot fight again after never having fought"
    assert card > was.last_fight, "a fight has to be later than the one before it"
    return Row(was.fighter, rank, rating, last=card, result=result, name=name)


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
    previous = {"a": ranked("a", 1, 1200), "b": ranked("b", 2, 1150, last=BEFORE)}
    current = [Row("b", 1, 1260, last=RECENT, result="win"), Row("a", 2, 1200)]

    changes = {c.name: c for c in diff(previous, current, {}, on=TODAY)}

    assert changes["B"].kind == UP and (changes["B"].was, changes["B"].now) == (2, 1)
    assert changes["B"].reason == "a win"
    # The fighter they passed did not fight; it was not their doing.
    assert changes["A"].kind == DOWN and changes["A"].reason == PUSHED


def test_sliding_after_a_loss_says_so():
    previous = {"a": ranked("a", 1, 1200, last=BEFORE)}
    current = [Row("a", 1, 1100, last=RECENT, result="loss")]
    # Same rank, so nothing is announced; the reason only matters when they move.
    assert diff(previous, current, {}, on=TODAY) == []

    current = [Row("b", 1, 1300, last=RECENT, result="win"), Row("a", 2, 1100, last=RECENT, result="loss")]
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
    previous = {"a": ranked("a", 3, 1200, last=BEFORE), "gone": ranked("gone", 5, 1100, last=OLD)}
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
        "a": RankedState("a", 1, 1100, date(2026, 1, 10), 1100),
        "b": RankedState("b", 2, 1098, date(2026, 1, 10), 1098),
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
        "winner": ranked("winner", 4, 1100, last=BEFORE),
        "beaten": ranked("beaten", 3, 1120, last=BEFORE),
        "drift_a": ranked("drift_a", 1, 1200, last=BEFORE),
        "drift_b": ranked("drift_b", 2, 1190, last=BEFORE),
    }
    current = [
        # These two swapped because the fade moved them, with no fight between.
        Row("drift_b", 1, 1188, last=BEFORE),
        Row("drift_a", 2, 1187, last=BEFORE),
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
        "winner": ranked("winner", 2, 1100, last=BEFORE),
        "passed": ranked("passed", 1, 1120, last=BEFORE),
    }
    current = [
        Row("winner", 1, 1150, last=RECENT, result="win"),
        Row("passed", 2, 1120, last=BEFORE),
    ]
    moved = {c.name: c for c in diff(previous, current, {}, on=TODAY)}

    assert moved["Passed"].kind == DOWN, "passed by somebody who fought"
    assert moved["Passed"].reason == PUSHED


def test_a_fighter_back_from_a_layoff_is_reported_by_result_not_direction():
    """The fade comes off the moment someone fights, so a returning fighter gets
    their layoff back and pays for the result out of it: carrying more than half
    of K they come back from a loss with a higher number than they left with, and
    the board moves them up. "Up to 4th" of a man who just lost is wrong, and
    saying nothing is worse -- that reports returning fighters' wins and not
    their losses. So the result is reported, and the place, and no direction."""
    previous = {
        # Shown at 1137 with 21 points of fade on it; 1158 underneath.
        "returning": ranked("returning", 5, 1137, last=OLD, raw=1158),
        "steady": ranked("steady", 4, 1140, last=BEFORE),
    }
    current = [
        Row("returning", 4, 1142, last=RECENT, result="loss"),  # fade back, minus the loss
        Row("steady", 5, 1140, last=BEFORE),
    ]
    moved = {c.name: c for c in diff(previous, current, {}, on=TODAY)}

    assert moved["Returning"].kind == RETURNED
    assert moved["Returning"].now == 4 and moved["Returning"].reason == "a loss"
    assert "Steady" not in moved, "passed by a layoff coming off, not by a result"


def test_a_fighter_who_wins_and_climbs_is_still_announced():
    previous = {"a": ranked("a", 2, 1100, last=BEFORE), "b": ranked("b", 1, 1120, last=BEFORE)}
    current = [
        Row("a", 1, 1150, last=RECENT, result="win"),
        Row("b", 2, 1120, last=BEFORE),
    ]
    moved = {c.name: c for c in diff(previous, current, {}, on=TODAY)}

    assert moved["A"].kind == UP and moved["A"].reason == "a win"


def test_a_return_reads_as_a_place_rather_than_a_climb():
    """The whole line, not a pattern in it.

    The comeback event carries the rank the fighter came from, because the diff
    needs it to know he moved at all, and only the renderer keeps it off the
    board. Asserting the absence of a pattern -- no arrow, no "**10" -- guards
    nothing the moment the format changes: drop the bold, write "=10", use "->"
    instead of an arrow, and the assertions still pass while a from-rank is back
    on the board. Against the whole line a deliberate format change fails here
    and has to be looked at, which is what this is for.
    """
    previous = {
        "rda": ranked("rda", 10, 1042, last=OLD, raw=1080),   # 38 points of fade
        "aldo": ranked("aldo", 9, 1065, last=BEFORE),
    }
    current = [
        Row("rda", 9, 1064, last=RECENT, result="loss", name="Rafael Dos Anjos"),
        Row("aldo", 10, 1065, last=BEFORE, name="Jose Aldo"),
    ]
    value = ratings_changes_embed("Lightweight", diff(previous, current, {}, on=TODAY)).fields[0].value

    # The non-breaking spaces are part of the output, not noise to be normalised
    # away: keep() puts them inside each fact so a narrow screen cannot wrap
    # "back, now" onto two lines, while the " · " between facts stays breakable.
    # Replacing them here would let a change that drops them pass while the line
    # falls apart on a phone.
    assert value == (
        "↩️ Rafael Dos Anjos · back, now **9**"
        " · after a loss · 1064"
    )


def test_a_returning_winner_only_passes_the_people_the_win_passed():
    """The direction agrees with the win, so a check on direction lets this
    through. Poirier coming back and winning is 21 points of fade plus 16 of
    result, and everyone he crosses inside the first 21 was passed by the layoff
    ending. Only the ones the win took him past have anything to report."""
    previous = {
        # 1158 underneath, shown at 1137 with 21 points of fade.
        "returning": ranked("returning", 6, 1137, last=OLD, raw=1158),
        "already_below": ranked("already_below", 5, 1150, last=BEFORE),   # under him in raw
        "genuinely_passed": ranked("genuinely_passed", 4, 1165, last=BEFORE),
    }
    current = [
        Row("returning", 4, 1174, last=RECENT, result="win"),   # fade back, plus the win
        Row("genuinely_passed", 5, 1165, last=BEFORE),
        Row("already_below", 6, 1150, last=BEFORE),
    ]
    moved = {c.name: c for c in diff(previous, current, {}, on=TODAY)}

    assert moved["Returning"].kind == RETURNED
    assert "Already_Below" not in moved, "he was above them before the fight; the fade hid it"
    assert moved["Genuinely_Passed"].kind == DOWN, "the win actually took him past this one"


def test_a_fixture_cannot_claim_a_fight_without_moving_the_date():
    """The helper exists because several tests here once did exactly that, and
    passed while testing nothing. The date is the card's, so the fighter is as
    recently active as the fixture says they are."""
    import pytest

    was = ranked("a", 3, 1100, last=BEFORE)
    card = TODAY - timedelta(days=2)

    assert fought_on(was, card, 2, 1130, result="win").last_fight == card
    with pytest.raises(AssertionError):
        fought_on(was, was.last_fight - timedelta(days=1), 2, 1130, result="win")


def test_a_crossing_nobody_can_explain_is_not_announced():
    """A row written before the raw rating was kept cannot say whether a
    returning fighter passed somebody on his result or on his layoff coming off.
    Falling back to the ranks the board showed would be reading the fade as the
    answer, which is the thing the raw rating exists to avoid."""
    previous = {
        "returner": RankedState("returner", 6, 1137, OLD, None),   # pre-migration row
        "bystander": ranked("bystander", 5, 1150, last=BEFORE),
    }
    current = [
        Row("returner", 5, 1174, last=RECENT, result="win"),
        Row("bystander", 6, 1150, last=BEFORE),
    ]
    moved = {c.name: c for c in diff(previous, current, {}, on=TODAY)}

    assert moved["Returner"].kind == RETURNED, "his own comeback is still reported"
    assert "Bystander" not in moved, "no way to tell a result from a fade, so nothing is said"
