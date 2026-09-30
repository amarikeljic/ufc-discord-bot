"""The headshot cache, which is the only thing the bot holds in megabytes.

Two faces a card, fetched once and kept while the card is on. What matters is
that it lets go of them afterwards, since nothing else it holds is this big.
"""

from __future__ import annotations

import time

from ufcbot.embeds.images import HEADSHOT_TTL, MatchupImages


def images_with(urls_to_bytes: dict[str, bytes], **kwargs) -> MatchupImages:
    """A headshot cache whose downloads are already decided."""
    images = MatchupImages(http=None, **kwargs)
    for url, data in urls_to_bytes.items():
        images._cache[url] = (time.monotonic(), data)
        images._held += len(data)
    return images


def test_headshots_a_card_is_using_are_not_dropped():
    """Every face on a card is read twice, at the walkout and at the result, so
    a cache that evicted between the two would fetch the whole card again."""
    card = {f"f{i}": b"\x89PNG" + bytes(200_000) for i in range(21)}
    images = images_with(card)

    images._expire(time.monotonic())

    assert len(images._cache) == 21, "4.2 MB of a 6 MB cap"


def test_headshots_nobody_has_looked_at_for_hours_are_dropped():
    """A card is four hours and the next one is a fortnight away. Holding the
    faces in between costs megabytes to save nothing."""
    images = images_with({"old": b"\x89PNG" + bytes(200_000)})
    images._cache["old"] = (time.monotonic() - HEADSHOT_TTL - 1, images._cache["old"][1])

    images._expire(time.monotonic())

    assert images._cache == {} and images._held == 0


def test_the_cap_still_bites_when_everything_is_fresh():
    images = images_with({f"f{i}": b"\x89PNG" + bytes(500_000) for i in range(20)})

    images._expire(time.monotonic())

    assert images._held <= 6 * 1024 * 1024
    assert len(images._cache) < 20, "the least recently read went first"
