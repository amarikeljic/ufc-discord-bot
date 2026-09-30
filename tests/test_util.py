"""The small shared helpers, tested where they are used or not at all."""

from __future__ import annotations

from ufcbot.util import format_duration


def test_an_uptime_reads_in_the_two_units_that_matter():
    assert format_duration(30) == "0m"
    assert format_duration(90 * 60) == "1h 30m"
    assert format_duration(26 * 3600 + 5 * 60) == "1d 2h", "days and hours, not minutes too"

