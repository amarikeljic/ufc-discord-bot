"""Data source backed by ESPN's public MMA endpoints.

Two hosts are used:

``sports.core.api.espn.com``
    The structured "core" feed. Events, fights, athletes and venues are
    separate documents linked by ``$ref``, so building a full card means
    following links. They are fetched concurrently and cached.

``site.web.api.espn.com``
    Only for fighter name search, which the core feed does not offer.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from urllib.parse import quote

from ..models import Bout, Event, Fighter, segment_rank
from ..util import https, normalise, parse_api_datetime
from .http import HttpClient, HttpError
from .polymarket import PROVIDER as POLYMARKET
from .polymarket import PolymarketOdds

log = logging.getLogger(__name__)

CORE = "https://sports.core.api.espn.com/v2/sports/mma"
LEAGUE = f"{CORE}/leagues/ufc"
SEARCH = "https://site.web.api.espn.com/apis/search/v2"

# The events endpoint rejects ranges longer than about a year.
MAX_RANGE_DAYS = 364

# Athlete bios and venues barely change; cache them far longer than schedules.
STATIC_TTL = 24 * 60 * 60


class UFCData:
    """Reads UFC schedules, fight cards and fighter profiles."""

    def __init__(self, http: HttpClient, *, odds_fallback: PolymarketOdds | None = None) -> None:
        self.http = http
        # ESPN has no lines for some cards, Contender Series among them.
        self.odds_fallback = odds_fallback

    # -- schedules ----------------------------------------------------------

    async def _window_refs(self, start: datetime, end: datetime) -> list[str]:
        url = (
            f"{LEAGUE}/events?limit=100"
            f"&dates={start.strftime('%Y%m%d')}-{end.strftime('%Y%m%d')}"
        )
        payload = await self.http.get_json(url)
        return [https(item["$ref"]) for item in payload.get("items", []) if item.get("$ref")]

    async def _event_refs(self, start: datetime, end: datetime) -> list[str]:
        """Event links covering an arbitrary span.

        The endpoint rejects ranges longer than about a year, so longer spans are
        split into windows and queried in parallel.
        """
        if end <= start:
            return []

        windows: list[tuple[datetime, datetime]] = []
        cursor = start
        while cursor < end:
            stop = min(end, cursor + timedelta(days=MAX_RANGE_DAYS))
            windows.append((cursor, stop))
            cursor = stop + timedelta(days=1)

        results = await asyncio.gather(
            *(self._window_refs(a, b) for a, b in windows), return_exceptions=True
        )

        refs: list[str] = []
        seen: set[str] = set()
        for result in results:
            if isinstance(result, BaseException):
                log.debug("Event window failed: %r", result)
                continue
            for ref in result:
                if ref and ref not in seen:
                    seen.add(ref)
                    refs.append(ref)
        return refs

    async def upcoming_events(self, *, days: int = 90, limit: int = 10) -> list[Event]:
        """Events starting from now, soonest first. Summary only, no fight cards."""
        now = datetime.now(UTC)
        # Reach back a little so a card that started tonight is still listed.
        refs = await self._event_refs(now - timedelta(days=1), now + timedelta(days=days))
        events = await self._load_events(refs, with_bouts=False)
        cutoff = now - timedelta(hours=6)
        upcoming = [event for event in events if event.start >= cutoff]
        upcoming.sort(key=lambda e: e.start)
        return upcoming[:limit]

    async def recent_events(self, *, days: int = 90, limit: int = 10) -> list[Event]:
        """Events that have already happened, most recent first."""
        now = datetime.now(UTC)
        refs = await self._event_refs(now - timedelta(days=days), now)
        events = await self._load_events(refs, with_bouts=False)
        past = [event for event in events if event.start < now]
        past.sort(key=lambda e: e.start, reverse=True)
        return past[:limit]

    async def next_event(self) -> Event | None:
        """The next card, with its full fight card resolved."""
        # Widen the search until something turns up; the calendar can have long gaps.
        for window in (45, 120, 365):
            events = await self.upcoming_events(days=window, limit=1)
            if events:
                return await self.get_event(events[0].id)
        return None

    async def get_event(self, event_id: str, *, ttl: int | None = None) -> Event | None:
        """One event with every bout and fighter resolved.

        Pass a short ``ttl`` during a live card so winner flags are current.
        """
        try:
            payload = await self.http.get_json(f"{LEAGUE}/events/{event_id}", ttl=ttl)
        except HttpError as exc:
            log.warning("Could not load event %s: %r", event_id, exc)
            return None
        return await self._build_event(payload, with_bouts=True)

    async def find_event(self, query: str, *, days: int = 365) -> Event | None:
        """Look up an event by name, nickname or number, e.g. "UFC 325" or "Silva"."""
        wanted = normalise(query)
        if not wanted:
            return None

        now = datetime.now(UTC)
        refs = await self._event_refs(now - timedelta(days=days), now + timedelta(days=days))
        candidates = await self._load_events(refs, with_bouts=False)

        scored: list[tuple[int, tuple[int, float], Event]] = []
        for event in candidates:
            haystack = normalise(f"{event.name} {event.short_name or ''}")
            if wanted == haystack:
                score = 0
            elif haystack.startswith(wanted):
                score = 1
            elif wanted in haystack:
                score = 2
            elif all(token in haystack for token in wanted.split()):
                score = 3
            else:
                continue
            # Names repeat across years, so favour an upcoming card over a past one,
            # then the one nearest to today.
            delta = (event.start - now).total_seconds()
            proximity = (0, delta) if delta >= 0 else (1, -delta)
            scored.append((score, proximity, event))

        if not scored:
            return None
        scored.sort(key=lambda row: (row[0], row[1]))
        return await self.get_event(scored[0][2].id)

    # -- event construction -------------------------------------------------

    async def _load_events(self, refs: list[str], *, with_bouts: bool) -> list[Event]:
        if not refs:
            return []
        payloads = await asyncio.gather(
            *(self.http.get_json(ref) for ref in refs), return_exceptions=True
        )
        built = await asyncio.gather(
            *(
                self._build_event(payload, with_bouts=with_bouts)
                for payload in payloads
                if not isinstance(payload, BaseException)
            ),
            return_exceptions=True,
        )
        for payload in payloads:
            if isinstance(payload, BaseException):
                log.debug("Skipping event that failed to load: %r", payload)
        return [event for event in built if isinstance(event, Event)]

    async def _build_event(self, payload: dict, *, with_bouts: bool) -> Event | None:
        start = parse_api_datetime(payload.get("date"))
        if start is None or not payload.get("id"):
            return None

        event = Event(
            id=str(payload["id"]),
            name=payload.get("name") or payload.get("shortName") or "UFC Event",
            short_name=payload.get("shortName"),
            start=start,
            espn_url=_first_web_link(payload.get("links")),
        )

        competitions = payload.get("competitions") or []

        # Each fight carries the venue inline, so the usual case costs no extra request.
        venue = _venue_from_competitions(competitions)
        if venue is None:
            venue = await self._load_venue(payload.get("venues"))
        if venue:
            event.venue_name, event.venue_city, event.venue_country = venue

        if with_bouts:
            # Broadcaster lives behind another link, so only pay for it on detail views.
            loaded, event.broadcast = await asyncio.gather(
                self._load_bouts(competitions), self._load_broadcast(competitions)
            )
            event.bouts, event.partial = loaded

        # The event date is when the broadcast opens, so the main card time has to
        # come from the fights on that segment.
        main_card_starts = [
            dt
            for dt in (
                parse_api_datetime(c.get("date"))
                for c in competitions
                if segment_rank((c.get("cardSegment") or {}).get("description")) == 0
            )
            if dt is not None
        ]
        if main_card_starts:
            event.main_card_start = min(main_card_starts)

        return event

    async def _load_broadcast(self, competitions: list[dict]) -> str | None:
        """Name of the network carrying the main card, e.g. "ESPN+" or "Paramount+"."""
        main_card = min(
            competitions,
            key=lambda c: c.get("matchNumber") if isinstance(c.get("matchNumber"), int) else 999,
            default=None,
        )
        ref = https((main_card or {}).get("broadcasts", {}).get("$ref"))
        if not ref:
            return None
        try:
            payload = await self.http.get_json(ref)
        except HttpError:
            return None
        for item in payload.get("items", []):
            media = item.get("media") or {}
            name = media.get("shortName") or media.get("name") or media.get("callLetters")
            if name:
                return name
        return None

    async def _load_venue(self, venues: list | None) -> tuple[str | None, str | None, str | None] | None:
        if not venues:
            return None
        ref = https(venues[0].get("$ref"))
        if not ref:
            return None
        try:
            payload = await self.http.get_json(ref, ttl=STATIC_TTL)
        except HttpError:
            return None
        return _venue_fields(payload)

    async def _load_bouts(self, competitions: list[dict]) -> tuple[list[Bout], bool]:
        """Every fight on the card, and whether any of them could not be built.

        A caller deciding that a fight has come off the card needs to know the
        difference between a card without it and a card that did not fully load.
        """
        if not competitions:
            return [], False
        results = await asyncio.gather(
            *(self._build_bout(c) for c in competitions), return_exceptions=True
        )
        bouts = [bout for bout in results if isinstance(bout, Bout)]
        for result in results:
            if isinstance(result, BaseException):
                log.warning("Skipping a fight that failed to load: %r", result)
        return bouts, len(bouts) != len(competitions)

    async def _build_bout(self, competition: dict) -> Bout:
        competitors = sorted(
            competition.get("competitors") or [],
            key=lambda c: c.get("order") if isinstance(c.get("order"), int) else 99,
        )
        fighters = await asyncio.gather(
            *(self._fighter_from_competitor(c) for c in competitors), return_exceptions=True
        )

        winner_id = None
        for competitor in competitors:
            if competitor.get("winner"):
                winner_id = str(competitor.get("id"))
                break

        regulation = (competition.get("format") or {}).get("regulation") or {}

        stats_refs: dict[str, str] = {}
        linescore_refs: dict[str, str] = {}
        for competitor in competitors:
            athlete_id = str(competitor.get("id") or "")
            if not athlete_id:
                continue
            stats_ref = https((competitor.get("statistics") or {}).get("$ref"))
            if stats_ref:
                stats_refs[athlete_id] = stats_ref
            own_ref = https(competitor.get("$ref"))
            if own_ref:
                linescore_refs[athlete_id] = own_ref.split("?")[0] + "/linescores"

        # Most cards link their odds document inline. Contender Series competitions do
        # not, so fall back to the standard address, which serves lines once they exist.
        competition_ref = (https(competition.get("$ref")) or "").split("?")[0]
        odds_ref = https((competition.get("odds") or {}).get("$ref"))
        if not odds_ref and competition_ref:
            odds_ref = f"{competition_ref}/odds"

        return Bout(
            odds_ref=odds_ref,
            status_ref=https((competition.get("status") or {}).get("$ref")),
            plays_ref=https((competition.get("details") or {}).get("$ref")),
            stats_refs=stats_refs,
            linescore_refs=linescore_refs,
            id=str(competition.get("id") or ""),
            weight_class=(competition.get("type") or {}).get("text"),
            segment=(competition.get("cardSegment") or {}).get("description"),
            match_number=competition.get("matchNumber")
            if isinstance(competition.get("matchNumber"), int)
            else None,
            rounds=regulation.get("periods"),
            start=parse_api_datetime(competition.get("date")),
            fighters=[f for f in fighters if isinstance(f, Fighter)],
            competitors=len(competitors),
            winner_id=winner_id,
            # The detailed status sits behind another link; the inline winner flag is
            # the cheapest reliable "this fight is done" signal.
            completed=winner_id is not None,
        )

    async def _fighter_from_competitor(self, competitor: dict) -> Fighter | None:
        athlete_ref = https((competitor.get("athlete") or {}).get("$ref"))
        if not athlete_ref:
            return None
        try:
            payload = await self.http.get_json(athlete_ref, ttl=STATIC_TTL)
        except HttpError:
            return None

        fighter = _fighter_from_payload(payload)
        if fighter is None:
            return None
        fighter.record = await self._load_record(https((competitor.get("record") or {}).get("$ref")))
        return fighter

    # -- per-fight detail --------------------------------------------------

    async def load_odds(self, event: Event, *, ttl: int = 600, fallback: bool = True) -> None:
        """Attach current moneylines to every bout that has them.

        ``fallback`` allows the prediction market to fill in for fights no
        sportsbook prices. Pick'em turns it off: points are staked at a real
        book's line or not at all.
        """
        await asyncio.gather(
            *(self.load_bout_odds(bout, ttl=ttl) for bout in event.bouts if bout.odds_ref),
            return_exceptions=True,
        )
        if fallback and self.odds_fallback is not None:
            await asyncio.gather(
                *(self._fallback_odds(bout, event) for bout in event.bouts if len(bout.odds) < 2),
                return_exceptions=True,
            )

    async def _fallback_odds(self, bout: Bout, event: Event) -> None:
        odds = await self.odds_fallback.odds_for(bout, around=bout.start or event.start)
        if odds:
            bout.odds = odds
            bout.odds_provider = POLYMARKET

    async def load_bout_odds(self, bout: Bout, *, ttl: int = 600) -> None:
        """Attach current moneylines to one bout. Every card ESPN lists has them, Contender Series included."""
        if not bout.odds_ref:
            return
        try:
            payload = await self.http.get_json(bout.odds_ref, ttl=ttl)
        except HttpError:
            return
        items = sorted(payload.get("items") or [], key=lambda i: (i.get("provider") or {}).get("priority", 99))
        for item in items:
            odds: dict[str, int] = {}
            for side in ("homeAthleteOdds", "awayAthleteOdds"):
                entry = item.get(side) or {}
                athlete_ref = (entry.get("athlete") or {}).get("$ref") or ""
                athlete_id = athlete_ref.split("/athletes/")[-1].split("?")[0] if "/athletes/" in athlete_ref else None
                line = entry.get("moneyLine")
                if athlete_id and isinstance(line, (int, float)):
                    odds[athlete_id] = int(line)
            if len(odds) == 2:
                bout.odds = odds
                bout.odds_provider = (item.get("provider") or {}).get("name")
                return

    async def load_status(self, bout: Bout, *, ttl: int = 30) -> None:
        """Fill the bout's live state and, once over, how it ended."""
        if not bout.status_ref:
            return
        try:
            payload = await self.http.get_json(bout.status_ref, ttl=ttl)
        except HttpError:
            return
        kind = payload.get("type") or {}
        bout.state = kind.get("state")
        bout.period = payload.get("period") if isinstance(payload.get("period"), int) else None
        bout.clock = payload.get("displayClock")
        if kind.get("completed"):
            bout.completed = True
        result = payload.get("result") or {}
        if result:
            bout.result_method = result.get("name") or result.get("displayName")
            bout.result_description = result.get("description") or result.get("displayDescription")
            bout.result_target = (result.get("target") or {}).get("name")
            if result.get("displayName") and "draw" in str(result.get("displayName")).lower():
                bout.result_method = "draw"

    async def load_plays(self, bout: Bout, *, ttl: int = 20) -> list[dict]:
        """Play-by-play for a bout: round starts and ends, knockdowns, takedowns, results."""
        if not bout.plays_ref:
            return []
        separator = "&" if "?" in bout.plays_ref else "?"
        try:
            payload = await self.http.get_json(f"{bout.plays_ref}{separator}limit=300", ttl=ttl)
        except HttpError:
            return []
        plays = [
            {
                "id": str(item.get("id")),
                "sequence": int(item.get("sequenceNumber") or 0),
                "type": (item.get("type") or {}).get("text") or "",
                "period": (item.get("period") or {}).get("number") or 0,
                "clock": (item.get("clock") or {}).get("displayValue"),
                "wallclock": parse_api_datetime(item.get("wallclock")),
            }
            for item in payload.get("items") or []
        ]
        plays.sort(key=lambda play: play["sequence"])
        return plays

    async def load_fight_stats(self, bout: Bout, *, ttl: int = 20) -> dict[str, dict[str, float]]:
        """Cumulative statistics for each fighter in this bout, keyed by athlete id."""
        async def one(athlete_id: str, ref: str) -> tuple[str, dict[str, float]]:
            try:
                payload = await self.http.get_json(ref, ttl=ttl)
            except HttpError:
                return athlete_id, {}
            values: dict[str, float] = {}
            for category in (payload.get("splits") or {}).get("categories") or []:
                for stat in category.get("stats") or []:
                    name = stat.get("name")
                    if not name:
                        continue
                    value = stat.get("value")
                    display = str(stat.get("displayValue") or "")
                    if name == "timeInControl" and ":" in display:
                        minutes, _, seconds = display.partition(":")
                        try:
                            value = int(minutes) * 60 + int(seconds)
                        except ValueError:
                            value = None
                    if isinstance(value, (int, float)):
                        values[name] = float(value)
            return athlete_id, values

        results = await asyncio.gather(
            *(one(athlete_id, ref) for athlete_id, ref in bout.stats_refs.items()),
            return_exceptions=True,
        )
        return {aid: vals for aid, vals in (r for r in results if isinstance(r, tuple)) if vals}

    async def load_scorecards(self, bout: Bout, *, ttl: int = 60) -> dict[str, list[int]]:
        """Judges' totals for each fighter, in judge order. Empty until a decision is read."""
        async def one(athlete_id: str, ref: str) -> tuple[str, list[int]]:
            try:
                payload = await self.http.get_json(ref, ttl=ttl)
            except HttpError:
                return athlete_id, []
            for item in payload.get("items") or []:
                judges = sorted(item.get("linescores") or [], key=lambda j: j.get("order") or 0)
                scores = [int(j["value"]) for j in judges if isinstance(j.get("value"), (int, float))]
                if scores:
                    return athlete_id, scores
            return athlete_id, []

        results = await asyncio.gather(
            *(one(athlete_id, ref) for athlete_id, ref in bout.linescore_refs.items()),
            return_exceptions=True,
        )
        return {aid: s for aid, s in (r for r in results if isinstance(r, tuple)) if s}

    # -- fighters -----------------------------------------------------------

    async def _load_record(self, ref: str | None) -> str | None:
        if not ref:
            return None
        try:
            payload = await self.http.get_json(ref, ttl=STATIC_TTL)
        except HttpError:
            return None
        for item in payload.get("items", []):
            if item.get("type") == "total" or item.get("name") == "overall":
                return item.get("summary") or item.get("displayValue")
        items = payload.get("items") or []
        return (items[0].get("summary") if items else None) or None

    async def get_fighter(self, athlete_id: str) -> Fighter | None:
        """Full profile for one athlete, including their record."""
        try:
            payload = await self.http.get_json(f"{CORE}/athletes/{athlete_id}", ttl=STATIC_TTL)
        except HttpError:
            return None
        fighter = _fighter_from_payload(payload)
        if fighter is None:
            return None
        fighter.record = await self._load_record(f"{CORE}/athletes/{athlete_id}/records")
        return fighter

    async def search_fighters(self, query: str, *, limit: int = 5) -> list[Fighter]:
        """Search fighters by name. Returns lightweight stubs, id plus display name."""
        query = query.strip()
        if not query:
            return []
        url = f"{SEARCH}?query={quote(query, safe='')}&limit=20&sport=mma"
        try:
            payload = await self.http.get_json(url)
        except HttpError as exc:
            log.warning("Fighter search failed for %r: %r", query, exc)
            return []

        results: list[Fighter] = []
        seen: set[str] = set()
        for group in payload.get("results", []):
            if group.get("type") != "player":
                continue
            for entry in group.get("contents", []):
                if entry.get("sport") != "mma":
                    continue
                athlete_id = _athlete_id_from_uid(entry.get("uid"))
                if not athlete_id or athlete_id in seen:
                    continue
                seen.add(athlete_id)
                results.append(
                    Fighter(
                        id=athlete_id,
                        display_name=entry.get("displayName") or "Unknown",
                        profile_url=(entry.get("link") or {}).get("web"),
                    )
                )
                if len(results) >= limit:
                    return results
        return results


# -- module helpers ---------------------------------------------------------


def _athlete_id_from_uid(uid: str | None) -> str | None:
    """ESPN uids look like ``s:3301~a:2335639``; the athlete id is the ``a:`` part."""
    if not uid:
        return None
    for part in uid.split("~"):
        if part.startswith("a:"):
            return part[2:]
    return None


def _fighter_from_payload(payload: dict) -> Fighter | None:
    if not payload.get("id"):
        return None
    return Fighter(
        id=str(payload["id"]),
        display_name=payload.get("displayName") or payload.get("fullName") or "Unknown",
        nickname=payload.get("nickname") or None,
        weight_class=(payload.get("weightClass") or {}).get("text"),
        height=payload.get("displayHeight"),
        reach=payload.get("displayReach"),
        stance=(payload.get("stance") or {}).get("text"),
        age=payload.get("age") if isinstance(payload.get("age"), int) else None,
        citizenship=payload.get("citizenship"),
        headshot_url=(payload.get("headshot") or {}).get("href"),
        profile_url=_first_web_link(payload.get("links")),
    )


def _first_web_link(links: list | None) -> str | None:
    for link in links or []:
        href = link.get("href")
        if href and href.startswith("http") and "desktop" in (link.get("rel") or []):
            return href
    for link in links or []:
        href = link.get("href")
        if href and href.startswith("http"):
            return href
    return None


def _venue_fields(payload: dict) -> tuple[str | None, str | None, str | None]:
    """Split a venue document into (name, city, country), folding state into the city."""
    address = payload.get("address") or {}
    city = address.get("city")
    state = address.get("state")
    if city and state:
        city = f"{city}, {state}"
    return payload.get("fullName"), city, address.get("country")


def _venue_from_competitions(
    competitions: list[dict],
) -> tuple[str | None, str | None, str | None] | None:
    """Read the venue straight off a fight, avoiding a separate venue lookup."""
    for competition in competitions:
        venue = competition.get("venue")
        if isinstance(venue, dict) and venue.get("fullName"):
            return _venue_fields(venue)
    return None
