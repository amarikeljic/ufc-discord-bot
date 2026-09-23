"""Telling an outage from a bad afternoon.

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
    HostHealth,
    HttpClient,
    HttpError,
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
