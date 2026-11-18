"""A read-only summary of the bot, for a tile on a home dashboard.

The same arrangement as the Whale Watcher's card: Homepage's ``customapi``
widget reads ``GET /api/summary`` over a Docker network the two containers
share. Nothing here can change the bot or its data -- only GET is routed, and
every field is read from what the bot already holds or would fetch anyway.

It listens on 127.0.0.1 unless told otherwise. The image tells it otherwise,
because inside a container that address is the container itself; there it
listens on the container's own interfaces, which are the Docker networks it is
on and nothing more, and the deploy publishes the port to this machine's
loopback only. No new port is opened to the network.

What it never says: tokens, webhook URLs, anybody's id, or anybody's picks.
The pick'em leader is a display name and a points total, which is what the
leaderboard in the server already shows everyone.

If the bot is down there is nothing answering here, and Homepage says so in
place of the figures rather than showing old ones; while it runs, ``status``
says whether it is connected to Discord and since when, and whether its loops
are keeping up. Every response carries when it was made.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta
from typing import Any

import discord
from aiohttp import web

from .embeds.common import surname
from .features.pickem import card_over
from .stats.techniques import METHOD_LABELS, method_from_espn
from .util import central_time, normalise

log = logging.getLogger(__name__)

# Homepage asks once a minute; anything newer than this is served as it was.
CACHE_SECONDS = 30
# The live loop runs every fifteen seconds and the boards every hour. Past
# these, the loop is stuck, and the tile says which.
LIVE_LOOP_STALE = timedelta(minutes=2)
BOARDS_STALE = timedelta(hours=2)
# A member's display name, fetched once in a while rather than on every call.
NAME_SECONDS = 3600

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
    if local.date() == today.date():
        return time_of_day
    if timedelta(0) < local - today < timedelta(days=6):
        return f"{local:%a} {time_of_day}"
    return f"{local:%b} {local.day}, {time_of_day}"


def _short(name: str) -> str:
    """ "UFC Fight Night: Procházka vs. Stirling" -> "Procházka vs. Stirling"."""
    return name.split(": ", 1)[1] if ": " in name else name


def _status(bot, now: datetime) -> str:
    if bot.disconnected_since is not None:
        return f"disconnected from Discord {_ago(now - bot.disconnected_since)}"
    if bot.connected_since is None:
        return "starting"
    problems = []
    for name, limit, label in (("live", LIVE_LOOP_STALE, "live loop"), ("boards", BOARDS_STALE, "boards")):
        last = bot.heartbeats.get(name)
        if last is not None and now - last > limit:
            problems.append(f"{label} stalled {_ago(now - last)}")
    return ", ".join([f"connected since {_clock(bot.connected_since, now)}", *problems])


async def _next_event(bot, now: datetime) -> str:
    for summary in await bot.live.active_events():
        if summary.start <= now:
            event = await bot.data.get_event(summary.id, ttl=300)
            if event is not None and not card_over(event, now):
                return f"Live: {_short(event.name)}"
    for event in await bot.data.upcoming_events(days=60, limit=6):
        if event.start > now and "contender series" not in normalise(event.name):
            return f"{_short(event.name)} · {_clock(event.start, now)}"
    return "none scheduled"


async def _latest_result(bot, now: datetime) -> str:
    cards = await bot.data.recent_events(days=21, limit=6)
    for summary in sorted(cards, key=lambda event: event.start, reverse=True):
        if summary.start > now or "contender series" in normalise(summary.name):
            continue
        event = await bot.data.get_event(summary.id, ttl=300)
        done = [bout for bout in (event.bouts if event else []) if bout.completed and bout.has_opponents]
        if not done:
            continue
        # The lowest match number to have finished is the latest fight of the night.
        fight = min(done, key=lambda bout: bout.match_number if bout.match_number is not None else 999)
        await bot.data.load_status(fight, ttl=300)
        method = method_from_espn(fight.result_method)
        winner = fight.fighter(str(fight.winner_id)) if fight.winner_id else None
        loser = fight.opponent(winner.id) if winner else None
        if winner is None or loser is None:
            return f"{fight.matchup} · {METHOD_LABELS.get(method or '', 'no result')}"
        how = METHOD_LABELS.get(method or "", "result in")
        if method in ("dec_u", "dec_s"):
            cards = await bot.data.load_scorecards(fight, ttl=600)
            totals = [f"{c.scores[winner.id]}-{c.scores[loser.id]}" for c in cards if {winner.id, loser.id} <= set(c.scores)]
            if totals:
                how = f"{how} {', '.join(totals)}"
        elif fight.period:
            how = f"{how} R{fight.period} {fight.clock or ''}".rstrip()
        return f"{surname(winner.display_name)} def. {surname(loser.display_name)} · {how}"
    return "none yet"


async def _bot_picks(bot, now: datetime) -> str:
    """The model's record on the card under way or just gone, else the picks for the next one."""
    events = await bot.storage.picked_events()
    started = [event for event in events if event[2] <= now]
    event_id, name, _start = started[0] if started else (events[-1] if events else (None, None, None))
    if event_id is None:
        return "no picks yet"
    records = await bot.tracker.records_for(event_id)
    scored = [record for record in records if record.correct is not None]
    if not scored:
        return f"{len(records)} picks · {_short(name)}"
    hits = sum(1 for record in scored if record.correct)
    pending = any(record.graded_at is None for record in records)
    return f"{hits}-{len(scored) - hits}{' so far' if pending else ''} · {_short(name)}"


class StatusServer:
    """Serves the summary. Started and stopped with the bot."""

    def __init__(self, bot, *, host: str, port: int) -> None:
        self.bot = bot
        self.host, self.port = host, port
        self._runner: web.AppRunner | None = None
        self._cached: tuple[float, dict[str, Any]] | None = None
        self._names: dict[int, tuple[float, str]] = {}

    async def start(self) -> None:
        app = web.Application()
        app.router.add_get("/api/summary", self._summary)
        app.router.add_get("/healthz", self._health)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.host, self.port)
        await site.start()
        log.info("Dashboard summary on http://%s:%s/api/summary", self.host, self.port)

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    async def _health(self, request: web.Request) -> web.Response:
        return web.json_response({"status": "ok"}, headers=HEADERS)

    async def _summary(self, request: web.Request) -> web.Response:
        return web.json_response(await self.summary(), headers=HEADERS)

    async def summary(self, *, now: datetime | None = None) -> dict[str, Any]:
        """Every field a short string, each worked out on its own, so one source
        failing says "unavailable" in its own tile and leaves the others."""
        if self._cached is not None and now is None and time.monotonic() - self._cached[0] < CACHE_SECONDS:
            return self._cached[1]
        stamp_now = now or datetime.now(central_time())
        out: dict[str, Any] = {"status": _status(self.bot, stamp_now)}
        for key, part in (
            ("next_event", _next_event),
            ("latest_result", _latest_result),
            ("bot_picks", _bot_picks),
            ("pickem_leader", self._pickem_leader),
        ):
            try:
                out[key] = await part(self.bot, stamp_now)
            except Exception:  # one tile must not cost the others
                log.exception("Working out %s for the dashboard failed", key)
                out[key] = "unavailable"
        out["updated"] = stamp_now.isoformat(timespec="seconds")
        out["updated_at"] = _clock(stamp_now, stamp_now)
        if now is None:
            self._cached = (time.monotonic(), out)
        return out

    async def _pickem_leader(self, bot, now: datetime) -> str:
        """Who tops the all-time board by points, as the board itself has it."""
        for settings in await bot.storage.guilds_with_channels():
            if not settings.pickem_channel_id:
                continue
            standings = await bot.storage.pickem_leaderboard(settings.guild_id)
            if not standings:
                return "nobody yet"
            top = standings[0]
            name = await self._name(settings.guild_id, top.user_id)
            return f"{name} · {top.points:,} pts"
        return "no pick'em channel"

    async def _name(self, guild_id: int, user_id: int) -> str:
        known = self._names.get(user_id)
        if known is not None and time.monotonic() - known[0] < NAME_SECONDS:
            return known[1]
        guild = self.bot.get_guild(guild_id)
        member = guild.get_member(user_id) if guild else None
        if member is None and guild is not None:
            try:
                member = await guild.fetch_member(user_id)
            except discord.HTTPException:  # left the server, or Discord would not say
                member = None
        name = member.display_name if member is not None else "a former member"
        self._names[user_id] = (time.monotonic(), name)
        return name
