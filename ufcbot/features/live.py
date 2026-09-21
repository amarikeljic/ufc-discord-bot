"""Live fight coverage: previews, knockdowns, stats after every round, and official results.

During a card the bot polls ESPN's play-by-play for every bout. Each update is
recorded once in the database, so a restart mid-card never repeats a post. ESPN
only publishes cumulative fight stats, so per-round numbers are the difference
between the totals captured at the end of consecutive rounds.

Order for each fight: "Up next" as they walk out, stats at the end of every
round including the last, then the result once the decision has been read out.
"""

from __future__ import annotations

import asyncio
import io
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import discord

from ..embeds import live_open_embed, live_result_embed, live_round_embed
from ..embeds.images import IMAGE_NAME, MatchupImages
from ..models import Bout, Event
from ..sources.espn import UFCData
from ..stats.service import FighterCareer
from ..stats.techniques import method_from_espn, technique_from_espn
from ..storage import Storage

log = logging.getLogger(__name__)

LIVE_TTL = 10
# Odds stop moving once a fight is under way, so they are cached far longer.
ODDS_TTL = 600
# A card is watched from shortly before the first bell until well after it should be over.
WATCH_BEFORE = timedelta(minutes=45)
WATCH_AFTER = timedelta(hours=12)
# ESPN's stat totals trail the round-end play slightly; wait before snapshotting.
STATS_SETTLE = timedelta(seconds=20)
# ESPN logs a "Results" play when the official decision is read. If it never
# arrives, post the result anyway once this long has passed since the fight ended.
OFFICIAL_FALLBACK = timedelta(minutes=10)
# How many fights past the one under way to keep watching, so a walkout is never
# missed if ESPN is slow to mark the fight before it finished.
LOOKAHEAD = 3

STAT_SOURCES = {
    "sig_l": ("sigStrikesLanded",),
    "sig_a": ("sigStrikesAttempted",),
    "tot_l": ("totalStrikesLanded",),
    "tot_a": ("totalStrikesAttempted",),
    "kd": ("knockDowns",),
    "td_l": ("takedownsLanded",),
    "td_a": ("takedownsAttempted",),
    "sub": ("submissions",),
    "ctrl": ("timeInControl",),
    "head": ("sigDistanceHeadStrikesLanded", "sigClinchHeadStrikesLanded", "sigGroundHeadStrikesLanded"),
    "body": ("sigDistanceBodyStrikesLanded", "sigClinchBodyStrikesLanded", "sigGroundBodyStrikesLanded"),
    "leg": ("sigDistanceLegStrikesLanded", "sigClinchLegStrikesLanded", "sigGroundLegStrikesLanded"),
}


def summarise(raw: dict[str, float]) -> dict[str, float]:
    """ESPN's stat names to the handful shown in live posts."""
    return {key: float(sum(raw.get(name, 0.0) for name in names)) for key, names in STAT_SOURCES.items()}


def subtract(after: dict[str, float], before: dict[str, float] | None) -> dict[str, float]:
    before = before or {}
    return {key: max(0.0, after.get(key, 0.0) - before.get(key, 0.0)) for key in STAT_SOURCES}


def _method_from_plays(plays: list[dict]) -> str | None:
    """ESPN logs a play such as "Unofficial Winner Kotko" before it fills in the official result."""
    for play in reversed(plays):
        if play["type"].lower().startswith("unofficial winner"):
            return method_from_espn(play["type"])
    return None


def stats_edge(a: dict[str, float], b: dict[str, float]) -> int:
    """1 if the numbers favour A, -1 for B, 0 if too close to call. Not a judge's score."""

    def points(s: dict[str, float]) -> float:
        return s["sig_l"] + 10 * s["kd"] + 3 * s["td_l"] + s["ctrl"] / 20 + 2 * s["sub"]

    pa, pb = points(a), points(b)
    if abs(pa - pb) < max(4.0, 0.15 * max(pa, pb)):
        return 0
    return 1 if pa > pb else -1


class LiveCoverage:
    def __init__(
        self,
        data: UFCData,
        storage: Storage,
        *,
        images: MatchupImages | None = None,
        career_provider: Callable[[str], FighterCareer | None] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.data = data
        self.storage = storage
        self.images = images
        # Optional: looks up a fighter's ufcstats.com career for the preview's tale of the tape.
        self.career_provider = career_provider
        self.clock = clock or (lambda: datetime.now(UTC))

    async def active_events(self) -> list[Event]:
        now = self.clock()
        upcoming, recent = await asyncio.gather(
            self.data.upcoming_events(days=2, limit=5), self.data.recent_events(days=2, limit=3)
        )
        candidates: dict[str, Event] = {}
        for event in upcoming + recent:
            if event.start - WATCH_BEFORE <= now <= event.start + WATCH_AFTER:
                candidates[event.id] = event
        return sorted(candidates.values(), key=lambda e: e.start)

    async def tick(self, channels: list[discord.abc.Messageable]) -> int:
        """Post whatever happened since the last tick. Returns the number of posts."""
        if not channels:
            return 0
        posted = 0
        for summary in await self.active_events():
            event = await self.data.get_event(summary.id, ttl=LIVE_TTL)
            if event is None:
                continue
            await self.data.load_odds(event, ttl=ODDS_TTL)
            # ordered_bouts runs main event first; reversed is the order they are fought.
            bouts = [bout for bout in reversed(event.ordered_bouts()) if bout.has_opponents]
            posted_keys = await self.storage.live_posted_many([bout.id for bout in bouts])
            live = self._in_play([bout for bout in bouts if "result" not in posted_keys[bout.id]])

            # Fetch their play-by-play at once, so the posts below read from cache
            # instead of waiting on one request per fight.
            await asyncio.gather(
                *(self.data.load_plays(bout, ttl=LIVE_TTL) for bout in live), return_exceptions=True
            )
            for bout in live:
                try:
                    posted += await self._cover(event, bout, channels, posted_keys[bout.id])
                except Exception:  # one bout must not stop the card
                    log.exception("Live coverage failed for %s", bout.matchup)
        return posted

    @staticmethod
    def _in_play(pending: list[Bout]) -> list[Bout]:
        """The fights worth asking about, given ``pending`` is in the order they are fought.

        Fights happen one at a time, so polling the whole card every fifteen
        seconds asks ESPN about ten fights that have not begun. This keeps the
        ones already fought but not yet posted, the one under way, and the next
        couple, which is everything that can produce an update. On a twelve-fight
        card it is the difference between twelve requests a tick and about three.
        """
        started = next((i for i, bout in enumerate(pending) if not bout.completed), len(pending))
        return pending[: started + LOOKAHEAD]

    async def _cover(
        self, event: Event, bout: Bout, channels: list[discord.abc.Messageable], done: set[str]
    ) -> int:
        now = self.clock()
        plays = [
            p for p in await self.data.load_plays(bout, ttl=LIVE_TTL)
            if p["wallclock"] is None or p["wallclock"] <= now
        ]
        if not plays:
            return 0

        over = next((p for p in plays if p["type"] == "Fight Over"), None)
        announced = any(p["type"] == "Results" for p in plays)
        final_round = max((p["period"] for p in plays if p["type"] == "Round End"), default=0)

        if not done and over is not None:
            # Joined after the fight ended: skip the play-by-play and post only the result.
            for key in ["open"] + [f"round:{p['period']}" for p in plays if p["type"] == "Round End"]:
                await self.storage.mark_live_posted(bout.id, key)
            done = await self.storage.live_posted(bout.id)

        posted = 0
        record = await self.storage.prediction_for_bout(bout.id)
        if record is not None and {record.athlete_a, record.athlete_b} != {f.id for f in bout.fighters[:2]}:
            # The pick on record was made for a different pairing, so a fighter has
            # been replaced since. Its pick, method and closing line all describe a
            # fight that is not the one about to happen.
            record = None
        odds, source = self._odds(bout, record)

        if "open" not in done:
            if len(odds) < 2:
                # Lines for a fight can land minutes before the walkout, so take one
                # last look rather than posting the preview without them.
                await self.data.load_bout_odds(bout, ttl=LIVE_TTL)
                odds, source = self._odds(bout, record)
            embed = live_open_embed(
                event_name=event.name,
                bout=bout,
                record=record,
                odds=odds,
                odds_source=source,
                careers=self._careers(bout),
            )
            image = await self._matchup(bout)
            if image:
                embed.set_image(url=f"attachment://{IMAGE_NAME}")
            await self._send(channels, embed=embed, image=image)
            await self._mark(bout, "open", done)
            posted += 1

        for play in plays:
            if play["type"] != "Round End":
                continue
            round_number = play["period"]
            key = f"round:{round_number}"
            if key in done:
                continue
            if play["wallclock"] and now - play["wallclock"] < STATS_SETTLE:
                continue
            totals = await self._totals(bout)
            if totals is None:
                continue
            previous = await self.storage.live_snapshot(bout.id, round_number - 1)
            delta = {athlete: subtract(stats, (previous or {}).get(athlete)) for athlete, stats in totals.items()}
            await self.storage.save_live_snapshot(bout.id, round_number, totals)

            a, b = bout.fighters[0], bout.fighters[1]
            edge = stats_edge(delta[a.id], delta[b.id]) if a.id in delta and b.id in delta else 0
            stopped = await self._ended_in_round(bout, round_number, final_round, over)
            embed = live_round_embed(
                event_name=event.name, bout=bout, round_number=round_number, stats=delta, edge=edge, stopped=stopped
            )
            leader = a if edge > 0 else b if edge < 0 else None
            if leader and leader.headshot_url:
                embed.set_thumbnail(url=leader.headshot_url)
            await self._send(channels, embed=embed)
            await self._mark(bout, key, done)
            posted += 1

        if over is not None and "result" not in done:
            final_posted = final_round == 0 or f"round:{final_round}" in done
            waited = now - (over["wallclock"] or now)
            # Round stats first, then the result, unless the stats never turn up.
            if final_posted or waited >= OFFICIAL_FALLBACK:
                posted += await self._post_result(
                    event, bout, channels, record, odds, final_round, announced, waited, plays
                )

        return posted

    async def _post_result(
        self,
        event,
        bout,
        channels,
        record,
        odds,
        final_round: int,
        announced: bool,
        waited: timedelta,
        plays: list[dict],
    ) -> int:
        if not announced and waited < OFFICIAL_FALLBACK:
            return 0
        await self.data.load_status(bout, ttl=LIVE_TTL)
        method = method_from_espn(bout.result_method)
        no_contest = "contest" in (bout.result_method or "").lower()
        finished = bout.state == "post" or bout.completed
        # Wait until ESPN has named the winner, or confirmed there is none.
        if not finished or (not bout.winner_id and method != "draw" and not no_contest):
            return 0

        if method is None:
            # ESPN sets the winner flag before it fills in how the fight ended, and this
            # post only goes out once. Try again uncached, then read the play-by-play,
            # rather than posting a result that never says how it was won.
            await self.data.load_status(bout, ttl=0)
            method = method_from_espn(bout.result_method) or _method_from_plays(plays)
            no_contest = "contest" in (bout.result_method or "").lower()
            if method is None and not no_contest and waited < OFFICIAL_FALLBACK:
                return 0

        totals = await self._totals(bout) or {}
        scorecards = await self.data.load_scorecards(bout) if method in ("dec_u", "dec_s", "draw") else {}
        winner = bout.fighter(str(bout.winner_id)) if bout.winner_id else None
        embed = live_result_embed(
            event_name=event.name,
            odds_source=self._odds(bout, record)[1],
            bout=bout,
            winner=winner,
            method=method,
            technique=technique_from_espn(method, bout.result_description, bout.result_target),
            round_number=bout.period or final_round or None,
            clock=bout.clock,
            totals=totals,
            scorecards=scorecards,
            record=record,
            odds=odds,
        )
        image = await self._matchup(bout, winner_id=winner.id if winner else None)
        if image:
            embed.set_image(url=f"attachment://{IMAGE_NAME}")
        await self._send(channels, embed=embed, image=image)
        await self.storage.mark_live_posted(bout.id, "result")
        return 1

    async def _ended_in_round(self, bout: Bout, round_number: int, final_round: int, over: dict | None) -> bool:
        """Whether the fight was finished inside this round rather than going the distance.

        ESPN's round-end clock is not a reliable signal, so this uses the result:
        the last round of a fight that ended early, or that ended by KO/TKO or submission.
        """
        if over is None or round_number != final_round:
            return False
        if bout.rounds and final_round < bout.rounds:
            return True
        await self.data.load_status(bout, ttl=LIVE_TTL)
        return method_from_espn(bout.result_method) in ("ko", "sub")

    def _careers(self, bout: Bout) -> dict[str, FighterCareer]:
        """Career stats for both fighters, keyed by athlete id. Empty when the model is off."""
        if self.career_provider is None:
            return {}
        careers = {}
        for fighter in bout.fighters[:2]:
            career = self.career_provider(fighter.display_name)
            if career is not None:
                careers[fighter.id] = career
        return careers

    async def _mark(self, bout: Bout, key: str, done: set[str]) -> None:
        await self.storage.mark_live_posted(bout.id, key)
        done.add(key)

    async def _matchup(self, bout: Bout, *, winner_id: str | None = None) -> bytes | None:
        if self.images is None or not bout.has_opponents:
            return None
        try:
            return await self.images.matchup(bout.fighters[0], bout.fighters[1], winner_id=winner_id)
        except Exception as exc:  # the post still goes out without art
            log.debug("Matchup image failed for %s: %r", bout.matchup, exc)
            return None

    async def _totals(self, bout: Bout) -> dict[str, dict[str, float]] | None:
        raw = await self.data.load_fight_stats(bout, ttl=LIVE_TTL)
        if len(raw) < 2:
            return None
        return {athlete: summarise(values) for athlete, values in raw.items()}

    @staticmethod
    def _odds(bout: Bout, record) -> tuple[dict[str, int], str | None]:
        """(lines, where they came from): the locked closing line if there is one, else the current one.

        ``record`` is only ever passed here once it has been confirmed to describe
        this pairing.
        """
        if record is not None and record.odds_a is not None and record.odds_b is not None:
            # Recorded before the card; the book behind it was not stored.
            return {record.athlete_a: record.odds_a, record.athlete_b: record.odds_b}, None
        return dict(bout.odds), bout.odds_provider

    @staticmethod
    async def _send(
        channels: list[discord.abc.Messageable],
        *,
        content: str | None = None,
        embed: discord.Embed | None = None,
        image: bytes | None = None,
    ) -> None:
        for channel in channels:
            kwargs: dict = {}
            if content:
                kwargs["content"] = content
            if embed is not None:
                kwargs["embed"] = embed
            if image:
                # A File is consumed by sending, so each channel gets its own.
                kwargs["file"] = discord.File(io.BytesIO(image), filename=IMAGE_NAME)
            try:
                await channel.send(**kwargs)
            except discord.HTTPException as exc:
                log.warning("Live post failed in %s: %r", getattr(channel, "name", channel), exc)
