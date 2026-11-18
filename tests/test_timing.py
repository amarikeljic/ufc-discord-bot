"""Estimated fight times on the pick'em board.

Built on UFC 334 as it was scheduled -- early prelims at 22:00 UTC, prelims at
midnight, the main card at two -- with what really happened where a test needs
a fight to have ended: the opener's last bell was 22:33:00 and the second
fight's first bell came at 22:46:01.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import discord

from ufcbot.cogs.jobs import PICKEM_TIMES_REDRAW, JobsCog
from ufcbot.embeds import pickem_board_embed
from ufcbot.embeds.pickem import FIGHT_TIMES
from ufcbot.features.timing import FightClock, estimate_first_bells, fight_clocks
from ufcbot.models import Bout, Event, Fighter
from ufcbot.records import GuildSettings

EARLY = datetime(2026, 11, 14, 22, tzinfo=UTC)
PRELIMS = datetime(2026, 11, 15, 0, tzinfo=UTC)
MAIN = datetime(2026, 11, 15, 2, tzinfo=UTC)
DAY_BEFORE = EARLY - timedelta(days=1)


def at(hour: int, minute: int, second: int = 0, *, day: int = 14) -> datetime:
    return datetime(2026, 11, day, hour, minute, second, tzinfo=UTC)


def ufc_334() -> Event:
    """Thirteen fights in running order, match 13 first; the main event five rounds."""
    parts = [(EARLY, "Early Prelims", range(13, 9, -1)), (PRELIMS, "Prelims", range(9, 5, -1)), (MAIN, "Main Card", range(5, 0, -1))]
    bouts = [
        Bout(
            id=f"M{match}", segment=segment, match_number=match, start=air, competitors=2,
            rounds=5 if match == 1 else 3,
            fighters=[Fighter(f"{match}a", f"Al {match}"), Fighter(f"{match}b", f"Bo {match}")],
        )
        for air, segment, matches in parts
        for match in matches
    ]
    event = Event(id="600061400", name="UFC 334", start=EARLY, bouts=bouts, main_card_start=MAIN)
    event.segment_starts = {2: EARLY, 1: PRELIMS, 0: MAIN}
    return event


def test_a_day_out_each_part_opens_after_its_walk_in():
    times = estimate_first_bells(ufc_334(), now=DAY_BEFORE)

    assert times["M13"] == at(22, 15), "fourteen minutes after the early prelims go on air"
    assert times["M9"] == at(0, 15, day=15)
    assert times["M5"] == at(2, 20, day=15), "the main card takes longer to walk in"
    assert len(times) == 13


def test_every_time_is_on_a_five_minute_mark_and_in_running_order():
    times = estimate_first_bells(ufc_334(), now=DAY_BEFORE)
    order = [times[f"M{match}"] for match in range(13, 0, -1)]

    assert all(t.minute % 5 == 0 and t.second == 0 for t in order)
    assert order == sorted(order)


def test_a_prelims_part_fills_its_window_and_hands_over_on_time():
    """The early prelims are spread across the two hours before the prelims go on
    air, rather than chained on and left to run into them."""
    times = estimate_first_bells(ufc_334(), now=DAY_BEFORE)

    assert at(23, 0) < times["M10"] < PRELIMS
    assert times["M6"] < MAIN


def test_once_a_fight_ends_the_next_is_due_its_gap_after_it():
    """UFC 334's opener went the distance and ended at 22:33; the next fight's
    first bell came at 22:46. Eleven minutes after a decision on the prelims."""
    event = ufc_334()
    event.bouts[0].completed = True
    clocks = {"M13": FightClock(at(22, 15, 27), at(22, 33))}

    times = estimate_first_bells(event, now=at(22, 34), clocks=clocks)

    assert times["M12"] == at(22, 45)
    assert "M13" not in times, "a fight that has happened has no estimate"


def test_a_finish_leaves_a_longer_gap_than_a_decision():
    event = ufc_334()
    event.bouts[0].completed = True
    clocks = {"M13": FightClock(at(22, 15), at(22, 19))}  # stopped in the first round

    assert estimate_first_bells(event, now=at(22, 20), clocks=clocks)["M12"] == at(22, 35)


def test_a_fight_running_late_is_due_at_the_next_mark_and_moves_the_rest():
    event = ufc_334()
    event.bouts[0].completed = True
    clocks = {"M13": FightClock(at(22, 15, 27), at(22, 33))}
    on_time = estimate_first_bells(event, now=at(22, 34), clocks=clocks)

    late = estimate_first_bells(event, now=at(22, 52), clocks=clocks)  # 22:45 came and went

    assert late["M12"] == at(22, 55), "due now, shown at the next five minutes"
    assert late["M11"] > on_time["M11"], "and everything after it moves back"


def test_a_fight_under_way_puts_the_next_after_its_expected_end():
    event = ufc_334()
    clocks = {"M13": FightClock(at(22, 15))}

    times = estimate_first_bells(event, now=at(22, 20), clocks=clocks)

    assert "M13" not in times, "the fight on now has no estimate"
    assert at(22, 35) <= times["M12"] <= at(22, 45)


def test_a_part_running_long_holds_up_the_next():
    """The last prelim began at 02:05, after the main card had gone on air. The
    main card cannot start until it is over."""
    event = ufc_334()
    for bout in event.bouts:
        bout.completed = bout.match_number > 6
    clocks = {"M6": FightClock(at(2, 5, day=15))}

    times = estimate_first_bells(event, now=at(2, 10, day=15), clocks=clocks)

    assert times["M5"] > at(2, 20, day=15), "later than the main card's usual walk-in"


def test_the_board_shows_each_fight_its_time_and_says_they_are_estimates():
    event = ufc_334()
    event.bouts[0].completed, event.bouts[0].winner_id = True, "13a"
    starts = estimate_first_bells(event, now=DAY_BEFORE)
    assert "M13" not in starts
    embed = pickem_board_embed(event, {}, 0, now=DAY_BEFORE, starts=starts)
    by_name = {field.name: field.value for field in embed.fields}

    assert FIGHT_TIMES in embed.description
    assert by_name["Al 12 vs. Bo 12"].startswith(f"🕒 ≈ {discord.utils.format_dt(starts['M12'], 't')}")
    assert "🕒" not in by_name["Al 13 vs. Bo 13"], "a fight that has happened shows its result instead"


class PlayByPlay:
    """ESPN's play-by-play for UFC 334's first two fights, the second still going."""

    def __init__(self) -> None:
        self.asked: list[str] = []

    async def load_plays(self, bout, **kwargs):
        self.asked.append(bout.id)
        plays = {
            "M13": [("Round Start", 1, at(22, 15, 27)), ("Round End", 3, at(22, 33)), ("Fight Over", 0, at(22, 33))],
            "M12": [("Walkout", 0, at(22, 40)), ("Round Start", 1, at(22, 46, 1))],
        }.get(bout.id, [])
        return [{"type": kind, "period": period, "wallclock": when} for kind, period, when in plays]


async def test_the_clocks_come_from_the_play_by_play_and_only_once_the_card_is_on():
    event = ufc_334()
    event.bouts[0].completed = True
    espn = PlayByPlay()

    assert await fight_clocks(espn, event, now=DAY_BEFORE) == {}
    assert espn.asked == [], "nothing is asked before the card"

    clocks = await fight_clocks(espn, event, now=at(22, 50))
    assert clocks == {"M13": FightClock(at(22, 15, 27), at(22, 33)), "M12": FightClock(at(22, 46, 1))}
    assert set(espn.asked) == {"M13", "M12"}, "the last fight over and the one after it, nothing more"


# The board's times are redrawn while a card is on, even with nothing posted.


class LiveCardBot:
    def __init__(self, storage, *, card_on: bool) -> None:
        self.storage = storage
        self.redrawn: list[int] = []
        self.guild = SimpleNamespace(id=1)
        outer = self

        class Live:
            async def active_events(self):
                return ["UFC 334"] if card_on else []

        class Publisher:
            async def publish_pickem(self, guild, settings):
                outer.redrawn.append(guild.id)

        self.live, self.publisher = Live(), Publisher()

    def get_guild(self, guild_id):
        return self.guild


async def test_the_board_is_redrawn_every_five_minutes_while_a_card_is_on(storage, monkeypatch):
    await storage.save_settings(GuildSettings(guild_id=1, pickem_channel_id=503))
    bot = LiveCardBot(storage, card_on=True)
    jobs = JobsCog(bot)

    await jobs._keep_pickem_times()
    await jobs._keep_pickem_times()  # fifteen seconds later, say
    assert bot.redrawn == [1], "once, not every tick"

    jobs._last_pickem_redraw -= PICKEM_TIMES_REDRAW
    await jobs._keep_pickem_times()
    assert bot.redrawn == [1, 1]


async def test_no_card_on_means_no_redraw(storage):
    await storage.save_settings(GuildSettings(guild_id=1, pickem_channel_id=503))
    bot = LiveCardBot(storage, card_on=False)

    await JobsCog(bot)._keep_pickem_times()

    assert bot.redrawn == []
