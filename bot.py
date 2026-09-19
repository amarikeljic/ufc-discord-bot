"""Entry point. Run with: python bot.py"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sys
from pathlib import Path

import discord

from ufcbot.bot import UFCBot
from ufcbot.config import Config
from ufcbot.instance import AlreadyRunning, InstanceLock


def configure_logging(level: str) -> None:
    discord.utils.setup_logging(level=getattr(logging, level, logging.INFO))
    logging.getLogger("discord.http").setLevel(logging.WARNING)
    logging.getLogger("discord.gateway").setLevel(logging.WARNING)


async def main() -> None:
    try:
        config = Config.load()
    except RuntimeError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc

    configure_logging(config.log_level)

    lock = InstanceLock(Path(config.database_path).with_suffix(".lock"))
    try:
        lock.acquire()
    except AlreadyRunning as exc:
        logging.getLogger(__name__).error("%s", exc)
        raise SystemExit(1) from None

    bot = UFCBot(config)
    try:
        await bot.start(config.token)
    except discord.LoginFailure:
        logging.getLogger(__name__).error(
            "Discord rejected the token. Check DISCORD_TOKEN in your .env file."
        )
        raise SystemExit(1) from None
    finally:
        if not bot.is_closed():
            await bot.close()


if __name__ == "__main__":
    # Ctrl+C is how this is meant to be stopped, so it exits quietly.
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main())
