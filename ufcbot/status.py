"""A read-only status for the bot, for a tile on a home dashboard.

The same arrangement as the Whale Watcher's card: Homepage's ``customapi``
widget reads ``GET /api/summary`` over a Docker network the two containers
share. Nothing here can change the bot or its data -- only GET is routed, and
the answer is worked out from what the bot already holds, with no request to
anybody.

``status`` is "ok", as the Whale Watcher's is, or says what is wrong: not yet
connected, disconnected from Discord and for how long, or a background loop
that has stopped running. If the bot is down nothing answers, and the dashboard
says so in place of the tile rather than showing an old one. Every answer
carries when it was made.

It listens on 127.0.0.1 unless told otherwise. The image tells it otherwise,
because inside a container that address is the container itself; there it
listens on the container's own interfaces, which are the Docker networks it is
on and nothing more, and the deploy publishes the port to this machine's
loopback only. No new port is opened to the network.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from aiohttp import web

from .util import central_time

log = logging.getLogger(__name__)

# The live loop runs every fifteen seconds and the boards every hour. Past
# these, the loop is stuck, and the status says which.
LIVE_LOOP_STALE = timedelta(minutes=2)
BOARDS_STALE = timedelta(hours=2)

HEADERS = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}


def _ago(gap: timedelta) -> str:
    minutes = int(gap.total_seconds() // 60)
    if minutes < 60:
        return f"{minutes} min"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} h {minutes} min" if minutes else f"{hours} h"


def _status(bot, now: datetime) -> str:
    """ "ok", or what is wrong."""
    if bot.disconnected_since is not None:
        return f"disconnected from Discord {_ago(now - bot.disconnected_since)}"
    if bot.connected_since is None:
        return "starting"
    problems = []
    for name, limit, label in (("live", LIVE_LOOP_STALE, "live loop"), ("boards", BOARDS_STALE, "boards")):
        last = bot.heartbeats.get(name)
        if last is not None and now - last > limit:
            problems.append(f"{label} stalled {_ago(now - last)}")
    return ", ".join(problems) or "ok"


class StatusServer:
    """Serves the status. Started and stopped with the bot."""

    def __init__(self, bot, *, host: str, port: int) -> None:
        self.bot = bot
        self.host, self.port = host, port
        self._runner: web.AppRunner | None = None

    async def start(self) -> None:
        app = web.Application()
        app.router.add_get("/api/summary", self._summary)
        app.router.add_get("/healthz", self._health)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.host, self.port)
        await site.start()
        log.info("Dashboard status on http://%s:%s/api/summary", self.host, self.port)

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    async def _health(self, request: web.Request) -> web.Response:
        return web.json_response({"status": "ok"}, headers=HEADERS)

    async def _summary(self, request: web.Request) -> web.Response:
        return web.json_response(self.summary(), headers=HEADERS)

    def summary(self, *, now: datetime | None = None) -> dict[str, Any]:
        now = now or datetime.now(central_time())
        return {
            "status": _status(self.bot, now),
            "updated": now.isoformat(timespec="seconds"),
        }
