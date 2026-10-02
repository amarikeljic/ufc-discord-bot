"""That every embed builds, stays inside Discord's limits, and says the right thing.

Discord rejects an embed over 6000 characters or with more than 25 fields, and a
board that fails to build stops updating without saying so.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from conftest import bout, card, fighter, pickem_record, prediction

from ufcbot.embeds import (
    card_changes_embed,
    live_open_embed,
    live_result_embed,
    live_round_embed,
    pickem_board_embed,
    pickem_picker_embed,
    pickem_picks_embed,
    picks_board_embed,
    predictions_embed,
    rankings_embed,
    scheduled_event_description,
    scheduled_event_location,
)
from ufcbot.embeds.common import EMBED_BUDGET, FIELD_LIMIT, ZERO_WIDTH
from ufcbot.features.cardwatch import CardChange
from ufcbot.records import PickemRecord, PredictionRecord
from ufcbot.stats.rankings import Ranked

ALLEN = fighter("1", "Arnold Allen")
PICO = fighter("2", "Aaron Pico")
VOLK = fighter("3", "Alexander Volkanovski")
EVLOEV = fighter("4", "Movsar Evloev")
START = datetime.now(UTC) + timedelta(days=5)


def record(bout_id: str, a, b) -> PredictionRecord:
    """A pick with a line attached, which the board is expected not to show."""
    return PredictionRecord(
        espn_event_id="EV", bout_id=bout_id, event_name="UFC 333", event_start=START,
        athlete_a=a.id, name_a=a.display_name, athlete_b=b.id, name_b=b.display_name,
        prob_a=0.62, weight_class="Featherweight", position=0, odds_a=-160, odds_b=135,
        method="dec_u", method_prob=0.34,
        detail_json=json.dumps(prediction().to_dict()),
    )


def within_limits(embed) -> bool:
    """Every limit Discord enforces, including the per-field one.

    A field over 1024 characters is rejected outright rather than truncated, and
    it is the easy one to miss: the whole embed can be well inside its budget
    while one block of prose inside it is not."""
    return (
        len(embed) <= EMBED_BUDGET
        and len(embed.fields) <= 25
        and all(len(field.value or "") <= FIELD_LIMIT for field in embed.fields)
    )


def test_the_picks_board_carries_no_betting_line():
    """The picks channel is the model's opinion. Odds belong in pick'em, where
    they are what you are playing for."""
    embed = picks_board_embed("UFC 333", START, [record("B1", ALLEN, PICO)], locked=False)

    value = embed.fields[0].value.replace(" ", " ")
    assert "Odds" not in embed.description
    assert "-160" not in value and "Polymarket" not in value
    assert "Arnold Allen" in value, "the pick itself is still there"


def test_a_single_pick_is_not_called_picks():
    embed = picks_board_embed("UFC 333", START, [record("B1", ALLEN, PICO)], locked=False)
    assert "1 pick" in embed.description and "1 picks" not in embed.description


def test_a_full_card_of_picks_fits_in_an_embed():
    records = [record(f"B{i}", ALLEN, PICO) for i in range(13)]
    assert within_limits(picks_board_embed("UFC 333", START, records, locked=False))


def test_the_predictions_command_carries_no_betting_line():
    fight = bout("B1", ALLEN, PICO)
    fight.odds, fight.odds_provider = {"1": -160, "2": 135}, "Polymarket"
    embed = predictions_embed(card(fight, start=START), {"B1": prediction()})

    assert "Odds" not in embed.description
    assert "-160" not in embed.fields[0].value


def test_the_pickem_board_shows_the_lines_without_naming_the_source():
    """Every price comes from the same place, so saying so on every board is a
    line of text that never varies."""
    fight = bout("B1", ALLEN, PICO)
    fight.odds, fight.odds_provider = {"1": -160, "2": 135}, "Polymarket"

    embed = pickem_board_embed(card(fight, start=START), {}, 4, now=datetime.now(UTC))

    assert "Polymarket" not in embed.description
    assert "Odds from" not in embed.description
    assert "-160" in embed.fields[0].value, "the price itself is still shown"
    assert within_limits(embed)


def test_the_picker_counts_only_fights_still_on_the_card():
    """A pick on a fight that has come off is not a pick out of twelve, and its
    points are not there to be won."""
    event = card(bout("B1", ALLEN, PICO), bout("B2", VOLK, EVLOEV, match=2), start=START)
    picks = {
        bout_id: PickemRecord(
            guild_id=1, user_id=1, espn_event_id="EV", bout_id=bout_id, event_name="UFC 333",
            event_start=START, athlete_id="1", athlete_name="x", opponent_id="2", opponent_name="y",
            odds=-200, points_if_right=50, locks_at=START, picked_at=datetime.now(UTC),
            result="void" if bout_id == "GONE" else None,
            graded_at=datetime.now(UTC) if bout_id == "GONE" else None,
        )
        for bout_id in ("B1", "B2", "GONE")
    }

    embed = pickem_picker_embed(event, picks, page=0, pages=1, now=datetime.now(UTC))

    assert "Picked **2** of 2" in embed.description
    assert "100" in embed.description, "only the two live picks are worth points"


def test_the_predictions_embed_builds_for_a_full_card():
    bouts = [bout(f"B{i}", ALLEN, PICO, match=i) for i in range(1, 14)]
    event = card(*bouts, start=START)
    picks = {b.id: prediction() for b in bouts}
    assert within_limits(predictions_embed(event, picks))


def test_a_scheduled_event_description_is_only_the_card():
    event = card(bout("B1", ALLEN, PICO), start=START)
    event.broadcast = "Paramount+"
    event.main_card_start = START + timedelta(hours=3)

    text = scheduled_event_description(event, {})

    assert "Live on" not in text and "Prelims first" not in text
    assert "Arnold Allen vs. Aaron Pico" in text


def test_card_changes_read_as_what_happened():
    event = card(bout("B1", ALLEN, PICO), start=START)
    changes = [
        CardChange("replaced", "Allen vs Ige", "Featherweight", left="Aaron Pico", arrived="Dan Ige", opponent="Arnold Allen"),
        CardChange("removed", "Renato Moicano vs. Brian Ortega", "Lightweight"),
    ]
    # Embeds hold names together with non-breaking spaces so a narrow screen
    # wraps between facts, never inside one.
    value = card_changes_embed(event, changes).fields[0].value.replace(" ", " ")
    assert "Dan Ige** replaces Aaron Pico" in value
    assert "Off the card — Renato Moicano vs. Brian Ortega" in value


def test_a_ratings_board_builds():
    entries = [
        Ranked(rank=i, name=f"Fighter {i}", rating=1700 - i, record="12-2-0", division="Lightweight")
        for i in range(1, 16)
    ]
    embed = rankings_embed("Lightweight", entries)
    assert within_limits(embed) and "1699" in embed.fields[0].value


def test_every_live_post_names_the_card():
    fight = bout("B1", ALLEN, PICO)
    stats = {i: dict.fromkeys(("sig_l", "sig_a", "tot_l", "tot_a", "kd", "td_l", "td_a", "sub", "ctrl", "head", "body", "leg"), 10.0) for i in ("1", "2")}
    name = "UFC 331: Van vs. Pantoja 2"

    for embed in (
        live_open_embed(event_name=name, bout=fight, record=None, odds={}, careers={}),
        live_round_embed(event_name=name, bout=fight, round_number=2, stats=stats, edge=1),
        live_result_embed(event_name=name, bout=fight, winner=ALLEN, method="ko", technique="punches",
                          round_number=2, clock="3:41", totals=stats, scorecards={}, record=None, odds={}),
    ):
        assert embed.author.name == name, "the card is named in the post, not in the footer"
        assert embed.footer.text is None
        assert embed.timestamp is not None


def test_a_scheduled_event_is_located_by_city_not_arena():
    event = card(bout("B1", ALLEN, PICO), start=START)
    event.venue_name, event.venue_city, event.venue_country = "T-Mobile Arena", "Las Vegas", "USA"

    assert scheduled_event_location(event) == "Las Vegas, USA"


def test_the_fighter_card_shows_the_rating_and_where_it_places():
    from ufcbot.embeds import fighter_embed
    from ufcbot.stats.career import Ledger
    from ufcbot.stats.service import FighterCareer

    ledger = Ledger(name="Islam Makhachev")
    ledger.fights, ledger.wins, ledger.elo = 18, 17, 1273.4

    place = Ranked(rank=1, name=ledger.name, rating=1273, record="17-1-0", division="Welterweight")
    fields = {f.name: f.value for f in fighter_embed(None, FighterCareer(ledger, None), standing=place).fields}

    assert "1273" in fields["Bot Rating"]
    assert "1st at Welterweight" in fields["Bot Rating"].replace("\xa0", " ")


def test_the_fighter_card_calls_a_shared_rank_joint():
    from ufcbot.embeds import fighter_embed
    from ufcbot.stats.career import Ledger
    from ufcbot.stats.service import FighterCareer

    ledger = Ledger(name="Kamaru Usman")
    ledger.fights, ledger.wins, ledger.elo = 20, 16, 1171.0
    place = Ranked(rank=1, name=ledger.name, rating=1171, record="16-4-0", division="Middleweight", tied=True)

    fields = {f.name: f.value for f in fighter_embed(None, FighterCareer(ledger, None), standing=place).fields}

    assert "joint 1st at Middleweight" in fields["Bot Rating"].replace("\xa0", " ")


def test_every_name_on_a_ratings_board_starts_in_the_same_column():
    """A medal is a different width from a number, and a shared rank is a
    character wider again. Mixing the three pushed the names out of line."""
    entries = [
        Ranked(rank=1, name="A", rating=1200, record="10-0-0", division="Lightweight"),
        Ranked(rank=3, name="B", rating=1171, record="9-1-0", division="Lightweight", tied=True),
        Ranked(rank=3, name="C", rating=1171, record="9-1-0", division="Lightweight", tied=True),
        Ranked(rank=15, name="D", rating=1100, record="8-2-0", division="Lightweight"),
    ]
    lines = rankings_embed("Lightweight", entries).fields[0].value.split("\n")

    badges = [line[: line.index("`", 1) + 1] for line in lines]
    assert {len(badge) for badge in badges} == {5}, f"badges differ in width: {badges}"
    assert badges == ["`  1`", "`= 3`", "`= 3`", "` 15`"]


# -- everyone's picks for a card -------------------------------------------------


def graded(record, result: str, points: int):
    record.result, record.points = result, points
    record.graded_at = datetime.now(UTC)
    return record


def test_a_cards_picks_are_grouped_by_fight_with_who_backed_whom(soon):
    picks = [
        pickem_record(user_id=11, bout_id="B1", athlete_id="A", opponent_id="B", locks_at=soon),
        pickem_record(user_id=22, bout_id="B1", athlete_id="A", opponent_id="B", locks_at=soon),
        pickem_record(user_id=33, bout_id="B1", athlete_id="B", opponent_id="A", locks_at=soon),
    ]
    embed = pickem_picks_embed("UFC 331", picks)

    assert "3 players" in embed.description and "3 picks" in embed.description
    assert len(embed.fields) == 1, "one fight, one field"
    value = embed.fields[0].value
    assert "<@11>" in value and "<@22>" in value and "<@33>" in value
    # Both corners are listed, and the heading reads the same way either way.
    assert embed.fields[0].name == "Fighter A vs. Fighter B"


def test_picks_still_to_lock_are_counted_but_not_shown():
    embed = pickem_picks_embed("UFC 331", [], hidden=7)

    assert not embed.fields
    assert "lock" in embed.description


def test_a_card_nobody_played_says_so():
    assert "Nobody picked" in pickem_picks_embed("UFC 331", []).description


def test_the_card_scores_only_appear_once_something_has_been_graded(soon):
    pending = [pickem_record(user_id=11, bout_id="B1", athlete_id="A", opponent_id="B", locks_at=soon)]
    assert not any(field.name == "Card scores" for field in pickem_picks_embed("UFC 331", pending).fields)

    settled = [
        graded(pickem_record(user_id=11, bout_id="B1", athlete_id="A", opponent_id="B", locks_at=soon), "win", 44),
        graded(pickem_record(user_id=22, bout_id="B1", athlete_id="B", opponent_id="A", locks_at=soon), "loss", -100),
    ]
    scores = next(field for field in pickem_picks_embed("UFC 331", settled).fields if field.name == "Card scores")
    # The winner leads, and both totals carry their sign.
    assert scores.value.index("<@11>") < scores.value.index("<@22>")
    assert "+44" in scores.value and "-100" in scores.value


def test_the_picks_board_names_the_fights_it_cannot_call():
    """A debut has no UFC history to predict from. Dropping the fight silently
    leaves a board missing a bout with no hint it was ever on the card."""
    embed = picks_board_embed(
        "UFC 333", START, [record("B1", ALLEN, PICO)], locked=False,
        unpicked=[(1, "Mehemmedeli Osmanli vs. Ilimbek Akylbek Uulu")],
    )
    text = embed.description + " ".join(f"{f.name} {f.value}" for f in embed.fields)

    assert "Picks for 1 of 2 fights" in text
    assert "Mehemmedeli Osmanli vs. Ilimbek Akylbek Uulu" in text
    assert "UFC debut" in text
    names = [f.name for f in embed.fields]
    assert names.index("Mehemmedeli Osmanli vs. Ilimbek Akylbek Uulu") == 1, "in its place on the card"


def test_a_fully_picked_card_says_nothing_about_missing_fights():
    embed = picks_board_embed("UFC 333", START, [record("B1", ALLEN, PICO)], locked=False)
    text = embed.description + " ".join(f.value for f in embed.fields)

    assert "No pick" not in text and "of 1 fights" not in text


# -- have these two met before? -----------------------------------------------------


def test_a_first_meeting_says_nothing_about_a_series():
    """Most fights are first meetings, and a line saying so on every one of them
    is a line that never varies."""
    from ufcbot.embeds.picks import series_line
    from ufcbot.stats.career import NO_REMATCH

    assert series_line("A", "B", NO_REMATCH) is None


def test_the_series_names_whoever_is_ahead():
    from ufcbot.embeds.picks import series_line
    from ufcbot.stats.career import Rematch

    ahead = series_line("Alexander Volkanovski", "Max Holloway", Rematch(meetings=2, wins=2, losses=0))
    behind = series_line("Alexander Volkanovski", "Max Holloway", Rematch(meetings=3, wins=1, losses=2))

    assert "Volkanovski leads 2-0" in ahead.replace("\xa0", " ")
    assert "Holloway leads 2-1" in behind.replace("\xa0", " "), "named from the other corner"


def test_a_split_series_is_called_square_rather_than_led():
    from ufcbot.embeds.picks import series_line
    from ufcbot.stats.career import Rematch

    assert "all square" in series_line("A", "B", Rematch(meetings=2, wins=1, losses=1))
    assert "neither has won" in series_line("A", "B", Rematch(meetings=1, wins=0, losses=0))


def test_how_long_ago_is_only_said_when_it_is_known():
    from ufcbot.embeds.picks import series_line
    from ufcbot.stats.career import Rematch

    assert "last met" not in series_line("A", "B", Rematch(meetings=1, wins=1))
    assert "3y ago" in series_line("A", "B", Rematch(meetings=1, wins=1, days_since=1200.0))


def test_the_ratings_note_is_split_rather_than_rejected():
    """It explains two boards now and is past what Discord allows in one field."""
    from ufcbot.embeds import rankings_embed
    from ufcbot.stats.rankings import Ranked

    def entry(rank, name):
        return Ranked(rank=rank, name=name, rating=1200 - rank, record="10-2-0",
                      division="Lightweight", key=name)

    top = [entry(i, f"Fighter {i}") for i in range(1, 16)]
    embed = rankings_embed("Pound for pound", top, pound_for_pound=True, note=True, all_time=top)

    note = " ".join(f.value for f in embed.fields if "rating" in (f.name or "").lower() or f.name == ZERO_WIDTH)
    assert "All Time Ratings" in note and "every title they won and defended" in note
    assert within_limits(embed)


def test_a_board_carries_the_all_time_list_under_the_current_one():
    from ufcbot.embeds import rankings_embed
    from ufcbot.stats.rankings import Ranked

    now = [Ranked(rank=1, name="Active Fighter", rating=1100, record="9-1-0",
                  division="Welterweight", key="a")]
    ever = [Ranked(rank=1, name="Retired Great", rating=1260, record="20-2-0",
                   division="Welterweight", key="b")]
    embed = rankings_embed("Welterweight", now, all_time=ever)

    names = [f.name for f in embed.fields]
    plain = [f.value.replace(" ", " ") for f in embed.fields]

    assert names[0] == "Current Ratings" and "All Time" in names[1], "current first, all-time beneath"
    assert "Retired Great" not in plain[0]
    assert "Retired Great" in plain[1]
    assert within_limits(embed)


def test_a_board_says_who_holds_a_belt_and_who_held_one():
    """The question the board kept getting asked was why a champion sits below
    someone who is not one, so it has to say which is which."""
    from ufcbot.embeds import rankings_embed
    from ufcbot.stats.rankings import Ranked

    def entry(rank, name, **kw):
        return Ranked(rank=rank, name=name, rating=1200 - rank, record="10-2-0",
                      division="Lightweight", key=name, **kw)

    rows = [
        entry(1, "Contender"),
        entry(2, "Champion", champion=True),
        entry(3, "Ex Champion", former_champion=True),
    ]
    lines = rankings_embed("Lightweight", rows).fields[0].value.splitlines()

    assert "🏆" not in lines[0] and "🎖️" not in lines[0], "never held one"
    assert "🏆" in lines[1]
    assert "🎖️" in lines[2] and "🏆" not in lines[2]


def test_a_champion_is_not_also_marked_as_a_former_one():
    from ufcbot.stats.career import Ledger

    champ = Ledger(name="Champ")
    champ.held_belt, champ.champion = True, True
    lost_it = Ledger(name="Lost it")
    lost_it.held_belt, lost_it.champion = True, False
    interim_only = Ledger(name="Interim only")
    interim_only.held_belt, interim_only.title_wins = True, 0
    never = Ledger(name="Never")

    assert not champ.former_champion, "holding it now is not having held it"
    assert lost_it.former_champion
    # Aspinall's two heavyweight belts were both interim, so the lineal count is
    # zero and he still held a belt.
    assert interim_only.former_champion
    assert not never.former_champion


def test_a_full_ranking_is_never_split_across_two_fields():
    """Past 1024 characters Discord does not refuse a field, it takes the second
    half into a field of its own -- which puts a gap through the middle of a
    ranking. A fifteen-deep board has to fit in one.

    Both shapes, because they are the two longest for different reasons: pound
    for pound carries a division on every line, and a divisional board carries
    the odds against the champion. The real boards run to about 920 and 970 of
    the 1024.
    """
    from ufcbot.embeds import rankings_embed
    from ufcbot.embeds.common import FIELD_LIMIT
    from ufcbot.stats.rankings import Ranked

    p4p = [
        ("Jon Jones", "Light Heavyweight"), ("Georges St-Pierre", "Welterweight"),
        ("Anderson Silva", "Middleweight"), ("Demetrious Johnson", "Flyweight"),
        ("Islam Makhachev", "Lightweight"), ("Amanda Nunes", "Women's Bantamweight"),
        ("Valentina Shevchenko", "Women's Flyweight"), ("Kamaru Usman", "Welterweight"),
        ("Alexander Volkanovski", "Featherweight"), ("Matt Hughes", "Welterweight"),
        ("Israel Adesanya", "Middleweight"), ("Max Holloway", "Featherweight"),
        ("Stipe Miocic", "Heavyweight"), ("Daniel Cormier", "Light Heavyweight"),
        ("Chuck Liddell", "Light Heavyweight"),
    ]
    divisional = [
        "Charles Oliveira", "Arman Tsarukyan", "Justin Gaethje", "Ilia Topuria",
        "Dustin Poirier", "Grant Dawson", "Paddy Pimblett", "Quillan Salkilld",
        "Beneil Dariush", "Renato Moicano", "Benoit Saint Denis", "Jim Miller",
        "King Green", "Mateusz Gamrot", "Nasrat Haqparast",
    ]

    def row(i, name, division, champion):
        # A long record rather than the longest: one in six carries a no contest,
        # and fifteen of them in a row is not a board the sport produces.
        record = "22-11-0 (1 NC)" if i % 6 == 0 else "22-11-0"
        return Ranked(rank=i, name=name, rating=1250 - i * 5, record=record,
                      division=division, raw=1250 - i * 5, key=name, defences=12,
                      champion=champion, former_champion=not champion)

    boards = [
        ("Pound for pound", [row(i, n, d, False) for i, (n, d) in enumerate(p4p, 1)], True),
        ("Lightweight", [row(i, n, "Lightweight", i == 3) for i, n in enumerate(divisional, 1)], False),
    ]
    for title, rows, is_p4p in boards:
        embed = rankings_embed(title, rows, pound_for_pound=is_p4p, all_time=rows)
        for field in embed.fields:
            assert len(field.value) <= FIELD_LIMIT, f"{title}/{field.name} splits at {len(field.value)}"
        assert len(embed.fields) == 2, f"{title}: one field each for current and all time"


def board_row(i, name, rating, *, champion=False, key=None):
    from ufcbot.stats.rankings import Ranked

    return Ranked(rank=i, name=name, rating=rating, record="11-2-0", division="Lightweight",
                  raw=rating, key=key or name, champion=champion)


def test_a_divisional_line_says_what_the_gap_is_worth_against_the_champion():
    """Instead of a tie band, which says two fighters cannot be told apart
    without ever saying how far apart that is."""
    from ufcbot.embeds import rankings_embed

    rows = [
        board_row(1, "Charles Oliveira", 1212),
        board_row(2, "Justin Gaethje", 1155, champion=True),
        board_row(3, "Paddy Pimblett", 1120),
    ]
    lines = rankings_embed("Lightweight", rows).fields[0].value.replace(" ", " ").splitlines()

    assert "56% vs Gaethje" in lines[0], "rated above the champion, so better than even"
    assert "vs" not in lines[1], "the champion is not compared with himself"
    assert "46% vs Gaethje" in lines[2]


def test_pound_for_pound_carries_no_odds():
    """A win probability between a flyweight and a heavyweight is a number about
    a fight nobody can make."""
    from ufcbot.embeds import rankings_embed

    rows = [board_row(1, "Islam Makhachev", 1273, champion=True), board_row(2, "Max Holloway", 1206)]
    value = rankings_embed("Pound for pound", rows, pound_for_pound=True).fields[0].value

    assert "vs" not in value


def test_a_fading_fighter_is_marked_inactive_rather_than_given_odds():
    """The fade eases an absent fighter off the board. It is not a measured loss
    of skill, so running it through a win probability would turn a display rule
    into a claim about a fight -- and the number would tick down every day he
    stayed retired."""
    from ufcbot.embeds import rankings_embed

    rows = [
        board_row(1, "Justin Gaethje", 1155, champion=True),
        board_row(2, "Dustin Poirier", 1137, key="poirier"),
    ]
    value = rankings_embed("Lightweight", rows, fading=frozenset({"poirier"})).fields[0].value

    assert "inactive" in value and "% vs" not in value


def test_the_tie_marker_goes_where_the_odds_are_shown():
    """"=3" beside two different percentages is the board contradicting itself on
    one line, and a shared 3 with nothing to explain it is worse than either."""
    from ufcbot.embeds import rankings_embed
    from ufcbot.stats.rankings import Ranked

    tied = [
        Ranked(rank=1, name="Champ", rating=1200, record="11-2-0", division="Lightweight",
               raw=1200, key="champ", champion=True),
        Ranked(rank=2, name="A", rating=1150, record="11-2-0", division="Lightweight",
               raw=1150, key="a", tied=True),
        Ranked(rank=2, name="B", rating=1148, record="11-2-0", division="Lightweight",
               raw=1148, key="b", tied=True),
    ]
    divisional = rankings_embed("Lightweight", tied).fields[0].value
    p4p = rankings_embed("Pound for pound", tied, pound_for_pound=True).fields[0].value

    assert "=" not in divisional, "the percentages say how close they are"
    assert [line.split("`")[1].strip() for line in divisional.splitlines()] == ["1", "2", "3"]
    assert "=" in p4p, "pound for pound has no odds column, so the marker still earns its place"
