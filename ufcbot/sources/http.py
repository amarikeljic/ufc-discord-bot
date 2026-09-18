"""A small aiohttp wrapper: one shared session, in-memory TTL cache, retries, concurrency cap."""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections import OrderedDict
from typing import Any

import aiohttp

log = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


class HttpError(Exception):
    """Raised when a request fails after all retries."""


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
    ) -> None:
        self._cache_ttl = cache_ttl
        self._max_entries = max_entries
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: aiohttp.ClientSession | None = None
        # Ordered by how recently each entry was read, oldest first, so the cache
        # can be kept to a size worth holding without dropping what is in use.
        # Entries are (stored at, longest TTL asked for, payload).
        #
        # The default leaves room for what a board pass actually touches. Each
        # fighter on a card costs two documents, a profile and a record, both held
        # for a day, so a dozen upcoming cards is already several hundred entries;
        # a cap below that would evict them between passes and fetch them again.
        self._cache: OrderedDict[str, tuple[float, int, Any]] = OrderedDict()
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

        cached = self._cache.get(url)
        if cached is not None and now - cached[0] < ttl:
            self._cache.move_to_end(url)
            return cached[2]

        existing = self._inflight.get(url)
        if existing is not None:
            return await asyncio.shield(existing)

        future: asyncio.Future = asyncio.get_running_loop().create_future()
        # Nothing awaits this future when no other caller joined, so retrieve any
        # exception to keep asyncio from warning about it.
        future.add_done_callback(lambda f: f.cancelled() or f.exception())
        self._inflight[url] = future
        try:
            payload = await self._fetch_json(url)
        except Exception as exc:  # propagated to every waiter
            if not future.done():
                future.set_exception(exc)
            raise
        else:
            self._store(url, payload, ttl)
            if not future.done():
                future.set_result(payload)
            return payload
        finally:
            self._inflight.pop(url, None)

    def _store(self, url: str, payload: Any, ttl: int) -> None:
        """Cache a response and keep the cache to its size.

        A URL can be asked for with different lifetimes -- live coverage wants a
        fight card seconds old where a board is happy with fifteen minutes -- so
        the longest lifetime asked for decides when the entry is swept, while
        each caller still compares against its own.
        """
        previous = self._cache.get(url)
        longest = max(ttl, previous[1]) if previous else ttl
        self._cache[url] = (time.monotonic(), longest, payload)
        self._cache.move_to_end(url)
        if len(self._cache) > self._max_entries:
            self._trim()

    def _trim(self) -> None:
        """Drop expired entries, then the least recently read, until within size.

        Fighter profiles are held for a day each, so a long-running bot would
        otherwise keep every athlete it has ever looked at.
        """
        now = time.monotonic()
        for key in [k for k, (at, ttl, _) in self._cache.items() if now - at >= ttl]:
            del self._cache[key]
        while len(self._cache) > self._max_entries:
            self._cache.popitem(last=False)

    async def _fetch_json(self, url: str, attempts: int = 3) -> Any:
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
                    # Several ESPN hosts return JSON under a text/plain content type.
                    return await response.json(content_type=None)
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
