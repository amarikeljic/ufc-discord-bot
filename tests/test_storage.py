"""The database, where a bug is silent.

Nothing here reaches the network. What is worth pinning down is the bookkeeping
the rest of the bot trusts without checking: that a board it remembers is the
board it edits, that a snapshot written twice leaves one reading rather than
two, and that rows meant to be swept are the only ones swept.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from ufcbot.records import CardBout, PredictionRecord, RankedState, ShortNotice

NOW = datetime(2026, 9, 21, 20, 0, tzinfo=UTC)


def prediction(bout_id: str, *, event: str = "EV1", start: datetime = NOW) -> PredictionRecord:
    return PredictionRecord(
        espn_event_id=event, bout_id=bout_id, event_name="UFC 333", event_start=start,
        athlete_a="A", name_a="Fighter A", athlete_b="B", name_b="Fighter B",
        prob_a=0.62, weight_class="Lightweight", position=0, odds_a=-160, odds_b=135,
    )


# -- the boards the bot remembers -------------------------------------------------


async def test_a_board_is_found_by_the_kind_and_key_it_was_saved_under(storage):
    await storage.save_post(1, "picks", "board", channel_id=50, message_id=900, signature="abc")

    post = await storage.get_post(1, "picks", "board")

    assert (post.channel_id, post.message_id, post.signature) == (50, 900, "abc")
    assert await storage.get_post(1, "picks", "other") is None
    assert await storage.get_post(2, "picks", "board") is None, "another server's board is not this one's"


async def test_saving_a_board_twice_replaces_it_rather_than_duplicating_it(storage):
    """Otherwise the bot would edit one message and leave a second behind."""
    await storage.save_post(1, "picks", "board", channel_id=50, message_id=900, signature="abc")
    await storage.save_post(1, "picks", "board", channel_id=50, message_id=901, signature="def")

    assert (await storage.get_post(1, "picks", "board")).message_id == 901
    assert len(await storage.posts_of_kind(1, "picks")) == 1


async def test_a_deleted_board_is_forgotten(storage):
    await storage.save_post(1, "picks", "board", channel_id=50, message_id=900, signature="abc")
    await storage.delete_post(1, "picks", "board")

    assert await storage.get_post(1, "picks", "board") is None


async def test_boards_of_one_kind_are_listed_without_the_others(storage):
    """The pick'em publisher walks its own kind to find the message left over
    from the last stage; picking up the leaderboard would delete it."""
    await storage.save_post(1, "pickem", "last:OLD", channel_id=50, message_id=900, signature="a")
    await storage.save_post(1, "pickem", "board:NEW", channel_id=50, message_id=901, signature="b")
    await storage.save_post(1, "pickem_leaderboard", "board", channel_id=50, message_id=902, signature="c")

    assert set(await storage.posts_of_kind(1, "pickem")) == {"last:OLD", "board:NEW"}


# -- predictions ------------------------------------------------------------------


async def test_a_card_of_picks_is_written_and_read_back_in_order(storage):
    await storage.upsert_predictions([prediction("B2"), prediction("B1")])

    records = await storage.predictions_for_event("EV1")

    assert {r.bout_id for r in records} == {"B1", "B2"}
    assert await storage.prediction_for_bout("B1") is not None


async def test_re_recording_a_card_updates_its_picks_rather_than_adding_more(storage):
    """Picks are re-recorded every pass while a card is still days away, because
    the odds and the fighters move."""
    await storage.upsert_predictions([prediction("B1")])
    changed = prediction("B1")
    changed.prob_a = 0.71
    await storage.upsert_predictions([changed])

    records = await storage.predictions_for_event("EV1")

    assert len(records) == 1 and records[0].prob_a == 0.71


async def test_a_pick_for_a_fight_that_came_off_the_card_is_dropped(storage):
    await storage.upsert_predictions([prediction("B1"), prediction("B2")])

    await storage.delete_predictions("EV1", ["B2"])

    assert [r.bout_id for r in await storage.predictions_for_event("EV1")] == ["B1"]


async def test_only_graded_picks_count_towards_the_record(storage):
    await storage.upsert_predictions([prediction("B1"), prediction("B2")])
    await storage.grade_prediction("EV1", "B1", winner_athlete="A", correct=True)

    graded = await storage.graded_predictions()

    assert [r.bout_id for r in graded] == ["B1"]
    assert graded[0].correct is True and graded[0].winner_athlete == "A"
    assert "EV1" in await storage.events_with_ungraded_predictions()


async def test_a_draw_is_graded_without_a_winner(storage):
    """Nobody won, so the pick was neither right nor wrong; it must still count
    as graded or the card never finishes grading."""
    await storage.upsert_predictions([prediction("B1")])

    await storage.grade_prediction("EV1", "B1", winner_athlete=None, correct=None)

    assert await storage.events_with_ungraded_predictions() == set()
    assert (await storage.graded_predictions())[0].correct is None


async def test_the_record_can_be_read_from_a_date_onwards(storage):
    old = prediction("B1", event="OLD", start=NOW - timedelta(days=400))
    new = prediction("B2", event="NEW", start=NOW)
    await storage.upsert_predictions([old, new])
    for record in (old, new):
        await storage.grade_prediction(record.espn_event_id, record.bout_id, "A", True)

    recent = await storage.graded_predictions(since=(NOW - timedelta(days=30)).date())

    assert [r.bout_id for r in recent] == ["B2"]


# -- the ratings board as it was last published --------------------------------------


async def test_a_boards_last_reading_is_replaced_whole(storage):
    """Saving a board has to clear the fighters who dropped off it, or someone
    who left would be compared against for ever."""
    await storage.save_ranking_state(
        "Lightweight",
        [RankedState("a", 1, 1200, date(2026, 8, 1)), RankedState("b", 2, 1150, None)],
    )
    await storage.save_ranking_state("Lightweight", [RankedState("a", 1, 1210, date(2026, 9, 1))])

    state = await storage.ranking_state("Lightweight")

    assert set(state) == {"a"}
    assert state["a"].rating == 1210 and state["a"].last_fight == date(2026, 9, 1)


async def test_each_division_keeps_its_own_board(storage):
    await storage.save_ranking_state("Lightweight", [RankedState("a", 1, 1200, None)])
    await storage.save_ranking_state("Heavyweight", [RankedState("b", 1, 1300, None)])

    assert set(await storage.ranking_state("Lightweight")) == {"a"}
    assert set(await storage.ranking_state("Heavyweight")) == {"b"}


# -- the card as it last stood -------------------------------------------------------


async def test_a_cards_fights_are_remembered_and_replaced_whole(storage):
    await storage.save_card_bouts("EV1", [CardBout("B1", (("1", "A"), ("2", "B")), "Lightweight")])
    await storage.save_card_bouts("EV1", [CardBout("B1", (("1", "A"), ("3", "C")), "Lightweight")])

    bouts = await storage.card_bouts("EV1")

    assert set(bouts) == {"B1"}
    assert bouts["B1"].athletes == {"1", "3"}, "the replacement, not both readings"


async def test_only_cards_older_than_the_cutoff_are_swept(storage):
    await storage.save_card_bouts("OLD", [CardBout("B1", (("1", "A"), ("2", "B")))])
    await storage.save_card_bouts("NEW", [CardBout("B2", (("1", "A"), ("2", "B")))])

    # Age the first card by hand; the table stamps rows as it writes them.
    await storage.db.execute(
        "UPDATE card_bouts SET seen_at = ? WHERE espn_event_id = 'OLD'",
        ((NOW - timedelta(days=60)).isoformat(),),
    )
    await storage.db.commit()

    swept = await storage.prune_card_bouts(NOW - timedelta(days=21))

    assert swept == 1
    assert await storage.card_bouts("OLD") == {}
    assert set(await storage.card_bouts("NEW")) == {"B2"}


# -- live coverage bookkeeping ---------------------------------------------------------


async def test_what_has_been_posted_is_read_back_for_a_whole_card_at_once(storage):
    """One query per tick instead of one per fight; a restart mid-card relies on
    it to avoid posting the same round twice."""
    await storage.mark_live_posted("B1", "open")
    await storage.mark_live_posted("B1", "round:1")
    await storage.mark_live_posted("B2", "open")

    posted = await storage.live_posted_many(["B1", "B2", "B3"])

    assert posted["B1"] == {"open", "round:1"}
    assert posted["B2"] == {"open"}
    assert posted["B3"] == set(), "a fight with nothing posted still has an entry"


async def test_marking_the_same_post_twice_is_harmless(storage):
    await storage.mark_live_posted("B1", "open")
    await storage.mark_live_posted("B1", "open")

    assert (await storage.live_posted_many(["B1"]))["B1"] == {"open"}


# -- things said once ------------------------------------------------------------------


async def test_a_notice_is_due_once_and_then_not_again(storage):
    """A warning that repeats every hour is a warning nobody reads."""
    assert await storage.notice_due("stale_data", "2026-09-12") is True
    assert await storage.notice_due("stale_data", "2026-09-12") is False
    assert await storage.notice_due("stale_data", "2026-09-19") is True, "a new subject is new news"


# -- who stepped in, and how little warning they had -------------------------------------


def replacement(bout_id: str, *, arrived: str, days: float) -> ShortNotice:
    return ShortNotice(
        espn_event_id="EV1", bout_id=bout_id, arrived=arrived, departed="Someone Else",
        opponent="Arnold Allen", weight_class="Featherweight",
        event_start=NOW, noticed_at=NOW - timedelta(days=days), days_notice=days,
    )


async def test_a_replacement_is_recorded_with_how_much_warning_it_had(storage):
    await storage.record_short_notice([replacement("B1", arrived="Dan Ige", days=6.0)])

    rows = await storage.short_notice_for("EV1")

    assert len(rows) == 1
    assert (rows[0].arrived, rows[0].days_notice) == ("Dan Ige", 6.0)
    assert rows[0].opponent == "Arnold Allen"


async def test_the_first_sighting_is_the_one_kept(storage):
    """The card is re-read every hour. Rewriting the row each time would record
    the notice as shrinking towards zero as the card approached."""
    await storage.record_short_notice([replacement("B1", arrived="Dan Ige", days=6.0)])
    await storage.record_short_notice([replacement("B1", arrived="Dan Ige", days=2.0)])

    rows = await storage.short_notice_for("EV1")

    assert len(rows) == 1 and rows[0].days_notice == 6.0


async def test_replacements_come_back_shortest_notice_first(storage):
    await storage.record_short_notice(
        [replacement("B1", arrived="Late", days=1.0), replacement("B2", arrived="Early", days=20.0)]
    )

    assert [row.arrived for row in await storage.short_notice_for("EV1")] == ["Late", "Early"]


async def test_a_card_with_no_replacements_has_none(storage):
    assert await storage.short_notice_for("EV1") == []


# -- who to nudge on fight day ------------------------------------------------------


async def pick_on(storage, *, user: int, event: str, bout: str) -> None:
    from ufcbot.records import PickemRecord

    await storage.save_pickem_pick(
        PickemRecord(
            guild_id=1, user_id=user, espn_event_id=event, bout_id=bout, event_name="UFC 333",
            event_start=NOW, athlete_id="A", athlete_name="A", opponent_id="B", opponent_name="B",
            odds=-150, points_if_right=67, locks_at=NOW, picked_at=NOW,
        )
    )


async def test_players_missing_one_fight_are_the_ones_it_changed_under(storage):
    """A pick made last week was made against a fighter who may not be in the
    bout any more, so the people to tell are the ones with no pick on it."""
    await pick_on(storage, user=10, event="EV1", bout="B1")
    await pick_on(storage, user=10, event="EV1", bout="B2")
    await pick_on(storage, user=20, event="EV1", bout="B1")

    missing = await storage.pickem_players_missing_bout(1, "EV1", "B2")

    assert missing == [20], "10 already picked that fight"


async def test_somebody_who_never_touched_the_card_is_nudged_about_it(storage):
    await pick_on(storage, user=10, event="OLD", bout="B1")
    await pick_on(storage, user=20, event="EV1", bout="B1")

    assert await storage.pickem_players_missing_card(1, "EV1") == [10]


async def test_somebody_who_has_never_played_is_left_alone(storage):
    """There is no way to tell them from everyone else in the server."""
    await pick_on(storage, user=10, event="EV1", bout="B1")

    assert await storage.pickem_players_missing_card(1, "EV1") == []
    assert await storage.pickem_players_missing_bout(1, "EV1", "B1") == []


async def test_another_server_is_never_pinged(storage):
    from ufcbot.records import PickemRecord

    await storage.save_pickem_pick(
        PickemRecord(
            guild_id=2, user_id=99, espn_event_id="OLD", bout_id="B1", event_name="x",
            event_start=NOW, athlete_id="A", athlete_name="A", opponent_id="B", opponent_name="B",
            odds=-150, points_if_right=67, locks_at=NOW, picked_at=NOW,
        )
    )
    await pick_on(storage, user=10, event="EV1", bout="B1")

    assert await storage.pickem_players_missing_card(1, "EV1") == []
