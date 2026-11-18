"""Live coverage: which fights the bot asks ESPN about while a card is on, and
the judges' scorecards that arrive after a result.

Fights happen one at a time, so the question the polling window answers is how
few requests a tick can get away with and still never miss a result.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import discord

from ufcbot.embeds.live import (
    SCORECARDS,
    TOTALS_ONLY,
    live_result_embed,
    scorecards_field,
    with_scorecards,
)
from ufcbot.features import live as live_module
from ufcbot.features.live import LiveCoverage
from ufcbot.models import Bout, Event, Fighter, JudgeCard
from ufcbot.sources.espn import UFCData
from ufcbot.sources.http import HttpError


def pending_bout(index: int, *, completed: bool = False) -> Bout:
    return Bout(
        id=f"B{index}",
        fighters=[Fighter(id=f"{index}a", display_name=f"A{index}"), Fighter(id=f"{index}b", display_name=f"B{index}")],
        competitors=2,
        completed=completed,
    )


def watched(bouts) -> list[str]:
    return [bout.id for bout in LiveCoverage._in_play(bouts)]


def test_before_the_card_only_the_first_few_are_watched():
    """Polling all twelve every fifteen seconds asks ESPN about ten fights that
    have not begun."""
    assert watched([pending_bout(i) for i in range(12)]) == ["B0", "B1", "B2"]


def test_mid_card_the_finished_the_live_and_the_next_are_watched():
    card = [pending_bout(i, completed=True) for i in range(4)] + [pending_bout(i) for i in range(4, 12)]
    assert watched(card) == ["B0", "B1", "B2", "B3", "B4", "B5", "B6"]


def test_a_finished_card_with_nothing_posted_is_watched_in_full():
    """A restart after the card still owes every result."""
    assert len(watched([pending_bout(i, completed=True) for i in range(12)])) == 12


def test_an_empty_card_watches_nothing():
    assert watched([]) == []


# -- the judges' scorecards --------------------------------------------------------

DUNCAN = Fighter(id="4867356", display_name="Chris Duncan")
DOBER = Fighter(id="2951202", display_name="Drew Dober")
OFFICIALS = "https://sports.core.api.espn.com/v2/sports/mma/leagues/ufc/events/1/competitions/2/officials"


def linescores(*judges: tuple[str, int, int]) -> dict:
    """One fighter's ESPN linescores document: (official id, order, total) per judge."""
    return {
        "items": [{
            "value": float(sum(total for _id, _order, total in judges)),
            "period": 0,
            "linescores": [
                {"value": float(total), "period": 0, "order": order,
                 "official": {"$ref": f"http://{OFFICIALS[8:]}/{official}?lang=en&region=us"}}
                for official, order, total in judges
            ],
        }],
    }


class FakeHttp:
    def __init__(self, pages: dict[str, dict]) -> None:
        self.pages = pages

    async def get_json(self, url: str, *, ttl=None, store=True):
        key = url.split("?")[0]
        if key not in self.pages:
            raise HttpError(f"404 {url}", answered=True)
        return self.pages[key]


def scored_bout() -> Bout:
    return Bout(
        id="401922642",
        fighters=[DOBER, DUNCAN],
        competitors=2,
        winner_id=DUNCAN.id,
        completed=True,
        linescore_refs={DOBER.id: "https://espn/dober/linescores", DUNCAN.id: "https://espn/duncan/linescores"},
    )


async def test_the_cards_are_read_judge_by_judge_and_matched_across_corners():
    """The two corners' documents list the judges in different orders, so a
    judge is matched by who they are, and named where ESPN names them."""
    http = FakeHttp({
        "https://espn/duncan/linescores": linescores(("4409191", 3, 29), ("4417545", 1, 30), ("5208560", 2, 30)),
        "https://espn/dober/linescores": linescores(("4417545", 1, 27), ("5208560", 2, 27), ("4409191", 3, 28)),
        f"{OFFICIALS}/4417545": {"firstName": "Dave", "lastName": "Bruce"},
        f"{OFFICIALS}/5208560": {"firstName": "Junichiro", "lastName": "Kamijo"},
        f"{OFFICIALS}/4409191": {"firstName": "Judge", "lastName": "3"},
    })
    cards = await UFCData(http).load_scorecards(scored_bout())

    assert cards == [
        JudgeCard("Dave Bruce", {DUNCAN.id: 30, DOBER.id: 27}),
        JudgeCard("Junichiro Kamijo", {DUNCAN.id: 30, DOBER.id: 27}),
        JudgeCard("Judge 3", {DUNCAN.id: 29, DOBER.id: 28}),
    ]


async def test_no_cards_yet_is_no_cards_not_an_error():
    """What ESPN returns between the result and the cards, and for every finish."""
    http = FakeHttp({
        "https://espn/duncan/linescores": {"count": 0, "items": []},
        "https://espn/dober/linescores": {"count": 0, "items": []},
    })
    assert await UFCData(http).load_scorecards(scored_bout()) == []


def split() -> list[JudgeCard]:
    return [
        JudgeCard("Sal D'Amato", {DUNCAN.id: 29, DOBER.id: 28}),
        JudgeCard("Derek Cleary", {DUNCAN.id: 28, DOBER.id: 29}),
        JudgeCard("Chris Lee", {DUNCAN.id: 29, DOBER.id: 28}),
    ]


def test_each_judge_gets_a_row_with_the_winners_score_first():
    name, value = scorecards_field(scored_bout(), DUNCAN, split())

    assert name == SCORECARDS
    rows = value.split("```")[1].strip().splitlines()
    assert rows[0].split() == ["DUNCAN", "DOBER"]
    assert [row.split() for row in rows[1:]] == [["D'Amato", "29", "28"], ["Cleary", "28", "29"], ["Lee", "29", "28"]]
    assert value.endswith(TOTALS_ONLY), "says round by round was not available rather than leaving it out"
    assert all(len(row) <= 24 for row in rows), "narrow enough not to wrap on a phone"


def test_a_finish_without_cards_shows_none():
    embed = live_result_embed(
        event_name="UFC 334", bout=scored_bout(), winner=DUNCAN, method="ko", technique="punches",
        round_number=2, clock="3:41", totals={}, scorecards=[], record=None, odds={},
    )
    assert not [f for f in embed.fields if f.name.startswith(SCORECARDS)]


def test_adding_the_cards_twice_shows_them_once():
    embed = live_result_embed(
        event_name="UFC 334", bout=scored_bout(), winner=DUNCAN, method="dec_s", technique=None,
        round_number=3, clock="5:00", totals={}, scorecards=[], record=None, odds={},
    )
    embed.add_field(name="Fight totals", value="...", inline=False)
    twice = with_scorecards(with_scorecards(embed, scored_bout(), DUNCAN, split()), scored_bout(), DUNCAN, split())

    assert [f.name for f in twice.fields] == [SCORECARDS, "Fight totals"]


# A result that posts before its cards, then the cards arriving on a later tick.

NOW = datetime(2026, 11, 15, 1, 50, tzinfo=UTC)


class SentMessage:
    def __init__(self, channel: LiveChannel, message_id: int) -> None:
        self.channel, self.id = channel, message_id

    async def edit(self, **kwargs) -> None:
        if self.channel.fail_edits:
            self.channel.fail_edits -= 1
            response = type("Response", (), {"status": 503, "reason": "", "headers": {}})()
            raise discord.HTTPException(response, {"code": 0, "message": "unavailable"})
        self.channel.edits.append((self.id, kwargs["embed"]))


class LiveChannel:
    def __init__(self, channel_id: int = 700) -> None:
        self.id, self.name = channel_id, "live"
        self.sent: list[discord.Embed] = []
        self.edits: list[tuple[int, discord.Embed]] = []
        self.fail_edits = 0

    async def send(self, **kwargs) -> SentMessage:
        self.sent.append(kwargs["embed"])
        return SentMessage(self, 9000 + len(self.sent))

    def get_partial_message(self, message_id: int) -> SentMessage:
        return SentMessage(self, message_id)


class FinishedCard:
    """Stands in for ESPN on a card whose fight has just been decided."""

    def __init__(self, method: str = "decision---split") -> None:
        self.bout = scored_bout()
        self.bout.segment, self.bout.rounds = "Prelims", 3
        self.event = Event(id="600061400", name="UFC 334", start=NOW - timedelta(hours=1), bouts=[self.bout])
        self.method = method
        self.cards: list[JudgeCard] = []
        self.asked_for_cards = 0
        ended = NOW - timedelta(minutes=2)
        self.plays = [
            *({"type": "Round End", "period": n, "wallclock": ended - timedelta(minutes=6 * (3 - n))} for n in (1, 2, 3)),
            {"type": "Fight Over", "period": 3, "wallclock": ended},
            {"type": "Results", "period": 3, "wallclock": ended},
        ]

    async def upcoming_events(self, **kwargs):
        return [self.event]

    async def recent_events(self, **kwargs):
        return []

    async def get_event(self, event_id, **kwargs):
        return self.event

    async def load_odds(self, event, **kwargs):
        return None

    async def load_bout_odds(self, bout, **kwargs):
        return None

    async def load_plays(self, bout, **kwargs):
        return self.plays

    async def load_status(self, bout, **kwargs):
        bout.state, bout.period, bout.clock, bout.result_method = "post", 3, "5:00", self.method

    async def load_fight_stats(self, bout, **kwargs):
        return {}

    async def load_scorecards(self, bout, **kwargs):
        self.asked_for_cards += 1
        return list(self.cards)


async def test_a_decision_posts_at_once_and_its_cards_are_added_by_editing_it(storage):
    """The result does not wait for the cards, and when they land the same
    message is edited. Nobody gets a second post about the same fight."""
    espn = FinishedCard()
    channel = LiveChannel()
    clock = {"now": NOW}
    coverage = LiveCoverage(espn, storage, clock=lambda: clock["now"])

    assert await coverage.tick([channel]) == 1
    assert len(channel.sent) == 1
    assert not [f for f in channel.sent[0].fields if f.name.startswith(SCORECARDS)], "none published yet"

    clock["now"] += timedelta(seconds=15)
    await coverage.tick([channel])
    assert channel.edits == [], "nothing to add until ESPN has them"

    espn.cards = split()
    clock["now"] += timedelta(minutes=4)
    assert await coverage.tick([channel]) == 0, "an edit, not a post: the boards have nothing new"
    assert len(channel.sent) == 1
    (message_id, edited), = channel.edits
    assert message_id == 9001, "the result post itself"
    assert edited.fields[0].name == SCORECARDS and "D'Amato" in edited.fields[0].value
    assert edited.title == channel.sent[0].title, "the rest of the post is left as it was"

    clock["now"] += timedelta(seconds=15)
    await coverage.tick([channel])
    assert len(channel.edits) == 1, "done once the cards are on it"


async def test_an_edit_discord_refuses_is_tried_again(storage, caplog):
    espn = FinishedCard()
    channel = LiveChannel()
    clock = {"now": NOW}
    coverage = LiveCoverage(espn, storage, clock=lambda: clock["now"])
    await coverage.tick([channel])

    espn.cards = split()
    channel.fail_edits = 1
    clock["now"] += timedelta(minutes=4)
    await coverage.tick([channel])
    assert channel.edits == [] and "Could not add the scorecards" in caplog.text

    clock["now"] += timedelta(seconds=15)
    await coverage.tick([channel])
    assert len(channel.edits) == 1


async def test_cards_that_never_come_stop_being_asked_for(storage):
    espn = FinishedCard()
    channel = LiveChannel()
    clock = {"now": NOW}
    coverage = LiveCoverage(espn, storage, clock=lambda: clock["now"])
    await coverage.tick([channel])

    clock["now"] += live_module.SCORECARDS_WAIT + timedelta(minutes=1)
    await coverage.tick([channel])
    asked = espn.asked_for_cards
    espn.cards = split()
    clock["now"] += timedelta(minutes=1)
    await coverage.tick([channel])

    assert espn.asked_for_cards == asked and channel.edits == []


async def test_a_finish_is_not_chased_for_cards(storage):
    """Asked once with the result, in case ESPN ever scores a stoppage, and
    never again."""
    espn = FinishedCard(method="kotko")
    channel = LiveChannel()
    clock = {"now": NOW}
    coverage = LiveCoverage(espn, storage, clock=lambda: clock["now"])

    await coverage.tick([channel])
    for _ in range(3):
        clock["now"] += timedelta(minutes=2)
        await coverage.tick([channel])

    assert espn.asked_for_cards == 1 and len(channel.sent) == 1 and channel.edits == []
