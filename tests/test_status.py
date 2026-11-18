"""The dashboard summary: what the home dashboard tile reads, and what it never says.

Built on a Wednesday afternoon after UFC 334, with Saturday's card next, as a
bot that has been connected since lunchtime would see it. ESPN is faked; the
database is real.
"""

from __future__ import annotations

import json
import socket
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import aiohttp
from conftest import pickem_record

from ufcbot.config import Config
from ufcbot.features.tracking import PredictionTracker
from ufcbot.models import Bout, Event, Fighter, JudgeCard
from ufcbot.records import GuildSettings, PredictionRecord
from ufcbot.status import StatusServer

NOW = datetime(2026, 11, 18, 20, 5, tzinfo=UTC)  # 2:05 PM in Chicago
GANE, HOKIT = Fighter("4426000", "Ciryl Gane"), Fighter("4049391", "Josh Hokit")
LEADER_ID = 284019736512345678


def ufc_334() -> Event:
    main_event = Bout(id="401913646", segment="Main Card", match_number=1, rounds=5, competitors=2,
                      fighters=[HOKIT, GANE], winner_id=GANE.id, completed=True)
    opener = Bout(id="401932013", segment="Early Prelims", match_number=13, rounds=3, competitors=2,
                  fighters=[Fighter("1", "Evan Elder"), Fighter("2", "Samuel Sanches")], winner_id="1", completed=True)
    return Event(id="600061400", name="UFC 334: Gane vs. Hokit", start=datetime(2026, 11, 14, 22, tzinfo=UTC),
                 bouts=[opener, main_event])


class Espn:
    def __init__(self) -> None:
        self.next_card = Event(id="600061500", name="UFC Fight Night: Procházka vs. Stirling",
                               start=datetime(2026, 11, 21, 15, tzinfo=UTC))
        self.last_card = ufc_334()
        self.broken = False

    async def upcoming_events(self, **kwargs):
        if self.broken:
            raise RuntimeError("ESPN is down")
        return [self.next_card]

    async def recent_events(self, **kwargs):
        return [self.last_card]

    async def get_event(self, event_id, **kwargs):
        return self.last_card if event_id == self.last_card.id else None

    async def load_status(self, bout, **kwargs):
        bout.state, bout.period, bout.clock = "post", 5, "5:00"
        bout.result_method = "decision---unanimous" if bout.match_number == 1 else "kotko"

    async def load_scorecards(self, bout, **kwargs):
        return [JudgeCard(name, {GANE.id: 48, HOKIT.id: 47}) for name in ("Sal D'Amato", "Eduardo Passos", "Adrian Castro")]


class Bot:
    def __init__(self, storage) -> None:
        self.storage = storage
        self.data = Espn()
        self.tracker = PredictionTracker(storage, self.data)
        self.live = SimpleNamespace(active_events=self._nothing_on)
        self.connected_since = NOW - timedelta(minutes=2)  # 2:03 PM
        self.disconnected_since = None
        self.heartbeats = {"live": NOW - timedelta(seconds=10), "boards": NOW - timedelta(minutes=5)}
        member = SimpleNamespace(display_name="Amar")
        self.guild = SimpleNamespace(get_member=lambda user_id: member if user_id == LEADER_ID else None)

    @staticmethod
    async def _nothing_on():
        return []

    def get_guild(self, guild_id):
        return self.guild


async def a_bot(storage) -> Bot:
    bot = Bot(storage)
    await storage.save_settings(GuildSettings(guild_id=1, pickem_channel_id=503))
    card = bot.data.last_card
    records = [
        PredictionRecord(espn_event_id=card.id, bout_id=f"B{i}", event_name=card.name, event_start=card.start,
                         athlete_a=f"{i}a", name_a=f"A {i}", athlete_b=f"{i}b", name_b=f"B {i}", prob_a=0.6)
        for i in range(12)
    ]
    await storage.upsert_predictions(records)
    for i in range(12):
        await storage.grade_prediction(card.id, f"B{i}", f"{i}a" if i < 8 else f"{i}b", i < 8)
    await storage.save_pickem_pick(pickem_record(user_id=LEADER_ID, bout_id="B0", athlete_id="0a", opponent_id="0b",
                                                 locks_at=card.start, event_id=card.id))
    await storage.grade_pickem_bout("B0", "0a", loss_points=-100)
    return bot


async def test_each_tile_says_what_it_should(storage):
    summary = await StatusServer(await a_bot(storage), host="127.0.0.1", port=0).summary(now=NOW)

    assert summary["status"] == "connected since 2:03 PM"
    assert summary["next_event"] == "Procházka vs. Stirling · Sat 9:00 AM"
    assert summary["latest_result"] == "Gane def. Hokit · Unanimous decision 48-47, 48-47, 48-47"
    assert summary["bot_picks"] == "8-4 · Gane vs. Hokit"
    assert summary["pickem_leader"] == "Amar · 44 pts"
    assert summary["updated"] == "2026-11-18T20:05:00+00:00" and summary["updated_at"] == "2:05 PM"


async def test_it_names_nobody_by_id(storage):
    summary = await StatusServer(await a_bot(storage), host="127.0.0.1", port=0).summary(now=NOW)

    assert str(LEADER_ID) not in json.dumps(summary)


async def test_a_dropped_connection_says_so_and_since_when(storage):
    bot = await a_bot(storage)
    bot.disconnected_since = NOW - timedelta(minutes=8)

    status = (await StatusServer(bot, host="127.0.0.1", port=0).summary(now=NOW))["status"]

    assert status == "disconnected from Discord 8 min"


async def test_a_stuck_loop_says_so(storage):
    bot = await a_bot(storage)
    bot.heartbeats["live"] = NOW - timedelta(minutes=9)

    status = (await StatusServer(bot, host="127.0.0.1", port=0).summary(now=NOW))["status"]

    assert status == "connected since 2:03 PM, live loop stalled 9 min"


async def test_one_source_failing_costs_only_its_own_tile(storage):
    bot = await a_bot(storage)
    bot.data.broken = True

    summary = await StatusServer(bot, host="127.0.0.1", port=0).summary(now=NOW)

    assert summary["next_event"] == "unavailable"
    assert summary["latest_result"].startswith("Gane def. Hokit")


def a_free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


async def test_the_summary_is_served_read_only(storage):
    port = a_free_port()
    server = StatusServer(await a_bot(storage), host="127.0.0.1", port=port)
    await server.start()
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"http://127.0.0.1:{port}/api/summary") as response:
                assert response.status == 200
                assert (await response.json())["bot_picks"] == "8-4 · Gane vs. Hokit"
                assert response.headers["Cache-Control"] == "no-store"
            async with session.post(f"http://127.0.0.1:{port}/api/summary") as response:
                assert response.status == 405, "nothing on it can change anything"
            async with session.get(f"http://127.0.0.1:{port}/healthz") as response:
                assert (await response.json()) == {"status": "ok"}
    finally:
        await server.stop()


def test_outside_a_container_it_listens_on_loopback_only(monkeypatch):
    monkeypatch.setattr("ufcbot.config.load_dotenv", lambda: None)  # whatever .env is lying about
    monkeypatch.setenv("DISCORD_TOKEN", "test")
    monkeypatch.delenv("STATUS_HOST", raising=False)
    monkeypatch.delenv("STATUS_PORT", raising=False)

    config = Config.load()

    assert (config.status_host, config.status_port) == ("127.0.0.1", 8788)
