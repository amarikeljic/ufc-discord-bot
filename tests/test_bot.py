"""The bot itself, built the way bot.py builds it.

Nothing else in the suite constructs it, which is how an attribute named
``status`` passed every test and took the bot down at start-up on the deploy
that followed: ``discord.Client`` already has a ``status`` property, whose
setter refuses anything but a ``discord.Status``. Building the bot here makes
a name like that fail the suite, which runs before every deploy, instead of
the deploy.
"""

from __future__ import annotations

import discord

from ufcbot.bot import UFCBot
from ufcbot.config import Config


def a_config(tmp_path, **overrides) -> Config:
    return Config(
        token="test",
        database_path=str(tmp_path / "bot.sqlite3"),
        data_dir=str(tmp_path / "data"),
        model_dir=str(tmp_path / "models"),
        **overrides,
    )


async def test_the_bot_can_be_built(tmp_path):
    bot = UFCBot(a_config(tmp_path))

    assert bot.status_server is not None
    assert isinstance(bot.status, discord.Status), "the client's own status is left as discord.py keeps it"


async def test_the_dashboard_reads_the_real_bot(tmp_path):
    """Every tile is worked out from the bot as it is really built, so one that
    reaches for something the bot does not have fails here and not on the server."""
    tiles = UFCBot(a_config(tmp_path)).status_server.summary()

    assert tiles["status"] == "starting", "not connected to Discord in a test"
    assert tiles["data_feeds"] == "ok" and tiles["errors"] == "0"


async def test_the_bot_can_be_built_without_the_dashboard_summary(tmp_path):
    bot = UFCBot(a_config(tmp_path, status_port=0))

    assert bot.status_server is None
    assert isinstance(bot.status, discord.Status)
