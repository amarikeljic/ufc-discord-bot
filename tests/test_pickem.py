"""Pick'em scoring, and what a changed card does to picks people have made.

Members' points ride on this, so the rule that matters most is the one about a
fight that never happened scoring nothing rather than counting as a loss.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from conftest import bout, card, fighter, pickem_record, unnamed_bout

from ufcbot.features.pickem import (
    NO_ODDS,
    OPEN,
    WRONG_PICK_POINTS,
    PickemService,
    benchmarks,
    bout_status,
    points_for,
)
from ufcbot.records import PredictionRecord
from ufcbot.ui.pickem import PAGE_SIZE, PickerView

MOICANO = fighter("20", "Renato Moicano")
ORTEGA = fighter("21", "Brian Ortega")
TSARUKYAN = fighter("22", "Arman Tsarukyan")
RUFFY = fighter("23", "Mauricio Ruffy")
REPLACEMENT = fighter("24", "Dan Hooker")


@pytest.mark.parametrize(
    ("odds", "expected"),
    [(235, 235), (110, 110), (-110, 91), (-290, 34), (-1800, 6), (100, 100), (-100, 100)],
)
def test_points_follow_the_odds(odds, expected):
    assert points_for(odds) == expected


def test_a_price_of_zero_does_not_divide_by_zero():
    """No book prices a fight at zero, but a bad feed can say so, and this runs
    over every stored pick at startup."""
    assert points_for(0) == 100


async def picks_for(storage, users, bout_id="M1", locks_at=None):
    for user, athlete, opponent in users:
        await storage.save_pickem_pick(
            pickem_record(
                user_id=user,
                bout_id=bout_id,
                athlete_id=athlete,
                opponent_id=opponent,
                locks_at=locks_at or datetime.now(UTC) - timedelta(hours=3),
            )
        )


async def results_by_user(storage, users):
    out = {}
    for user in users:
        summary = await storage.pickem_user_summary(1, user)
        out[user] = (summary.wins, summary.losses, summary.voids, summary.points)
    return out


async def test_a_pick_on_the_winner_scores(storage):
    await picks_for(storage, [(10, "22", "23")])
    await storage.grade_pickem_bout("M1", "22", loss_points=WRONG_PICK_POINTS, fighters={"22", "23"})
    assert (await results_by_user(storage, [10]))[10] == (1, 0, 0, 44)


async def test_a_pick_on_the_loser_costs(storage):
    await picks_for(storage, [(10, "22", "23")])
    await storage.grade_pickem_bout("M1", "23", loss_points=WRONG_PICK_POINTS, fighters={"22", "23"})
    assert (await results_by_user(storage, [10]))[10] == (0, 1, 0, WRONG_PICK_POINTS)


async def test_a_pick_on_a_replaced_fighter_is_void_not_a_loss(storage):
    """Moicano is picked, withdraws, and his opponent beats the replacement. The
    fight that was picked never happened."""
    await picks_for(storage, [(10, "20", "21"), (11, "21", "20")])

    await storage.grade_pickem_bout("M1", "21", loss_points=WRONG_PICK_POINTS, fighters={"21", "24"})

    scored = await results_by_user(storage, [10, 11])
    assert scored[10] == (0, 0, 1, 0), "the pick on the withdrawn fighter"
    assert scored[11] == (1, 0, 0, 44), "the pick on the fighter who fought and won"


async def test_a_bout_with_no_winner_is_void(storage):
    await picks_for(storage, [(10, "22", "23")])
    await storage.grade_pickem_bout("M1", None, loss_points=WRONG_PICK_POINTS)
    assert (await results_by_user(storage, [10]))[10] == (0, 0, 1, 0)


# -- fights that leave the card before they lock ------------------------------


def service(storage) -> PickemService:
    return PickemService(data=None, storage=storage)


async def test_a_fight_off_the_card_is_voided_before_it_locks(storage, soon):
    """Grading only ever looks at bouts that have locked, so without this a
    cancelled fight sits in everyone's picks until days after the card."""
    await picks_for(storage, [(30, "20", "21")], locks_at=soon)
    await picks_for(storage, [(31, "22", "23")], bout_id="M2", locks_at=soon)

    voided = await service(storage).retire_missing(
        card(bout("M2", TSARUKYAN, RUFFY, match=2), start=soon)
    )

    assert voided == 1
    scored = await results_by_user(storage, [30, 31])
    assert scored[30] == (0, 0, 1, 0), "the cancelled fight"
    assert (await storage.pickem_user_summary(1, 31)).pending == 1, "the other fight is untouched"


async def test_an_intact_card_voids_nothing(storage, soon):
    await picks_for(storage, [(30, "20", "21")], locks_at=soon)
    full = card(bout("M1", MOICANO, ORTEGA), bout("M2", TSARUKYAN, RUFFY, match=2), start=soon)
    assert await service(storage).retire_missing(full) == 0


async def test_a_half_loaded_card_voids_nothing(storage, soon):
    """Voiding cannot be taken back, so it waits for a card that read cleanly."""
    await picks_for(storage, [(30, "20", "21")], locks_at=soon)
    half = card(bout("M2", TSARUKYAN, RUFFY, match=2), start=soon, partial=True)
    assert await service(storage).retire_missing(half) == 0


async def test_a_bout_whose_names_did_not_load_voids_nothing(storage, soon):
    await picks_for(storage, [(30, "20", "21")], locks_at=soon)
    unresolved = card(unnamed_bout("M1"), bout("M2", TSARUKYAN, RUFFY, match=2), start=soon)
    assert await service(storage).retire_missing(unresolved) == 0


async def test_only_the_pick_on_the_replaced_fighter_is_voided(storage, soon):
    """The fighter who stayed is still fighting, so that pick stays live and can
    still be changed at the new price."""
    await picks_for(storage, [(30, "20", "21"), (31, "21", "20")], locks_at=soon)

    await service(storage).retire_missing(card(bout("M1", ORTEGA, REPLACEMENT), start=soon))

    assert (await storage.pickem_user_summary(1, 30)).voids == 1
    assert (await storage.pickem_user_summary(1, 31)).pending == 1


async def test_void_picks_are_left_out_of_the_crowd_split(storage, soon):
    """Otherwise the board shows a share of the room behind someone who is not
    in the fight."""
    await picks_for(storage, [(30, "20", "21"), (31, "21", "20")], locks_at=soon)
    await service(storage).retire_missing(card(bout("M1", ORTEGA, REPLACEMENT), start=soon))

    counts = await storage.pickem_counts(1, "EV1")
    assert counts.get("M1") == {"21": 1}


# -- which fights can be picked -------------------------------------------------


def test_a_fight_with_no_price_is_the_only_thing_held_back(soon):
    """The board no longer waits for the whole card to be priced. An unpriced
    fight is shown and cannot be picked; its neighbours are unaffected."""
    a, b, c, d = (fighter(f"F{i}", f"Fighter {i}") for i in range(4))
    one, two = bout("B1", a, b), bout("B2", c, d)
    event = card(one, two, start=soon)
    now = soon - timedelta(days=10)

    assert [bout_status(event, f, now) for f in event.fights] == [NO_ODDS, NO_ODDS]

    one.odds = {a.id: -150, b.id: 130}
    assert [bout_status(event, f, now) for f in event.fights] == [OPEN, NO_ODDS]

    two.odds = {c.id: -110}
    assert bout_status(event, two, now) == NO_ODDS, "one corner priced is not a fight anyone can pick"

    two.odds[d.id] = -110
    assert [bout_status(event, f, now) for f in event.fights] == [OPEN, OPEN]


def test_a_fight_with_no_second_name_is_not_on_the_board_at_all(soon):
    """It is a line on ESPN's card with nobody in it yet, so there is nothing to
    show and nothing to pick."""
    event = card(unnamed_bout("B1"), start=soon)
    assert event.fights == []
    assert bout_status(event, event.bouts[0], soon - timedelta(days=1)) == NO_ODDS


# -- what the model and the market scored on the same fights ---------------------


def graded_prediction(bout_id: str, *, prob_a: float, odds_a: int, odds_b: int, winner: str) -> PredictionRecord:
    return PredictionRecord(
        espn_event_id="EV1", bout_id=bout_id, event_name="UFC 333",
        event_start=datetime.now(UTC), athlete_a="A", name_a="Fighter A",
        athlete_b="B", name_b="Fighter B", prob_a=prob_a, weight_class="Lightweight",
        position=0, odds_a=odds_a, odds_b=odds_b, winner_athlete=winner,
        correct=(winner == ("A" if prob_a >= 0.5 else "B")),
    )


def test_the_model_and_the_market_are_scored_the_way_a_member_is():
    # The model likes A, the book has B shorter, and B wins.
    records = [graded_prediction("B1", prob_a=0.7, odds_a=200, odds_b=-250, winner="B")]

    model, market = benchmarks(records)

    assert (model.name, model.wins, model.losses) == ("🤖 The bot", 0, 1)
    assert model.points == WRONG_PICK_POINTS
    assert (market.wins, market.losses) == (1, 0)
    assert market.points == points_for(-250), "paid at the price it backed"


def test_a_fight_nobody_could_have_picked_is_not_scored():
    """Pick'em only opens fights with both prices, and a draw is void for
    everyone, so counting either would compare different sets of fights."""
    unpriced = PredictionRecord(
        espn_event_id="EV1", bout_id="B1", event_name="UFC 333", event_start=datetime.now(UTC),
        athlete_a="A", name_a="A", athlete_b="B", name_b="B", prob_a=0.7,
        weight_class="Lightweight", position=0, winner_athlete="A", correct=True,
    )
    drawn = graded_prediction("B2", prob_a=0.7, odds_a=-150, odds_b=130, winner="A")
    drawn.winner_athlete = None

    assert benchmarks([unpriced, drawn]) == []


def test_nothing_is_claimed_before_a_card_is_graded():
    assert benchmarks([]) == []


# -- the two ways of being good at this ------------------------------------------


def test_the_leaderboard_by_record_is_not_the_leaderboard_by_points():
    """Backing every favourite wins often and loses points; backing underdogs
    does the reverse. One table hides whichever half a player is good at."""
    from ufcbot.embeds.pickem import by_record
    from ufcbot.records import PickemStanding

    safe = PickemStanding(user_id=1, points=120, wins=12, losses=5, cards=2)
    bold = PickemStanding(user_id=2, points=1774, wins=6, losses=6, cards=2)

    assert by_record([bold, safe])[0] is safe, "most fights called right"
    assert sorted([safe, bold], key=lambda s: -s.points)[0] is bold, "most points"


def test_a_tie_on_record_is_broken_by_the_fewest_losses_then_points():
    from ufcbot.embeds.pickem import by_record
    from ufcbot.records import PickemStanding

    fewer_losses = PickemStanding(user_id=1, points=10, wins=8, losses=1, cards=1)
    more_losses = PickemStanding(user_id=2, points=900, wins=8, losses=6, cards=1)

    assert by_record([more_losses, fewer_losses])[0] is fewer_losses


def test_the_win_rate_board_makes_a_player_play_before_it_ranks_them():
    """One settled pick at 100% is not the best record in the server. Qualifying
    takes half as many settled picks as the busiest player on the board."""
    from ufcbot.embeds.pickem import by_win_rate
    from ufcbot.records import PickemStanding

    busy = PickemStanding(user_id=1, points=400, wins=14, losses=9, cards=3)   # 61%
    steady = PickemStanding(user_id=2, points=90, wins=8, losses=8, cards=2)   # 50%
    lucky = PickemStanding(user_id=3, points=300, wins=2, losses=0, cards=1)   # 100%

    order = by_win_rate([lucky, steady, busy])

    assert order[0] is busy, "the best rate among players who have played"
    assert order[-1] is lucky, "below the line, however good the percentage looks"


def test_the_win_rate_board_ranks_a_better_percentage_above_a_bigger_record():
    """This is the table that is not 'by score': fewer fights called right can
    still be the better share of them."""
    from ufcbot.embeds.pickem import by_record, by_win_rate
    from ufcbot.records import PickemStanding

    accurate = PickemStanding(user_id=1, points=50, wins=9, losses=3, cards=2)    # 75%
    prolific = PickemStanding(user_id=2, points=50, wins=12, losses=11, cards=2)  # 52%

    assert by_win_rate([prolific, accurate])[0] is accurate
    assert by_record([accurate, prolific])[0] is prolific, "the two tables disagree, which is the point"


# -- clicking faster than the view can redraw --------------------------------


class SlowStorage:
    """Storage whose reads take long enough for a second click to arrive."""

    def __init__(self, delay=0.02):
        self.delay = delay

    async def pickem_user_picks(self, guild_id, user_id, event_id):
        import asyncio

        await asyncio.sleep(self.delay)
        return []


class Recorder:
    """Stands in for the interaction, remembering what the view looked like."""

    def __init__(self, seen):
        self.seen = seen
        self.response = self

    async def defer(self):
        return None

    async def edit_original_response(self, *, embed, view):
        self.seen.append(len(view.children))

    async def edit_message(self, *, embed, view):
        self.seen.append(len(view.children))


async def test_the_components_keep_their_names_across_a_redraw():
    """The view is rebuilt from scratch after every pick and every page turn.
    Left to itself discord.py gives each menu and button a fresh random
    custom_id every time, and it looks interactions up by that id -- so a click
    made while the previous redraw was still in flight arrived naming a
    component the view had just thrown away, and was discarded without a word.
    That is what picking or paging too quickly looked like from the outside."""
    a, b = fighter("1", "Arnold Allen"), fighter("2", "Aaron Pico")
    start = datetime.now(UTC) + timedelta(days=5)
    bouts = [bout(f"B{i}", a, b, match=i) for i in range(1, PAGE_SIZE * 3 + 1)]
    event = card(*bouts, start=start)

    class Service:
        storage = SlowStorage(delay=0)

        def now(self):
            return datetime.now(UTC)

    view = PickerView(Service(), event, user_id=1, guild_id=1)
    await view.refresh()
    first = [item.custom_id for item in view.children]
    await view.refresh()
    again = [item.custom_id for item in view.children]

    assert first == again, "a redraw renamed the components out from under the user"
    assert len(set(first)) == len(first), "two components cannot share a name"
    assert any(name == "pickem:next" for name in first)


async def test_paging_faster_than_the_redraw_still_leaves_a_whole_view():
    """Every callback rebuilds the view in place -- clear_items() and then add
    them back -- while awaiting a database read. Two clicks inside that window
    used to interleave, so one coroutine emptied the components while another
    was halfway through putting them back, and the message went out short of
    its menus."""
    import asyncio


    a, b = fighter("1", "Arnold Allen"), fighter("2", "Aaron Pico")
    start = datetime.now(UTC) + timedelta(days=5)
    bouts = [bout(f"B{i}", a, b, match=i) for i in range(1, PAGE_SIZE * 3 + 1)]
    event = card(*bouts, start=start)

    class Service:
        storage = SlowStorage()

        def now(self):
            return datetime.now(UTC)

    view = PickerView(Service(), event, user_id=1, guild_id=1)
    assert view.pages > 1, "the fixture needs more than one page to have buttons"

    seen: list[int] = []
    await asyncio.gather(
        view._turn(Recorder(seen), 1),
        view._turn(Recorder(seen), 1),
        view._turn(Recorder(seen), -1),
    )

    assert len(seen) == 3, "every click answered"
    # A page always carries its menus; a half-rebuilt view carries fewer.
    assert all(count >= PAGE_SIZE for count in seen), f"a view went out short: {seen}"
    assert 0 <= view.page < view.pages


# -- what a finished fight looks like on the board ----------------------------


def test_a_finished_fight_is_not_marked_with_a_tick():
    """The field is titled "A vs. B" and its value starts hard against the left,
    so a tick there reads as marking the first name rather than the one it is
    actually in front of -- which is the other man half the time."""
    from ufcbot.embeds.pickem import pickem_board_embed

    start = datetime(2026, 5, 1, 22, 0, tzinfo=UTC)
    finished = bout("B1", MOICANO, ORTEGA)
    finished.completed = True
    finished.winner_id = ORTEGA.id
    event = card(finished, start=start)

    embed = pickem_board_embed(event, {}, players=1, now=start + timedelta(hours=4))
    value = embed.fields[0].value

    assert "✅" not in value, "a tick beside one of two names is a claim about both"
    # keep() joins a name with a non-breaking space so it cannot wrap.
    assert "Ortega" in value and "won" in value
    assert "Moicano" not in value, "the loser is not named by the winner's line"


# -- which card /ufc pickem picks shows when nobody says ----------------------


class Cards:
    """Stands in for ESPN: cards by start time, resolved on demand."""

    def __init__(self, *events):
        self.events = list(events)

    async def recent_events(self, *, days, limit):
        return [e for e in self.events][:limit]

    async def upcoming_events(self, *, days, limit):
        return [e for e in self.events][:limit]

    async def get_event(self, event_id, **kwargs):
        return next((e for e in self.events if e.id == event_id), None)

    async def load_odds(self, event, **kwargs):
        return None


def at(hours: float):
    return datetime(2026, 5, 2, 0, 0, tzinfo=UTC) + timedelta(hours=hours)


def a_card(event_id: str, start, *, done: bool = False):
    fight = bout(f"{event_id}-B1", MOICANO, ORTEGA)
    if done:
        fight.completed = True
        fight.winner_id = ORTEGA.id
    event = card(fight, start=start)
    event.id = event_id
    event.name = f"UFC {event_id}"
    return event


async def test_the_card_being_fought_is_the_one_shown(storage):
    """A card under way is what anybody asking means."""
    live = a_card("300", at(-2))
    svc = PickemService(data=Cards(live), storage=storage, clock=lambda: at(0))

    assert (await svc.latest_event()).id == "300"


async def test_between_cards_it_shows_the_one_that_happened(storage):
    """Not the one pick'em is now taking picks for. The board turns over the
    moment a card ends, so current_event points at a card nobody has picked --
    right for a board asking for picks, wrong for a command showing them."""
    finished = a_card("300", at(-30), done=True)
    nextup = a_card("301", at(100))
    svc = PickemService(data=Cards(finished, nextup), storage=storage, clock=lambda: at(0))

    shown = await svc.latest_event()

    assert shown.id == "300", "the night everybody just watched"


async def test_contender_series_is_never_the_answer(storage):
    """No pick was ever taken on one, so it would show an empty card."""
    dwcs = a_card("900", at(-10), done=True)
    dwcs.name = "Dana White's Contender Series: Week 5"
    proper = a_card("300", at(-40), done=True)
    svc = PickemService(data=Cards(dwcs, proper), storage=storage, clock=lambda: at(0))

    assert (await svc.latest_event()).id == "300"
