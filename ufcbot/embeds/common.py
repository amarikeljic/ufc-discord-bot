"""Colours, limits and text helpers shared by every embed."""

from __future__ import annotations

import math
from datetime import date

import discord

from ..stats.techniques import FINISHES, METHOD_SHORT
from ..util import format_odds

UFC_RED = discord.Colour.from_str("#D20A0A")
PICKS_PURPLE = discord.Colour.from_str("#7B3FE4")
LIVE_GOLD = discord.Colour.from_str("#E8B93B")

FIELD_LIMIT = 1024
EMBED_BUDGET = 5900  # Discord's hard limit is 6000; leave room for the footer.

DASH = "—"
NBSP = " "
# Code blocks on a phone fit about 25 characters before they wrap, so a table row
# is at most 8 + 1 + 7 + 1 + 7 = 24 characters wide.
TABLE_LABEL_WIDTH = 8
TABLE_VALUE_WIDTH = 7
ZERO_WIDTH = "​"  # a field title that renders as nothing

# Shared by every ranked list: the ratings boards and the pick'em leaderboard.
MEDALS = {1: "🥇", 2: "🥈", 3: "🥉"}

# Short names for finishing techniques where space is tight.
SHORT_TECHNIQUE = {
    "rear-naked choke": "RNC",
    "ground and pound": "G&P",
    "arm-triangle": "arm triangle",
    "D'Arce choke": "D'Arce",
    "anaconda choke": "anaconda",
    "triangle choke": "triangle",
    "north-south choke": "north-south",
    "von Flue choke": "von Flue",
    "ezekiel choke": "ezekiel",
    "Peruvian necktie": "necktie",
    "spinning back fist": "back fist",
    "doctor stoppage": "doctor",
    "corner stoppage": "corner",
    "body punches": "body shots",
}

STANCE_SHORT = {
    "orthodox": "Orth",
    "southpaw": "South",
    "switch": "Switch",
    "open stance": "Open",
    "sideways": "Side",
}


def is_nan(value: float | None) -> bool:
    return value is None or (isinstance(value, float) and math.isnan(value))


def pct(value: float | None) -> str:
    return DASH if is_nan(value) else f"{value:.0%}"


def num(value: float | None, digits: int = 2) -> str:
    return DASH if is_nan(value) else f"{value:.{digits}f}"


def inches(value: float | None) -> str:
    """71 -> 5'11\"."""
    if is_nan(value):
        return DASH
    feet, rem = divmod(int(value), 12)
    return f"{feet}'{rem}\"" if feet else f"{rem}\""


def fmt_odds(line: int | None) -> str:
    return DASH if line is None else format_odds(line)


def mmss(seconds: float | None) -> str:
    if is_nan(seconds):
        return DASH
    total = int(seconds)
    return f"{total // 60}:{total % 60:02d}"


def short_record(record: str | None) -> str | None:
    """``17-3-0`` reads better as ``17-3``; draws stay when there are some."""
    if not record:
        return None
    parts = record.split("-")
    if len(parts) == 3 and parts[2].strip().split(" ")[0] == "0":
        return f"{parts[0]}-{parts[1]}"
    return record


def age(dob: date | None, on: date | None = None) -> str:
    if dob is None:
        return DASH
    on = on or date.today()
    return str(on.year - dob.year - ((on.month, on.day) < (dob.month, dob.day)))


def surname(name: str) -> str:
    """Last name, keeping suffixes attached: "Raul Rosas Jr." -> "Rosas Jr."."""
    parts = name.split()
    if len(parts) >= 2 and parts[-1].rstrip(".").lower() in ("jr", "sr", "ii", "iii", "iv"):
        return f"{parts[-2]} {parts[-1]}"
    return parts[-1] if parts else name


def plural(count: int, singular: str, many: str | None = None) -> str:
    """``3, "pick"`` -> "3 picks"; ``1, "pick"`` -> "1 pick"."""
    return f"{count} {singular if count == 1 else (many or singular + 's')}"


def keep(text: str) -> str:
    """Stop a narrow screen from breaking this phrase across lines."""
    return text.replace(" ", NBSP)


def join(parts) -> str:
    """Facts separated by " · ", each kept on one line."""
    return " · ".join(keep(p) for p in parts if p)


def add_chunked_fields(embed: discord.Embed, name: str, lines: list[str]) -> None:
    """Add lines as one field, continuing into untitled fields past Discord's size limit."""
    chunk, title = "", name
    for line in lines:
        if chunk and len(chunk) + len(line) + 1 > FIELD_LIMIT:
            embed.add_field(name=title, value=chunk, inline=False)
            chunk, title = line, ZERO_WIDTH
        else:
            chunk = f"{chunk}\n{line}" if chunk else line
    if chunk:
        embed.add_field(name=title, value=chunk, inline=False)


def outcome_label(method: str | None, technique: str | None = None) -> str:
    """Compact result wording: "KO/TKO (punches)", "Sub (RNC)", "S-Dec"."""
    base = METHOD_SHORT.get(method or "", method or "result")
    if technique and method in FINISHES and technique not in ("other", "strikes"):
        return f"{base} ({SHORT_TECHNIQUE.get(technique, technique)})"
    return base


def bar(prob_a: float, width: int = 10) -> str:
    filled = round(prob_a * width)
    return "█" * filled + "░" * (width - filled)


def code_table(left: str, right: str, rows: list[tuple[str, str, str]]) -> str:
    """A fixed-width two-fighter table narrow enough that a phone never wraps a row."""
    head_a = surname(left).upper()[:TABLE_VALUE_WIDTH]
    head_b = surname(right).upper()[:TABLE_VALUE_WIDTH]
    label_width = min(TABLE_LABEL_WIDTH, max(len(row[0]) for row in rows))
    value_width = min(
        TABLE_VALUE_WIDTH,
        max(len(head_a), len(head_b), *(max(len(row[1]), len(row[2])) for row in rows)),
    )

    def row(label: str, a: str, b: str) -> str:
        return f"{label[:label_width]:<{label_width}} {a[:value_width]:>{value_width}} {b[:value_width]:>{value_width}}"

    lines = [row("", head_a, head_b)] + [row(*entry) for entry in rows]
    return "```\n" + "\n".join(lines) + "\n```"


def stamp(embed: discord.Embed) -> discord.Embed:
    """Finish an embed with the current time in its footer.

    Discord shows the timestamp in each viewer's own timezone, as "Today at
    8:53 PM" or a full date for older messages. Channel boards add a
    "Last updated" label when they are posted or edited.
    """
    embed.timestamp = discord.utils.utcnow()
    return embed


def streak(ledger) -> str:
    if ledger.win_streak:
        return f"W{ledger.win_streak}"
    if ledger.loss_streak:
        return f"L{ledger.loss_streak}"
    return DASH
