"""Small shared helpers."""

from __future__ import annotations

import re
import unicodedata
from datetime import UTC, datetime

_TRAILING_OFFSET = re.compile(r"([+-]\d{2})(\d{2})$")


def parse_api_datetime(value: str | None) -> datetime | None:
    """Parse the timestamp formats the upstream APIs emit, always returning UTC.

    ESPN uses ``2026-02-28T22:00Z`` (no seconds), TheSportsDB uses
    ``2026-09-12T18:00:00``. Both are handled here.
    """
    if not value:
        return None

    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"

    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        parsed = None
        normalised = _TRAILING_OFFSET.sub(r"\1:\2", text)
        for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M%z", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
            try:
                parsed = datetime.strptime(normalised, fmt)
                break
            except ValueError:
                continue
        if parsed is None:
            return None

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def https(url: str | None) -> str | None:
    """ESPN returns ``$ref`` links over plain HTTP; upgrade them."""
    if not url:
        return None
    if url.startswith("http://"):
        return "https://" + url[len("http://") :]
    return url


def normalise(text: str) -> str:
    """Casefold and strip accents so ``Chairez`` matches ``Cháirez``."""
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9 ]+", " ", stripped.casefold()).strip()


def truncate(text: str, limit: int) -> str:
    """Cut a string to ``limit`` characters, ending with an ellipsis if cut."""
    if len(text) <= limit:
        return text
    if limit <= 1:
        return text[:limit]
    return text[: limit - 1].rstrip() + "…"


def format_odds(line: int) -> str:
    """American moneyline with its sign: 250 -> "+250", -130 -> "-130"."""
    return f"+{line}" if line > 0 else str(line)
