"""Reading ESPN's feed: the shapes it actually sends, and the ones it fails to.

Every field here comes out of a real response. ESPN is an undocumented feed that
changes without warning and answers a missing fighter with an absent key rather
than an error, so the parsing worth pinning down is mostly what happens when
something is not there: the bot should end up with a card missing one fight, not
an exception that stops the card loading at all.
"""

from __future__ import annotations

import pytest
from conftest import bout, card, fighter

from ufcbot.models import segment_rank
from ufcbot.sources.espn import (
    _athlete_id_from_uid,
    _fighter_from_payload,
    _first_web_link,
    _venue_fields,
    _venue_from_competitions,
)
from ufcbot.stats.techniques import method_from_espn, technique_from_espn
from ufcbot.util import parse_api_datetime

# -- athlete ids -----------------------------------------------------------------


def test_the_athlete_id_is_picked_out_of_the_uid():
    assert _athlete_id_from_uid("s:3301~a:2335639") == "2335639"
    assert _athlete_id_from_uid("s:3301~l:3301~a:4426059") == "4426059"


@pytest.mark.parametrize("uid", [None, "", "s:3301", "s:3301~l:3301"])
def test_a_uid_with_no_athlete_in_it_gives_nothing(uid):
    assert _athlete_id_from_uid(uid) is None


# -- fighters ---------------------------------------------------------------------


def test_a_fighter_is_read_from_the_profile_document():
    figher = _fighter_from_payload(
        {
            "id": 2335639,
            "displayName": "Alexander Volkanovski",
            "nickname": "The Great",
            "weightClass": {"text": "Featherweight"},
            "displayHeight": "5' 6\"",
            "displayReach": "71\"",
            "stance": {"text": "Orthodox"},
            "age": 37,
            "citizenship": "Australia",
            "headshot": {"href": "https://a.espncdn.com/x.png"},
        }
    )

    assert figher.id == "2335639", "ids are used as strings everywhere else"
    assert figher.display_name == "Alexander Volkanovski"
    assert (figher.nickname, figher.stance, figher.age) == ("The Great", "Orthodox", 37)


def test_a_fighter_with_no_id_is_not_a_fighter():
    """Without an id there is nothing to match a pick or a result against."""
    assert _fighter_from_payload({"displayName": "Someone"}) is None
    assert _fighter_from_payload({}) is None


def test_a_debutant_with_almost_no_profile_still_reads():
    """A newcomer has no nickname, no reach and no headshot. That is a fighter
    with blanks, not a failure to parse."""
    figher = _fighter_from_payload({"id": 5, "fullName": "New Guy"})

    assert figher.display_name == "New Guy", "fullName stands in for displayName"
    assert figher.nickname is None and figher.headshot_url is None


def test_an_age_that_is_not_a_number_is_left_out():
    """ESPN has been seen sending an empty string here, and the profile card
    would print it as an age."""
    assert _fighter_from_payload({"id": 5, "displayName": "X", "age": ""}).age is None
    assert _fighter_from_payload({"id": 5, "displayName": "X", "age": 30}).age == 30


def test_the_profile_link_prefers_the_full_site():
    links = [
        {"href": "http://m.espn.com/mma/fighter/_/id/5", "rel": ["mobile"]},
        {"href": "https://www.espn.com/mma/fighter/_/id/5", "rel": ["desktop", "athlete"]},
    ]
    assert _first_web_link(links) == "https://www.espn.com/mma/fighter/_/id/5"


def test_any_web_link_beats_none():
    assert _first_web_link([{"href": "https://x/", "rel": ["mobile"]}]) == "https://x/"
    assert _first_web_link([{"href": "app://internal", "rel": ["desktop"]}]) is None
    assert _first_web_link(None) is None


# -- venues -------------------------------------------------------------------------


def test_a_venue_reads_as_name_city_country():
    name, city, country = _venue_fields(
        {"fullName": "T-Mobile Arena", "address": {"city": "Las Vegas", "state": "NV", "country": "USA"}}
    )
    assert (name, city, country) == ("T-Mobile Arena", "Las Vegas, NV", "USA")


def test_a_venue_outside_the_states_has_no_state_to_fold_in():
    assert _venue_fields(
        {"fullName": "Etihad Arena", "address": {"city": "Abu Dhabi", "country": "UAE"}}
    ) == ("Etihad Arena", "Abu Dhabi", "UAE")


def test_a_venue_with_no_address_is_still_a_venue():
    assert _venue_fields({"fullName": "Apex"}) == ("Apex", None, None)


def test_the_venue_is_taken_off_a_fight_when_one_carries_it():
    """Saves a request per card: the fights already name the building."""
    competitions = [
        {"id": "1"},
        {"id": "2", "venue": {"fullName": "Apex", "address": {"city": "Las Vegas", "country": "USA"}}},
    ]
    assert _venue_from_competitions(competitions) == ("Apex", "Las Vegas", "USA")


def test_no_fight_naming_a_venue_means_asking_for_it_separately():
    assert _venue_from_competitions([{"id": "1"}, {"id": "2", "venue": {}}]) is None
    assert _venue_from_competitions([]) is None


# -- the order a card is fought in -----------------------------------------------------


def test_a_card_runs_main_event_first_then_down_through_the_prelims():
    a, b = fighter("1", "A"), fighter("2", "B")
    event = card(
        bout("prelim2", a, b, match=2),
        bout("main1", a, b, match=1),
        bout("prelim1", a, b, match=1),
        bout("main2", a, b, match=2),
        start=parse_api_datetime("2026-09-21T23:00Z"),
    )
    for fight in event.bouts:
        fight.segment = "Main Card" if fight.id.startswith("main") else "Prelims"

    assert [fight.id for fight in event.ordered_bouts()] == ["main1", "main2", "prelim1", "prelim2"]


def test_a_fight_with_no_segment_is_not_sorted_above_the_main_event():
    assert segment_rank("Main Card") < segment_rank(None)
    assert segment_rank("Main Card") < segment_rank("Prelims")
    assert segment_rank("Prelims") < segment_rank("Early Prelims")


# -- how a fight ended -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("espn", "method"),
    [
        ("kotko", "ko"),
        ("submission", "sub"),
        ("decision---unanimous", "dec_u"),
        ("decision---split", "dec_s"),
        ("decision---majority", "dec_s"),
        ("draw", "draw"),
        ("tko---doctor-s-stoppage", "ko"),
    ],
)
def test_espn_result_names_map_onto_the_methods_the_model_knows(espn, method):
    assert method_from_espn(espn) == method


def test_a_result_espn_has_not_filled_in_yet_is_not_guessed():
    """The live post says how a fight ended and goes out once. Better to wait
    for the wording than to publish a result with the method invented."""
    assert method_from_espn(None) is None
    assert method_from_espn("") is None
    assert method_from_espn("no-contest") is None


def test_the_finishing_technique_is_read_where_espn_gives_one():
    assert technique_from_espn("sub", "Rear Naked Choke", None) == "rear-naked choke"
    assert technique_from_espn("ko", "Punches", "Head") == "punches"
    # A decision has no technique to read.
    assert technique_from_espn("dec_u", "Decision", None) is None
