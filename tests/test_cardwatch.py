"""Spotting what changed on a card between two readings.

A false positive here announces a fight as cancelled when it is not, and voids
picks that cannot be un-voided, so the cases about a card that only half-loaded
matter as much as the ones about real changes.
"""

from __future__ import annotations

from conftest import bout, card, fighter, unnamed_bout

from ufcbot.features.cardwatch import ADDED, REMOVED, REPLACED, _bout_state, diff
from ufcbot.records import CardBout

ALLEN = fighter("1", "Arnold Allen")
PICO = fighter("2", "Aaron Pico")
IGE = fighter("3", "Dan Ige")
MOICANO = fighter("4", "Renato Moicano")
ORTEGA = fighter("5", "Brian Ortega")
VOLK = fighter("6", "Alexander Volkanovski")
EVLOEV = fighter("7", "Movsar Evloev")


def before(soon):
    return card(
        bout("B1", ALLEN, PICO, weight="Featherweight"),
        bout("B2", MOICANO, ORTEGA, match=2),
        bout("B3", VOLK, EVLOEV, match=3, weight="Featherweight"),
        start=soon,
    )


def remembered(soon) -> dict[str, CardBout]:
    return {state.bout_id: state for state in _bout_state(before(soon))}


def kinds(changes) -> list[str]:
    return sorted(change.kind for change in changes)


def test_a_card_that_has_not_moved_reports_nothing(soon):
    assert diff(remembered(soon), _bout_state(before(soon))) == []


def test_a_fight_coming_off_is_reported_with_both_names(soon):
    after = card(bout("B1", ALLEN, PICO, weight="Featherweight"), start=soon)
    changes = [c for c in diff(remembered(soon), _bout_state(after)) if c.kind == REMOVED]
    matchups = {change.matchup for change in changes}
    assert "Renato Moicano vs. Brian Ortega" in matchups


def test_a_replacement_names_who_left_and_who_arrived(soon):
    after = card(
        bout("B1", ALLEN, IGE, weight="Featherweight"),
        bout("B2", MOICANO, ORTEGA, match=2),
        bout("B3", VOLK, EVLOEV, match=3, weight="Featherweight"),
        start=soon,
    )
    change = next(c for c in diff(remembered(soon), _bout_state(after)) if c.kind == REPLACED)
    assert (change.left, change.arrived, change.opponent) == ("Aaron Pico", "Dan Ige", "Arnold Allen")


def test_a_late_addition_is_reported(soon):
    after = card(*before(soon).bouts, bout("B4", fighter("8", "A"), fighter("9", "B"), match=4), start=soon)
    assert kinds(diff(remembered(soon), _bout_state(after))) == [ADDED]


def test_both_fighters_changing_reads_as_one_fight_off_and_one_on(soon):
    """That is a different fight in the same slot, not a replacement."""
    after = card(
        bout("B1", ALLEN, PICO, weight="Featherweight"),
        bout("B2", fighter("30", "X"), fighter("31", "Y"), match=2),
        bout("B3", VOLK, EVLOEV, match=3, weight="Featherweight"),
        start=soon,
    )
    assert kinds(diff(remembered(soon), _bout_state(after))) == [ADDED, REMOVED]


def test_a_bout_whose_names_did_not_load_is_not_a_cancellation(soon):
    after = card(
        bout("B1", ALLEN, PICO, weight="Featherweight"),
        unnamed_bout("B2"),
        bout("B3", VOLK, EVLOEV, match=3, weight="Featherweight"),
        start=soon,
    )
    unresolved = {b.id for b in after.bouts if b.awaiting_names}
    assert diff(remembered(soon), _bout_state(after), unresolved) == []


async def test_a_card_is_remembered_and_read_back(storage, soon):
    await storage.save_card_bouts("EV1", _bout_state(before(soon)))
    stored = await storage.card_bouts("EV1")

    assert set(stored) == {"B1", "B2", "B3"}
    assert stored["B2"].matchup == "Renato Moicano vs. Brian Ortega"
    assert stored["B2"].athletes == {"4", "5"}


async def test_saving_a_card_replaces_what_was_there(storage, soon):
    await storage.save_card_bouts("EV1", _bout_state(before(soon)))
    await storage.save_card_bouts("EV1", _bout_state(card(bout("B1", ALLEN, PICO), start=soon)))
    assert set(await storage.card_bouts("EV1")) == {"B1"}


# -- placeholders are not fights ---------------------------------------------------


def test_an_empty_slot_leaving_the_card_is_not_a_fight_coming_off():
    """ESPN carries unannounced bouts as placeholders with nobody in them and
    swaps them for the real fight. "TBA vs. TBA is off the card" is noise."""
    previous = {"B1": CardBout(bout_id="B1", fighters=())}
    current = [CardBout(bout_id="B2", fighters=(("1", "Arnold Allen"), ("2", "Aaron Pico")))]

    kinds = [change.kind for change in diff(previous, current)]

    assert kinds == ["added"], "the fight arriving is the whole story"


def test_a_real_fight_coming_off_is_still_reported():
    previous = {"B1": CardBout(bout_id="B1", fighters=(("1", "Arnold Allen"), ("2", "Aaron Pico")))}

    assert [change.kind for change in diff(previous, [])] == ["removed"]


def test_a_card_with_nobody_named_is_not_a_card_yet(soon):
    """A date and a row of empty slots is not something to put on a calendar."""


    assert not card(unnamed_bout("B1"), unnamed_bout("B2"), start=soon).has_anyone_named
    assert card(bout("B1", fighter("1", "A"), fighter("2", "B")), start=soon).has_anyone_named
