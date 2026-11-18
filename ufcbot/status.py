"""A read-only status for the bot, for a tile on a home dashboard.

The same arrangement as the Whale Watcher's card: Homepage's ``customapi``
widget reads ``GET /api/summary`` over a Docker network the two containers
share. Nothing here can change the bot or its data -- only GET is routed, and
every answer is worked out from what the bot already holds, with no request to
anybody. Four figures, in the order the card shows them:

    status       "ok", or what is wrong: not yet connected, disconnected from
                 Discord and for how long, or a background loop that stopped
    data_feeds   "ok", or which source is failing: ESPN, the odds, or the fight
                 dataset falling behind
    memory       what the bot holds in memory, as the kernel counts it
    errors       how many errors it has logged in the last day, and the last

If the bot is down nothing answers, and the dashboard says so in place of the
tiles rather than showing old ones. Every answer carries when it was made.

It listens on 127.0.0.1 unless told otherwise. The image tells it otherwise,
because inside a container that address is the container itself; there it
listens on the container's own interfaces, which are the Docker networks it is
on and nothing more, and the deploy publishes the port to this machine's
loopback only. No new port is opened to the network.
"""

from __future__ import annotations

import logging
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from aiohttp import web

from .sources.espn import CORE
from .sources.polymarket import GAMMA
from .util import central_time

log = logging.getLogger(__name__)

# The live loop runs every fifteen seconds and the boards every hour. Past
# these, the loop is stuck, and the status says which.
LIVE_LOOP_STALE = timedelta(minutes=2)
BOARDS_STALE = timedelta(hours=2)

ESPN_HOST = urlsplit(CORE).netloc
ODDS_HOST = urlsplit(GAMMA).netloc
# A feed is called down on the tile once it has failed this long, every time it
# was asked. Far sooner than the three hours the bot waits before telling the
# server: a tile is looked at, not pushed at anybody, so it can afford to be quick.
FEED_DOWN_AFTER = timedelta(minutes=10)
FEED_DOWN_ATTEMPTS = 5
# The dataset is published once a day and is often a day or two behind a card.
DATA_LATE_AFTER = timedelta(days=2)

ERROR_WINDOW = timedelta(hours=24)

HEADERS = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}


def _ago(gap: timedelta) -> str:
    minutes = int(gap.total_seconds() // 60)
    if minutes < 60:
        return f"{minutes} min"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} h {minutes} min" if minutes else f"{hours} h"


def _clock(when: datetime, now: datetime) -> str:
    """A time the way somebody glancing at a tile reads one, in Central time."""
    local, today = when.astimezone(central_time()), now.astimezone(central_time())
    time_of_day = local.strftime("%I:%M %p").lstrip("0")
    return time_of_day if local.date() == today.date() else f"{local:%a} {time_of_day}"


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


def _feeds(bot, now: datetime) -> str:
    """ "ok", or which source is failing. Read off what the bot already knows: the
    HTTP client's record of each host, and the stats service's of the dataset."""
    problems = []
    for host, label in ((ESPN_HOST, "ESPN"), (ODDS_HOST, "odds")):
        outage = bot.http_client.outage(host, after=FEED_DOWN_AFTER, attempts=FEED_DOWN_ATTEMPTS)
        if outage is not None:
            problems.append(f"{label} down {_ago(timedelta(hours=outage.hours))}")
    if bot.config.enable_predictions:
        stats = bot.stats
        if stats.last_error:
            problems.append("fight data failing to refresh")
        elif stats.is_behind and stats.expected_newest is not None:
            late = now.date() - stats.expected_newest
            if late >= DATA_LATE_AFTER:
                problems.append(f"fight data {late.days} days behind")
    return ", ".join(problems) or "ok"


def _memory(status: Path = Path("/proc/self/status")) -> str:
    """The bot's resident memory, from the kernel. "-" where there is no /proc."""
    try:
        for line in status.read_text(encoding="ascii").splitlines():
            if line.startswith("VmRSS:"):
                return f"{int(line.split()[1]) // 1024} MB"
    except (OSError, ValueError, IndexError):
        pass
    return "-"


class ErrorCounter(logging.Handler):
    """Counts the errors the bot logs. Kept for a day, and in memory, so a restart
    starts the count again."""

    def __init__(self) -> None:
        super().__init__(level=logging.ERROR)
        # Bounded, so a storm of errors costs a fixed amount of memory.
        self._times: deque[float] = deque(maxlen=10_000)

    def emit(self, record: logging.LogRecord) -> None:
        self._times.append(record.created)

    def last_day(self, now: datetime) -> str:
        cutoff = (now - ERROR_WINDOW).timestamp()
        while self._times and self._times[0] < cutoff:
            self._times.popleft()
        if not self._times:
            return "0"
        last = datetime.fromtimestamp(self._times[-1], tz=now.tzinfo)
        return f"{len(self._times)} · last {_clock(last, now)}"


class StatusServer:
    """Serves the status. Started and stopped with the bot."""

    def __init__(self, bot, *, host: str, port: int) -> None:
        self.bot = bot
        self.host, self.port = host, port
        self.errors = ErrorCounter()
        self._runner: web.AppRunner | None = None

    async def start(self) -> None:
        logging.getLogger().addHandler(self.errors)
        app = web.Application()
        app.router.add_get("/api/summary", self._summary)
        app.router.add_get("/healthz", self._health)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.host, self.port)
        await site.start()
        log.info("Dashboard status on http://%s:%s/api/summary", self.host, self.port)

    async def stop(self) -> None:
        logging.getLogger().removeHandler(self.errors)
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
            "data_feeds": _feeds(self.bot, now),
            "memory": _memory(),
            "errors": self.errors.last_day(now),
            "updated": now.isoformat(timespec="seconds"),
        }
