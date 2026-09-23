"""Plain data objects shared by the data sources and the Discord layer."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

# Card segments, ordered the way a broadcast runs them.
SEGMENT_ORDER = {
    "main card": 0,
    "main": 0,
    "prelims": 1,
    "preliminary card": 1,
    "early prelims": 2,
    "early preliminary card": 2,
}


def segment_rank(name: str | None) -> int:
    if not name:
        return 3
    return SEGMENT_ORDER.get(name.strip().lower(), 3)


@dataclass(slots=True)
class Fighter:
    """A single athlete. Populated lazily; every field except ``id`` may be missing."""

    id: str
    display_name: str
    nickname: str | None = None
    record: str | None = None
    weight_class: str | None = None
    height: str | None = None
    reach: str | None = None
    stance: str | None = None
    age: int | None = None
    citizenship: str | None = None
    headshot_url: str | None = None
    profile_url: str | None = None


@dataclass(slots=True)
class Bout:
    """One fight on a card."""

    id: str
    weight_class: str | None = None
    segment: str | None = None
    match_number: int | None = None
    rounds: int | None = None
    start: datetime | None = None
    fighters: list[Fighter] = field(default_factory=list)
    competitors: int = 0
    """How many corners the card lists, named or not.

    ``fighters`` can be shorter when a profile fails to load, and the difference
    matters: a bout with one corner has an opponent still to be announced, where
    a bout with two corners and one name is this bot not having the other yet.
    """
    winner_id: str | None = None
    completed: bool = False

    # Links to the per-fight documents, followed only when needed.
    status_ref: str | None = None
    plays_ref: str | None = None
    stats_refs: dict[str, str] = field(default_factory=dict)
    """Athlete id -> that fighter's statistics document for this bout."""
    linescore_refs: dict[str, str] = field(default_factory=dict)
    """Athlete id -> judges' scores for that fighter."""

    odds: dict[str, int] = field(default_factory=dict)
    """Athlete id -> American moneyline, when a sportsbook has posted one."""
    odds_provider: str | None = None

    # Filled by UFCData.load_status.
    state: str | None = None
    """"pre", "in" or "post"."""
    period: int | None = None
    clock: str | None = None
    result_method: str | None = None
    result_description: str | None = None
    result_target: str | None = None

    def fighter(self, athlete_id: str | None) -> Fighter | None:
        return next((f for f in self.fighters if f.id == athlete_id), None)

    def opponent(self, athlete_id: str | None) -> Fighter | None:
        return next((f for f in self.fighters if f.id != athlete_id), None)

    @property
    def has_opponents(self) -> bool:
        """Both fighters are named."""
        return len(self.fighters) >= 2

    @property
    def awaiting_names(self) -> bool:
        """The card has two corners but this bot could not name them both."""
        return self.competitors >= 2 and not self.has_opponents

    @property
    def is_championship_rounds(self) -> bool:
        """Five rounds means a title fight or the main event."""
        return self.rounds == 5

    @property
    def matchup(self) -> str:
        if len(self.fighters) >= 2:
            return f"{self.fighters[0].display_name} vs. {self.fighters[1].display_name}"
        if self.fighters:
            return f"{self.fighters[0].display_name} vs. TBA"
        return "TBA vs. TBA"


@dataclass(slots=True)
class Event:
    """A UFC card.

    ``start`` is when the broadcast opens, which is the early prelims or prelims.
    ``main_card_start`` is when the main card begins, typically a few hours later.
    """

    id: str
    name: str
    start: datetime
    short_name: str | None = None
    venue_name: str | None = None
    venue_city: str | None = None
    venue_country: str | None = None
    broadcast: str | None = None
    espn_url: str | None = None
    poster_url: str | None = None
    bouts: list[Bout] = field(default_factory=list)
    partial: bool = False
    """A fight on this card could not be read, so ``bouts`` is not the whole card.

    Anything that would conclude a fight has been cancelled has to check this
    first, or a bad response reads as a card losing fights.
    """
    main_card_start: datetime | None = None

    @property
    def location(self) -> str:
        parts = [p for p in (self.venue_name, self.venue_city, self.venue_country) if p]
        return ", ".join(parts) if parts else "Location TBA"

    @property
    def short_location(self) -> str:
        parts = [p for p in (self.venue_city, self.venue_country) if p]
        return ", ".join(parts) if parts else "TBA"

    @property
    def fights(self) -> list[Bout]:
        """Bouts with both fighters named, main event first."""
        return [bout for bout in self.ordered_bouts() if bout.has_opponents]

    def start_for(self, anchor: str) -> datetime:
        """Resolve the configured start anchor to a concrete UTC timestamp."""
        if anchor == "main_card" and self.main_card_start:
            return max(self.main_card_start, self.start)
        return self.start

    def ordered_bouts(self) -> list[Bout]:
        """Main event first, then down the main card, then prelims."""
        return sorted(
            self.bouts,
            key=lambda b: (
                segment_rank(b.segment),
                b.match_number if b.match_number is not None else 999,
            ),
        )

    def still_carded(self, bout_id: str, fighters: set[str]) -> bool | None:
        """Is this still the same fight? None when the card cannot say.

        True when the bout is on the card with exactly these two fighters, False
        when it has gone or someone has been replaced, and None when the bout is
        listed but its names did not load, which is not news about the fight.
        """
        bout = next((b for b in self.bouts if b.id == bout_id), None)
        if bout is None:
            return None if self.partial else False
        if bout.awaiting_names:
            return None
        return {f.id for f in bout.fighters[:2]} == fighters

    @property
    def has_anyone_named(self) -> bool:
        """Whether a single fighter on the card is known yet.

        ESPN posts a card months out as a date and a row of empty slots. Until
        one name lands there is nothing to put on a calendar or announce, and a
        "TBA vs. TBA" event only has to be rewritten once the card is real.
        """
        return any(bout.fighters for bout in self.bouts)

    def bouts_by_segment(self) -> list[tuple[str, list[Bout]]]:
        groups: dict[str, list[Bout]] = {}
        for bout in self.ordered_bouts():
            groups.setdefault(bout.segment or "Card", []).append(bout)
        return sorted(groups.items(), key=lambda kv: segment_rank(kv[0]))
