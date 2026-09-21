"""A small aiohttp wrapper: one shared session, in-memory TTL cache, retries, concurrency cap."""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from collections import OrderedDict
from typing import Any, NamedTuple

import aiohttp

log = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


class HttpError(Exception):
    """Raised when a request fails after all retries."""


# Nothing can read an entry past its lifetime, so they are dropped on a timer
# and not only when the cache fills. A card's play-by-play is cached for ten
# seconds and a board's for fifteen minutes; without this, a quiet bot holds
# hundreds of parsed payloads that no caller will ever be given again.
SWEEP_INTERVAL = 120

# The cache is capped by the weight of what it holds as well as the number of
# things it holds, because those two are barely related. A pass over a full
# schedule reads about 300 documents and 1.3MB of JSON, but parsed into Python
# objects that costs roughly eight times its own size in memory -- a few hundred
# thousand small dicts and strings. A cap counted only in entries therefore lets
# the cache grow to several times the working set it exists to serve, which is
# most of the difference between a bot that sits at 70MB and one that drifts
# past 190MB after a day. Responses are measured as they arrive, which is free,
# and the budget leaves ample room for that working set.
MAX_BYTES = 4 * 1024 * 1024

# ESPN sends these on its ``$ref`` links and not on the URLs built by hand, so
# the same document arrives under two spellings. They do not change the
# response, so they do not belong in the key: without this the largest documents
# of all, the event cards, are held twice.
IGNORED_QUERY = ("lang", "region")


def cache_key(url: str) -> str:
    """The URL with the parameters that do not change the response removed."""
    base, separator, query = url.partition("?")
    if not separator:
        return url
    kept = [
        part
        for part in query.split("&")
        if part and part.split("=", 1)[0] not in IGNORED_QUERY
    ]
    return f"{base}?{'&'.join(kept)}" if kept else base


class Entry(NamedTuple):
    """One cached response: when it arrived, how long it may be served, and its weight."""

    stored_at: float
    ttl: int
    payload: Any
    size: int


class HttpClient:
    """Shared HTTP client.

    Upstream endpoints are read-only and the same URLs get requested repeatedly
    (an athlete appears on many cards), so responses are cached by URL and
    concurrent requests are capped to stay polite.
    """

    def __init__(
        self,
        *,
        cache_ttl: int = 900,
        max_concurrency: int = 8,
        timeout: int = 20,
        max_entries: int = 1500,
        max_bytes: int = MAX_BYTES,
    ) -> None:
        self._cache_ttl = cache_ttl
        self._max_entries = max_entries
        self._max_bytes = max_bytes
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: aiohttp.ClientSession | None = None
        # Ordered by how recently each entry was read, oldest first, so the cache
        # can be kept to a size worth holding without dropping what is in use.
        #
        # The default leaves room for what a board pass actually touches. Each
        # fighter on a card costs two documents, a profile and a record, both held
        # for a day, so a dozen upcoming cards is already several hundred entries;
        # a cap below that would evict them between passes and fetch them again.
        self._cache: OrderedDict[str, Entry] = OrderedDict()
        self._held = 0
        self._last_sweep = time.monotonic()
        self._inflight: dict[str, asyncio.Future] = {}

    async def start(self) -> None:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=self._timeout,
                headers={
                    "User-Agent": USER_AGENT,
                    "Accept": "application/json, text/plain, */*",
                    "Accept-Language": "en-US,en;q=0.9",
                },
            )

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    def _session_or_raise(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            raise HttpError("HTTP client is not started")
        return self._session

    async def get_json(self, url: str, *, ttl: int | None = None) -> Any:
        """GET a URL and parse JSON, served from cache when fresh.

        Requests for the same URL that overlap in time share one round trip.
        """
        ttl = self._cache_ttl if ttl is None else ttl
        now = time.monotonic()
        key = cache_key(url)

        cached = self._cache.get(key)
        if cached is not None and now - cached.stored_at < ttl:
            self._cache.move_to_end(key)
            return cached.payload

        existing = self._inflight.get(key)
        if existing is not None:
            return await asyncio.shield(existing)

        future: asyncio.Future = asyncio.get_running_loop().create_future()
        # Nothing awaits this future when no other caller joined, so retrieve any
        # exception to keep asyncio from warning about it.
        future.add_done_callback(lambda f: f.cancelled() or f.exception())
        self._inflight[key] = future
        try:
            payload, size = await self._fetch_json(url)
        except Exception as exc:  # propagated to every waiter
            if not future.done():
                future.set_exception(exc)
            raise
        else:
            self._store(key, payload, ttl, size)
            if not future.done():
                future.set_result(payload)
            return payload
        finally:
            self._inflight.pop(key, None)

    def _store(self, key: str, payload: Any, ttl: int, size: int) -> None:
        """Cache a response and keep the cache to its size.

        A URL can be asked for with different lifetimes -- live coverage wants a
        fight card seconds old where a board is happy with fifteen minutes -- so
        the longest lifetime asked for decides when the entry is swept, while
        each caller still compares against its own.
        """
        previous = self._cache.get(key)
        longest = max(ttl, previous.ttl) if previous else ttl
        if previous is not None:
            self._held -= previous.size
        now = time.monotonic()
        self._cache[key] = Entry(now, longest, payload, size)
        self._held += size
        self._cache.move_to_end(key)
        if (
            len(self._cache) > self._max_entries
            or self._held > self._max_bytes
            or now - self._last_sweep >= SWEEP_INTERVAL
        ):
            self._trim()

    def _trim(self) -> None:
        """Drop expired entries, then the least recently read, until within both caps.

        Fighter profiles are held for a day each, so a long-running bot would
        otherwise keep every athlete it has ever looked at.
        """
        now = time.monotonic()
        self._last_sweep = now
        for key in [k for k, entry in self._cache.items() if now - entry.stored_at >= entry.ttl]:
            self._held -= self._cache.pop(key).size
        while self._cache and (len(self._cache) > self._max_entries or self._held > self._max_bytes):
            self._held -= self._cache.popitem(last=False)[1].size

    async def _fetch_json(self, url: str, attempts: int = 3) -> tuple[Any, int]:
        """The parsed response and the size of the body it came from.

        The body is read before it is parsed, which is what ``response.json``
        does anyway, so its length costs nothing and is a far better measure of
        what the entry will weigh than counting it as one of anything.
        """
        session = self._session_or_raise()
        last_error: Exception | None = None

        for attempt in range(attempts):
            if attempt:
                # Back off with jitter so a burst of failures does not retry in lockstep.
                await asyncio.sleep(min(8.0, 0.5 * 2**attempt) + random.uniform(0, 0.3))
            try:
                async with self._semaphore, session.get(url) as response:
                    if response.status == 404:
                        raise HttpError(f"404 Not Found: {url}")
                    if response.status in (429, 500, 502, 503, 504):
                        last_error = HttpError(f"{response.status} from {url}")
                        log.debug("Retryable %s from %s", response.status, url)
                        continue
                    response.raise_for_status()
                    # Several ESPN hosts return JSON under a text/plain content type,
                    # so the body is parsed directly rather than by content type.
                    body = await response.read()
                    return json.loads(body), len(body)
            except HttpError:
                raise
            except (TimeoutError, aiohttp.ClientError) as exc:
                last_error = exc
                log.debug("Request error for %s: %r", url, exc)

        raise HttpError(f"Failed to fetch {url}: {last_error}")

    async def get_bytes(self, url: str, *, max_bytes: int = 8_000_000) -> bytes | None:
        """Download a binary asset. Returns None instead of raising, since assets are optional."""
        try:
            session = self._session_or_raise()
            async with self._semaphore, session.get(url) as response:
                if response.status != 200:
                    return None
                length = response.content_length
                if length is not None and length > max_bytes:
                    return None
                # read() waits for the whole body; a single chunk read can return a partial image.
                data = await response.read()
                if len(data) > max_bytes:
                    return None
                if length is not None and len(data) != length:
                    return None
                return data
        except (TimeoutError, aiohttp.ClientError, HttpError) as exc:
            log.debug("Asset download failed for %s: %r", url, exc)
            return None
