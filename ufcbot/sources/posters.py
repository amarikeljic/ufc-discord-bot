"""Optional event poster art, sourced from TheSportsDB.

ESPN does not publish card posters, so this fills that gap. Everything here is
best-effort: a miss returns ``None`` and the caller carries on without art.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from ..models import Event
from ..util import normalise
from .http import HttpClient, HttpError

log = logging.getLogger(__name__)

API = "https://www.thesportsdb.com/api/v1/json/3"
UFC_LEAGUE_ID = "4443"
POSTER_TTL = 12 * 60 * 60

# Words that appear in nearly every card name and so say nothing about which one it is.
STOPWORDS = {"ufc", "fight", "night", "vs", "noche", "on", "the", "espn", "abc", "fox"}


class PosterLookup:
    def __init__(self, http: HttpClient) -> None:
        self.http = http

    async def poster_url(self, event: Event) -> str | None:
        """Find poster art for a card, matching on date and then on name."""
        # The card can land on the previous day in local time, so check either side.
        dates = {
            (event.start + timedelta(days=offset)).strftime("%Y-%m-%d") for offset in (0, -1)
        }

        best: tuple[float, str] | None = None
        for date in sorted(dates):
            for candidate in await self._events_on(date):
                if candidate.get("idLeague") != UFC_LEAGUE_ID:
                    continue
                poster = (candidate.get("strPoster") or "").strip()
                if not poster:
                    continue
                score = _name_similarity(event.name, candidate.get("strEvent") or "")
                if score >= 0.34 and (best is None or score > best[0]):
                    best = (score, poster)

        if best:
            log.debug("Poster matched for %s (score %.2f)", event.name, best[0])
            return best[1]
        return None

    async def poster_bytes(self, event: Event) -> bytes | None:
        url = await self.poster_url(event)
        if not url:
            return None
        return await self.http.get_bytes(url, max_bytes=8_000_000)

    async def _events_on(self, date: str) -> list[dict]:
        try:
            payload = await self.http.get_json(
                f"{API}/eventsday.php?d={date}&s=Fighting", ttl=POSTER_TTL
            )
        except HttpError as exc:
            log.debug("Poster lookup failed for %s: %r", date, exc)
            return []
        return payload.get("events") or []


def _name_similarity(left: str, right: str) -> float:
    """Jaccard overlap of the distinctive words in two card names.

    ESPN writes "Noche UFC: Silva vs. Delgado" where TheSportsDB writes
    "UFC Fight Night 288 Silva vs Delgado", so the fighter surnames carry the match.
    """
    left_tokens = set(normalise(left).split()) - STOPWORDS
    right_tokens = set(normalise(right).split()) - STOPWORDS
    if not left_tokens or not right_tokens:
        return 0.0
    intersection = left_tokens & right_tokens
    union = left_tokens | right_tokens
    return len(intersection) / len(union)
