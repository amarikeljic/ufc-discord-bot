"""The dashboard tiles: status, data feeds, memory and errors, and nothing that can
change anything."""

from __future__ import annotations

import logging
import socket
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import aiohttp

from ufcbot.config import Config
from ufcbot.sources.http import Outage
from ufcbot.status import ESPN_HOST, StatusServer, _memory

NOW = datetime(2026, 11, 18, 20, 5, tzinfo=UTC)  # 2:05 PM in Chicago


def a_bot(**changes) -> SimpleNamespace:
    """A bot connected since lunchtime, its loops running and its feeds answering."""
    bot = SimpleNamespace(
        connected_since=NOW - timedelta(hours=2),
        disconnected_since=None,
        heartbeats={"live": NOW - timedelta(seconds=10), "boards": NOW - timedelta(minutes=5)},
        http_client=SimpleNamespace(outage=lambda host, **kwargs: None),
        stats=SimpleNamespace(last_error=None, is_behind=False, expected_newest=date(2026, 11, 14)),
        config=SimpleNamespace(enable_predictions=True),
    )
    for name, value in changes.items():
        setattr(bot, name, value)
    return bot


def tiles(bot, server=None) -> dict:
    return (server or StatusServer(bot, host="127.0.0.1", port=0)).summary(now=NOW)


def test_the_tiles_come_in_the_cards_order():
    assert list(tiles(a_bot())) == ["status", "data_feeds", "memory", "errors", "updated"]


def test_a_healthy_bot_just_says_ok():
    shown = tiles(a_bot())
    assert (shown["status"], shown["data_feeds"], shown["errors"]) == ("ok", "ok", "0")


def test_a_dropped_connection_says_so_and_for_how_long():
    assert tiles(a_bot(disconnected_since=NOW - timedelta(minutes=8)))["status"] == "disconnected from Discord 8 min"


def test_a_stuck_loop_says_which():
    bot = a_bot()
    bot.heartbeats["live"] = NOW - timedelta(minutes=9)
    assert tiles(bot)["status"] == "live loop stalled 9 min"


def test_before_it_has_connected_it_says_so():
    assert tiles(a_bot(connected_since=None))["status"] == "starting"


def test_espn_not_answering_shows_on_the_feeds_tile():
    down = Outage(host=ESPN_HOST, since=NOW, hours=25 / 60, failures=12, last_error="timed out")
    bot = a_bot(http_client=SimpleNamespace(outage=lambda host, **kwargs: down if host == ESPN_HOST else None))
    assert tiles(bot)["data_feeds"] == "ESPN down 25 min"


def test_fight_data_falling_behind_shows_on_the_feeds_tile():
    """A day or two late is how the dataset always is; three is worth a look."""
    behind = SimpleNamespace(last_error=None, is_behind=True, expected_newest=date(2026, 11, 15))
    assert tiles(a_bot(stats=behind))["data_feeds"] == "fight data 3 days behind"
    nearly = SimpleNamespace(last_error=None, is_behind=True, expected_newest=date(2026, 11, 17))
    assert tiles(a_bot(stats=nearly))["data_feeds"] == "ok"


def test_no_model_means_no_dataset_to_worry_about():
    behind = SimpleNamespace(last_error="no model", is_behind=True, expected_newest=date(2026, 11, 1))
    bot = a_bot(stats=behind, config=SimpleNamespace(enable_predictions=False))
    assert tiles(bot)["data_feeds"] == "ok"


def test_memory_is_read_from_the_kernel(tmp_path):
    status = tmp_path / "status"
    status.write_text("Name:\tpython\nVmHWM:\t  212256 kB\nVmRSS:\t  180224 kB\n", encoding="ascii")
    assert _memory(status) == "176 MB"
    assert _memory(tmp_path / "absent") == "-", "no /proc, as on Windows"


def test_errors_are_counted_for_a_day_and_warnings_are_not():
    server = StatusServer(a_bot(), host="127.0.0.1", port=0)
    logger = logging.getLogger("test.status.errors")
    logger.addHandler(server.errors)
    logger.propagate = False
    try:
        for created, level in (
            (NOW - timedelta(hours=30), logging.ERROR),  # yesterday's, gone by now
            (NOW - timedelta(hours=3), logging.WARNING),  # a warning, not an error
            (NOW - timedelta(hours=2), logging.ERROR),
            (NOW - timedelta(minutes=1), logging.ERROR),
        ):
            record = logger.makeRecord(logger.name, level, __file__, 0, "boom", (), None)
            record.created = created.timestamp()
            logger.handle(record)
    finally:
        logger.removeHandler(server.errors)

    assert tiles(None, server)["errors"] == "2 · last 2:04 PM"


def a_free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


async def test_the_tiles_are_served_read_only():
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
    assert server.errors not in logging.getLogger().handlers, "stopping takes the error count off the root logger"


def test_outside_a_container_it_listens_on_loopback_only(monkeypatch):
    monkeypatch.setattr("ufcbot.config.load_dotenv", lambda: None)  # whatever .env is lying about
    monkeypatch.setenv("DISCORD_TOKEN", "test")
    monkeypatch.delenv("STATUS_HOST", raising=False)
    monkeypatch.delenv("STATUS_PORT", raising=False)

    config = Config.load()

    assert (config.status_host, config.status_port) == ("127.0.0.1", 8788)
