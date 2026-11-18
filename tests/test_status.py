"""The dashboard status: "ok", or what is wrong, and nothing that can change anything."""

from __future__ import annotations

import socket
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import aiohttp

from ufcbot.config import Config
from ufcbot.status import StatusServer

NOW = datetime(2026, 11, 18, 20, 5, tzinfo=UTC)


def a_bot(**changes) -> SimpleNamespace:
    """A bot connected since lunchtime, its loops running."""
    bot = SimpleNamespace(
        connected_since=NOW - timedelta(hours=2),
        disconnected_since=None,
        heartbeats={"live": NOW - timedelta(seconds=10), "boards": NOW - timedelta(minutes=5)},
    )
    for name, value in changes.items():
        setattr(bot, name, value)
    return bot


def status(bot) -> str:
    return StatusServer(bot, host="127.0.0.1", port=0).summary(now=NOW)["status"]


def test_a_healthy_bot_just_says_ok():
    assert status(a_bot()) == "ok"


def test_a_dropped_connection_says_so_and_for_how_long():
    assert status(a_bot(disconnected_since=NOW - timedelta(minutes=8))) == "disconnected from Discord 8 min"


def test_a_stuck_loop_says_which():
    bot = a_bot()
    bot.heartbeats["live"] = NOW - timedelta(minutes=9)
    assert status(bot) == "live loop stalled 9 min"


def test_before_it_has_connected_it_says_so():
    assert status(a_bot(connected_since=None)) == "starting"


def test_every_answer_says_when_it_was_made():
    assert StatusServer(a_bot(), host="127.0.0.1", port=0).summary(now=NOW)["updated"] == "2026-11-18T20:05:00+00:00"


def a_free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


async def test_the_status_is_served_read_only():
    port = a_free_port()
    now = datetime.now(UTC)  # served on the real clock, so the bot is healthy as of now
    bot = a_bot(connected_since=now - timedelta(hours=2), heartbeats={"live": now, "boards": now})
    server = StatusServer(bot, host="127.0.0.1", port=port)
    await server.start()
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"http://127.0.0.1:{port}/api/summary") as response:
                assert response.status == 200
                assert (await response.json())["status"] == "ok"
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
