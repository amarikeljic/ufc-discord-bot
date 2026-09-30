"""The HTTP client: what it keeps, and how it tells an outage from a bad afternoon.

ESPN drops connections and returns 500s most days, a fighter who has no page is
a 404 for ever, and a bot with no card to look at makes almost no requests. Each
of those looks like silence if you squint, and a warning that fires on any of
them is a warning nobody reads. So nearly all of this is about the cases where
the bot must say nothing.
"""

from __future__ import annotations

import time
from datetime import timedelta

import pytest

from ufcbot.sources.http import (
    OUTAGE_AFTER,
    OUTAGE_ATTEMPTS,
    SWEEP_INTERVAL,
    Entry,
    HostHealth,
    HttpClient,
    HttpError,
    cache_key,
)

ESPN = "sports.core.api.espn.com"
ODDS = "gamma-api.polymarket.com"
URL = f"https://{ESPN}/v2/sports/mma/leagues/ufc/events/1"


def failing(client: HttpClient, host: str, *, times: int, hours: float) -> None:
    """A host that has answered nothing for this long, over this many attempts."""
    health = client._health.setdefault(host, HostHealth())
    for _ in range(times):
        health.failed("Cannot connect to host")
    health.failing_since = time.monotonic() - hours * 3600


# -- when it must stay quiet ---------------------------------------------------------


def test_a_host_that_has_never_failed_is_not_an_outage():
    assert HttpClient().outage(ESPN) is None


def test_one_bad_afternoon_is_not_an_outage():
    """Long enough, but the bot barely tried: it was idle between cards."""
    client = HttpClient()
    failing(client, ESPN, times=OUTAGE_ATTEMPTS - 1, hours=12)

    assert client.outage(ESPN) is None


def test_a_burst_of_failures_over_minutes_is_not_an_outage():
    """A card pass fires hundreds of requests. All of them failing inside a
    minute is one bad minute, not a dead API."""
    client = HttpClient()
    failing(client, ESPN, times=500, hours=0.2)

    assert client.outage(ESPN) is None


def test_one_answer_clears_everything():
    """Any answer at all means the far end is there. A single success has to
    reset the clock, or a flaky hour would eventually accumulate into a warning."""
    client = HttpClient()
    failing(client, ESPN, times=500, hours=12)

    client._note(URL, answered=True)

    assert client.outage(ESPN) is None
    health = client._health[ESPN]
    assert health.failures == 0
    assert health.failing_since is None and health.failing_at is None, "the clock restarts too"


class FakeResponse:
    def __init__(self, status: int, body: bytes = b"{}") -> None:
        self.status, self._body = status, body

    async def read(self) -> bytes:
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc) -> None:
        return None


class FakeSession:
    """Answers every request with one status, however many times it is asked."""

    closed = False

    def __init__(self, status: int) -> None:
        self.status, self.calls = status, 0

    def get(self, url: str):
        self.calls += 1
        return FakeResponse(self.status)


async def fetch(status: int) -> HttpError:
    """Ask a host that always answers with this status; return what was raised."""
    client = HttpClient()
    client._session = FakeSession(status)
    with pytest.raises(HttpError) as raised:
        await client._fetch_json(URL, attempts=1)
    return raised.value


async def test_a_fighter_with_no_page_is_the_server_working():
    """A 404 is a failed request and a healthy host. Counting it as silence
    would have the bot declare an outage over a retired fighter with no profile,
    which it looks up constantly."""
    assert (await fetch(404)).answered is True


async def test_a_refusal_is_also_the_server_working():
    """403 and 401 mean the far end is there and has said no."""
    assert (await fetch(403)).answered is True
    assert (await fetch(401)).answered is True


async def test_a_server_that_breaks_or_never_replies_is_silence():
    """500s that survive every retry, and a connection that never opens, are the
    two things an outage actually looks like."""
    assert (await fetch(503)).answered is False

    client = HttpClient()
    client._session = None  # never started: the request cannot even be made
    with pytest.raises(HttpError):
        await client._fetch_json(URL, attempts=1)


async def test_a_404_leaves_the_host_counted_as_healthy():
    """The whole path, not just the flag: a 404 through get_json must clear the
    failure run rather than add to it."""
    client = HttpClient()
    client._session = FakeSession(404)
    failing(client, ESPN, times=OUTAGE_ATTEMPTS * 2, hours=12)

    with pytest.raises(HttpError):
        await client.get_json(URL)

    assert client.outage(ESPN) is None
    assert client.answering(ESPN) is True


def test_another_host_failing_is_not_this_one_failing():
    client = HttpClient()
    failing(client, ODDS, times=500, hours=12)

    assert client.outage(ESPN) is None


# -- when it must speak up -------------------------------------------------------------


def test_hours_of_silence_over_many_attempts_is_an_outage():
    client = HttpClient()
    failing(client, ESPN, times=OUTAGE_ATTEMPTS, hours=OUTAGE_AFTER.total_seconds() / 3600 + 1)

    outage = client.outage(ESPN)

    assert outage is not None
    assert outage.host == ESPN
    assert outage.failures == OUTAGE_ATTEMPTS
    assert outage.hours == pytest.approx(OUTAGE_AFTER.total_seconds() / 3600 + 1, abs=0.1)
    assert outage.last_error == "Cannot connect to host"


def test_the_outage_is_dated_from_the_first_failure_not_the_last():
    """So the warning says how long it has been going, and so the same outage
    keeps the same identity and is only announced once."""
    client = HttpClient()
    health = client._health.setdefault(ESPN, HostHealth())
    health.failed("first")
    first = health.failing_at
    for _ in range(OUTAGE_ATTEMPTS):
        health.failed("later")
    health.failing_since = time.monotonic() - OUTAGE_AFTER.total_seconds() - 60

    assert client.outage(ESPN).since == first


def test_the_thresholds_can_be_tightened_for_a_caller_that_wants_to_know_sooner():
    client = HttpClient()
    failing(client, ESPN, times=5, hours=1)

    assert client.outage(ESPN) is None
    assert client.outage(ESPN, after=timedelta(minutes=30), attempts=3) is not None


# -- the control host ------------------------------------------------------------------


def test_a_second_host_answering_says_the_trouble_is_not_here():
    client = HttpClient()
    client._note(f"https://{ODDS}/events", answered=True)
    failing(client, ESPN, times=OUTAGE_ATTEMPTS, hours=12)

    assert client.answering(ODDS) is True
    assert client.answering(ESPN) is False


def test_a_host_nobody_has_called_is_not_claimed_to_be_answering():
    assert HttpClient().answering(ODDS) is False


# -- what the warning says --------------------------------------------------------------


def embed_for(*, odds_working: bool):
    from ufcbot.embeds import api_outage_embed

    client = HttpClient()
    failing(client, ESPN, times=143, hours=5)
    return api_outage_embed(client.outage(ESPN), odds_working=odds_working)


def test_the_warning_says_how_long_and_how_many_and_what_still_works():
    embed = embed_for(odds_working=True)
    text = embed.description + " ".join(f.value for f in embed.fields)

    assert "5 hours" in text and "143" in text
    assert "Pick'em" in text and "Ratings" in text
    assert "ESPN rather than the bot's connection" in text


def test_the_warning_admits_when_it_might_be_this_machine():
    text = " ".join(f.value for f in embed_for(odds_working=False).fields)

    assert "may be the bot's own connection" in text


def test_the_all_clear_says_how_long_it_lasted():
    from ufcbot.embeds import api_restored_embed

    assert "5 hours" in api_restored_embed(5.0).description


# -- what the cache keeps ---------------------------------------------------------


def held(client: HttpClient, key: str, *, age: float = 0.0, ttl: int = 86400, size: int = 100) -> None:
    client._cache[key] = Entry(time.monotonic() - age, ttl, {"payload": key}, size)
    client._held += size


def test_expired_responses_are_dropped_on_a_timer_not_only_when_the_cache_fills():
    """A quiet bot never reaches either cap, so nothing would evict what has gone
    stale: hundreds of parsed payloads no caller can be given again."""
    client = HttpClient()
    held(client, "stale", age=3600, ttl=10)
    held(client, "fresh")

    client._last_sweep = time.monotonic() - SWEEP_INTERVAL - 1
    client._store("new", {"a": 1}, ttl=900, size=10)

    assert "stale" not in client._cache, "past its lifetime and unreadable"
    assert set(client._cache) == {"fresh", "new"}


def test_a_sweep_that_has_just_run_is_not_run_again():
    client = HttpClient()
    held(client, "stale", age=3600, ttl=10)

    client._last_sweep = time.monotonic()
    client._store("new", {"a": 1}, ttl=900, size=10)

    assert "stale" in client._cache, "swept at most once every SWEEP_INTERVAL"


def test_a_heavy_cache_is_trimmed_even_when_it_holds_few_things():
    """Entries vary from a couple of kilobytes to well over a hundred, so a cap
    counted in entries alone says almost nothing about what is being held."""
    client = HttpClient(max_bytes=1000)
    for index in range(5):
        held(client, f"old{index}", size=300)

    client._store("new", {"a": 1}, ttl=900, size=300)

    assert client._held <= 1000
    assert "new" in client._cache, "what just arrived is what is wanted"
    assert "old0" not in client._cache, "the least recently read goes first"


def test_the_weight_of_a_replaced_entry_is_not_counted_twice():
    client = HttpClient()
    client._store("same", {"a": 1}, ttl=900, size=500)
    client._store("same", {"a": 2}, ttl=900, size=700)

    assert client._held == 700


def test_espn_link_parameters_that_change_nothing_share_one_entry():
    """ESPN sends lang and region on its $ref links but not on URLs built by
    hand, and the response is identical. Keyed separately, the biggest documents
    of all are held twice."""
    assert cache_key("https://x/events/1?lang=en&region=us") == "https://x/events/1"
    assert cache_key("https://x/events/1") == "https://x/events/1"
    # Anything that does change the response stays in the key.
    assert cache_key("https://x/plays?limit=300&lang=en") == "https://x/plays?limit=300"



def test_a_response_can_be_read_without_being_kept():
    """A caller that distils a document into something smaller has no use for
    the document. Fighter profiles are three kilobytes each to fill in ten
    fields, and thousands of them held raw is the long tail of the cache."""
    client = HttpClient()
    client._store("kept", {"a": 1}, ttl=900, size=3000)

    assert client._held == 3000
    assert "kept" in client._cache
    # store=False is exercised through get_json, which needs a session; what is
    # pinned here is that _store is the only thing that ever grows the cache.
    assert set(client._cache) == {"kept"}


def test_the_cache_is_held_to_its_weight_not_its_count():
    client = HttpClient(max_bytes=10_000, max_entries=1000)
    for index in range(10):
        held(client, f"doc{index}", size=2_000)

    client._store("new", {"a": 1}, ttl=900, size=2_000)

    assert client._held <= 10_000
    assert len(client._cache) < 11, "well under the entry cap, but over the weight"


