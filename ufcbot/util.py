"""Small shared helpers."""

from __future__ import annotations

import logging
import os
import re
import sys
import unicodedata
from datetime import UTC, datetime, timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

log = logging.getLogger(__name__)

_TRAILING_OFFSET = re.compile(r"([+-]\d{2})(\d{2})$")

# Where the daily jobs are scheduled from. Not a display setting: everything a
# member reads is a Discord timestamp, shown in their own timezone.
CENTRAL = "America/Chicago"


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


def format_duration(seconds: float) -> str:
    """A span of time in the two largest units that matter: "3d 4h", "12m"."""
    minutes = int(seconds // 60)
    days, minutes = divmod(minutes, 1440)
    hours, minutes = divmod(minutes, 60)
    parts = [f"{days}d", f"{hours}h", f"{minutes}m"]
    if not days:
        parts.pop(0)
        if not hours:
            parts.pop(0)
    return " ".join(parts[:2])


def resident_memory_mb() -> float | None:
    """How much memory this process holds, or None where the platform will not say.

    Asked of the OS directly rather than adding psutil for one number.
    """
    if sys.platform == "win32":
        return _windows_working_set_mb()
    try:
        with open("/proc/self/statm") as handle:
            pages = int(handle.read().split()[1])
    except (OSError, IndexError, ValueError):
        return None
    return pages * os.sysconf("SC_PAGE_SIZE") / (1024 * 1024)


def _windows_working_set_mb() -> float | None:
    import ctypes
    from ctypes import wintypes

    class Counters(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("PageFaultCount", wintypes.DWORD),
            *(
                (name, ctypes.c_size_t)
                for name in (
                    "PeakWorkingSetSize",
                    "WorkingSetSize",
                    "QuotaPeakPagedPoolUsage",
                    "QuotaPagedPoolUsage",
                    "QuotaPeakNonPagedPoolUsage",
                    "QuotaNonPagedPoolUsage",
                    "PagefileUsage",
                    "PeakPagefileUsage",
                )
            ),
        ]

    try:
        kernel32 = ctypes.WinDLL("kernel32")
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        read = kernel32.K32GetProcessMemoryInfo
        read.restype = wintypes.BOOL
        read.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]

        counters = Counters()
        counters.cb = ctypes.sizeof(Counters)
        ok = read(kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb)
    except (OSError, AttributeError):
        return None
    return counters.WorkingSetSize / (1024 * 1024) if ok else None


def central_time() -> tzinfo:
    """US Central time, following daylight saving.

    Falls back to a fixed CST offset where the machine has no timezone database,
    which puts the nightly jobs an hour out over the summer but never stops the
    bot from starting over it.
    """
    try:
        return ZoneInfo(CENTRAL)
    except (ZoneInfoNotFoundError, OSError):
        log.warning("No timezone database for %s; falling back to a fixed CST offset", CENTRAL)
        return timezone(timedelta(hours=-6), "CST")
