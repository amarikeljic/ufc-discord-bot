"""Text for the Discord scheduled events that mirror each card.

Discord gives these three fields and no formatting, so they are plain strings
rather than embeds, and every one of them has a hard length limit.
"""

from __future__ import annotations

from ..models import Event
from ..util import truncate

SCHEDULED_EVENT_NAME_LIMIT = 100

SCHEDULED_EVENT_DESCRIPTION_LIMIT = 1000

SCHEDULED_EVENT_LOCATION_LIMIT = 100

def scheduled_event_name(event: Event) -> str:
    return truncate(event.name, SCHEDULED_EVENT_NAME_LIMIT)

def scheduled_event_location(event: Event) -> str:
    """Where the card is, as a city rather than a building: the arena name tells
    nobody anything they cannot get from the city."""
    return truncate(event.short_location, SCHEDULED_EVENT_LOCATION_LIMIT)

def scheduled_event_description(event: Event) -> str:
    """Main card summary, trimmed to Discord's 1000 character limit.

    The card and nothing else. It used to carry the model's pick beside each
    bout, which put a prediction somewhere nobody went looking for one and
    left it there: a calendar entry is read weeks out and never again, so the
    pick it showed was the one from whenever the event was created."""
    lines: list[str] = []

    main_card = next((bouts for _segment, bouts in event.bouts_by_segment() if bouts), [])
    if main_card:
        lines.append("Main card:")
        for bout in main_card:
            entry = f"• {bout.matchup}"
            if bout.weight_class:
                entry += f" ({bout.weight_class})"
            if len("\n".join(lines)) + len(entry) + 1 > SCHEDULED_EVENT_DESCRIPTION_LIMIT - 80:
                lines.append("• …")
                break
            lines.append(entry)

    if event.espn_url:
        lines.append("")
        lines.append(event.espn_url)

    return truncate("\n".join(lines).strip(), SCHEDULED_EVENT_DESCRIPTION_LIMIT)
