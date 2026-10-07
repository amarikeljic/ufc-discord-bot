"""The fighter card: one page ESPN's way, two behind buttons.

The card leads with who somebody is and what the bot makes of them, and the
detail waits to be asked for. The tests that matter are about what each page is
for, and about the history page in particular -- it is the only one that knows
about fights before the UFC, which is the thing the ratings cannot see.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from ufcbot.embeds.common import keep
from ufcbot.embeds.fighters import (
    fighter_embed,
    fighter_history_embed,
    fighter_stats_embed,
)
from ufcbot.models import Fighter, FightHistoryEntry
from ufcbot.stats.career import Ledger
from ufcbot.stats.service import FighterCareer
from ufcbot.ui.fighter import HISTORY, OVERVIEW, STATS, FighterView


def espn() -> Fighter:
    """ESPN's side of Patricio Pitbull: a long career, two fights of it in the UFC."""
    return Fighter(
        id="2532870",
        display_name="Patricio Pitbull",
        record="38-9-0",
        weight_class="Featherweight",
        height="5' 6\"",
        reach="67\"",
        stance="Orthodox",
        age=39,
        citizenship="Brazil",
        team="Pitbull Brothers",
        headshot_url="https://example.invalid/p.png",
        profile_url="https://example.invalid/pitbull",
    )


def ufc() -> FighterCareer:
    ledger = Ledger(name="Patricio Pitbull")
    ledger.fights = ledger.stat_fights = 2
    ledger.wins, ledger.losses = 1, 1
    ledger.wins_ko, ledger.losses_dec = 1, 1
    ledger.elo = 1006.0
    ledger.last_fight = date(2026, 9, 19)
    ledger.last_result = "win"
    ledger.seconds = 1080.0
    return FighterCareer(ledger, None)


def history() -> list[FightHistoryEntry]:
    return [
        FightHistoryEntry(
            on=datetime(2026, 9, 19, tzinfo=UTC), event="UFC 1", opponent="Dooho Choi",
            result="W", method="KO/TKO", rounds=1,
        ),
        FightHistoryEntry(
            on=datetime(2024, 3, 22, tzinfo=UTC), event="Bellator", opponent="Jeremy Kennedy",
            result="W", method="KO/TKO", rounds=3, title_fight=True,
        ),
        FightHistoryEntry(
            on=datetime(2023, 6, 16, tzinfo=UTC), event="Bellator", opponent="Sergio Pettis",
            result="L", method="Decision - Unanimous", rounds=5, title_fight=True,
        ),
    ]


def values(embed) -> str:
    return " ".join(f.value for f in embed.fields) + (embed.description or "")


def test_the_first_page_gives_both_records_and_says_which_is_which():
    """A fighter who arrived with a career behind them reads 38-9 and 1-1 at
    once, and the gap between those two numbers is exactly what the rating has
    never seen."""
    embed = fighter_embed(espn(), ufc())

    text = values(embed)
    assert "Pro **38-9-0**" in text
    assert "UFC **1-1-0**" in text
    assert "**1006**" in text, "and what the bot makes of him, which is nothing much"


def test_the_first_page_carries_the_bio_espn_leads_with():
    embed = fighter_embed(espn(), ufc())

    tape = next(f.value for f in embed.fields if f.name == "Tale of the tape")
    for fact in ("Height 5' 6\"", "Reach 67\"", "Stance Orthodox", "Age 39",
                 "From Brazil", "Team Pitbull Brothers"):
        assert keep(fact) in tape
    assert embed.thumbnail.url == "https://example.invalid/p.png"


def test_the_first_page_does_not_carry_the_numbers():
    """They moved behind a button. Leaving them here as well would be the card
    the rework was undoing."""
    embed = fighter_embed(espn(), ufc())

    text = values(embed)
    assert "SLpM" not in text and "TD Def." not in text


def test_the_stats_page_says_the_numbers_are_ufc_only():
    """Half this fighter's career is missing from them, and nothing on the page
    would otherwise say so."""
    embed = fighter_stats_embed(espn(), ufc())

    assert "SLpM" in values(embed)
    assert "UFC fights only" in embed.footer.text


def test_the_stats_page_says_so_when_there_is_no_ufc_record():
    embed = fighter_stats_embed(espn(), None)

    assert "Debutants appear" in (embed.description or "")


def test_the_history_page_lists_the_whole_career_newest_first():
    """Including the fights that happened somewhere else, which is the only
    place in the bot they appear at all."""
    embed = fighter_history_embed(espn(), ufc(), history())

    lines = (embed.description or "").splitlines()
    assert len(lines) == 3
    assert keep("Dooho Choi") in lines[0] and "Sep 2026" in lines[0]
    assert keep("Jeremy Kennedy") in lines[1], "a Bellator fight ufcstats has never heard of"
    assert "🏆" in lines[1] and "🏆" not in lines[0]
    assert lines[0].startswith("🟩") and lines[2].startswith("🟥")
    assert "3 of 3 fights" in embed.footer.text


def test_a_long_career_is_cut_rather_than_split():
    """Past Discord's limit the embed is refused outright, and the oldest fights
    are the least worth reading."""
    from ufcbot.embeds.fighters import HISTORY_SHOWN

    many = history() * 20
    embed = fighter_history_embed(espn(), ufc(), many)

    assert len((embed.description or "").splitlines()) == HISTORY_SHOWN
    assert f"{HISTORY_SHOWN} of {len(many)} fights" in embed.footer.text


def test_the_history_page_says_so_when_there_is_none():
    embed = fighter_history_embed(espn(), ufc(), [])

    assert "No fight history" in (embed.description or "")


# -- the buttons -------------------------------------------------------------


def view(**kw) -> FighterView:
    return FighterView(profile=espn(), career=ufc(), **kw)


def test_the_card_opens_on_the_overview_with_that_button_disabled():
    card = view()

    assert card.page == OVERVIEW
    assert "Pro **38-9-0**" in values(card.embed())
    disabled = [b.label for b in card.children if b.disabled]
    assert disabled == ["Overview"], "the page you are on is not a button worth pressing"


async def test_the_history_is_only_fetched_when_somebody_opens_it():
    """It is a second call to ESPN, and most lookups never leave the first
    page."""
    calls = []

    async def loader():
        calls.append(1)
        return history()

    card = view(history=loader)

    await card._page(OVERVIEW)
    await card._page(STATS)
    assert calls == [], "neither of those needs it"

    first = await card._page(HISTORY)
    second = await card._page(HISTORY)
    assert calls == [1], "fetched once and kept"
    assert keep("Dooho Choi") in (first.description or "")
    assert first.description == second.description


def _loader(entries):
    async def load():
        return entries

    return load


async def test_each_page_builds_the_embed_it_names():
    card = view(history=_loader(history()))

    assert "Pro **38-9-0**" in values(await card._page(OVERVIEW))
    assert "SLpM" in values(await card._page(STATS))
    assert keep("Jeremy Kennedy") in (await card._page(HISTORY)).description


async def test_the_buttons_stop_working_when_the_card_times_out():
    """Rather than looking live and doing nothing when pressed."""
    card = view()

    await card.on_timeout()

    assert all(button.disabled for button in card.children)


@pytest.mark.parametrize("page", [OVERVIEW, STATS, HISTORY])
async def test_no_page_falls_over_without_a_ufc_record(page):
    """A debutant has an ESPN career and no ufcstats one at all."""
    card = FighterView(profile=espn(), career=None, history=_loader(history()))

    embed = await card._page(page)
    assert embed.title.startswith("Patricio Pitbull")
