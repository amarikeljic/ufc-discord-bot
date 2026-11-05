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


# -- a card that will not hold still -------------------------------------------


class Feed:
    """ESPN, serving whichever reading of the card it was last handed."""

    def __init__(self, event):
        self.event = event
        self.reads = 0

    async def upcoming_events(self, *, days, limit):
        return [self.event]

    async def get_event(self, event_id, **kwargs):
        self.reads += 1
        return self.event


BRENNAN = fighter("10", "Lucas Brennan")
CALDERON = fighter("11", "Carlos Calderon")
BASHI = fighter("12", "Austin Bashi")


def against(opponent, soon):
    """The one-bout card, with Bashi facing whoever upstream says today."""
    return card(bout("B9", BASHI, opponent, weight="Featherweight"), start=soon)


async def test_a_card_that_changes_back_is_never_announced(storage, soon):
    """Upstream flips a bout between two readings and back again within the
    hour: Calderon replaces Brennan at five, Brennan replaces Calderon at six.
    Both were announced, and neither had happened."""
    from ufcbot.features.cardwatch import CardWatch

    feed = Feed(against(BRENNAN, soon))
    watch = CardWatch(feed, storage)

    assert await watch.poll() == [], "first sight of a card is not news"
    feed.event = against(CALDERON, soon)
    assert await watch.poll() == [], "not believed on one reading"
    feed.event = against(BRENNAN, soon)
    assert await watch.poll() == [], "and it was never true"

    remembered = await storage.card_bouts("EV1")
    assert remembered["B9"].athletes == {"12", "10"}, "Brennan was never written off"


async def test_a_change_that_holds_is_announced(storage, soon):
    """The cost of the above: a real replacement is reported one pass later
    than it used to be, once upstream has said it twice."""
    from ufcbot.features.cardwatch import CardWatch

    feed = Feed(against(BRENNAN, soon))
    watch = CardWatch(feed, storage)
    await watch.poll()

    feed.event = against(CALDERON, soon)
    assert await watch.poll() == [], "held back on the first reading"

    found = await watch.poll()

    assert len(found) == 1
    [(_event, changes)] = found
    assert kinds(changes) == [REPLACED]
    assert changes[0].left == "Lucas Brennan"
    assert changes[0].arrived == "Carlos Calderon"


async def test_a_confirmed_change_is_announced_once(storage, soon):
    """And not again on every pass after it, which is what writing the reading
    down only when it settles is for."""
    from ufcbot.features.cardwatch import CardWatch

    feed = Feed(against(BRENNAN, soon))
    watch = CardWatch(feed, storage)
    await watch.poll()
    feed.event = against(CALDERON, soon)
    await watch.poll()
    assert await watch.poll(), "announced here"

    assert await watch.poll() == [], "and not again"
    assert await watch.poll() == []
