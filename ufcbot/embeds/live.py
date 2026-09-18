"""Live coverage posts: fight previews, knockdowns, round stats and results."""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord

from ..models import Bout, Fighter
from ..stats.techniques import FINISHES, METHOD_LABELS, describe
from ..util import truncate
from .common import (
    DASH,
    LIVE_GOLD,
    STANCE_SHORT,
    UFC_RED,
    age,
    code_table,
    fmt_odds,
    inches,
    is_nan,
    join,
    keep,
    mmss,
    num,
    outcome_label,
    pct,
    short_record,
    stamp,
    streak,
    surname,
)

if TYPE_CHECKING:
    from ..stats.service import FighterCareer
    from ..storage import PredictionRecord


def _stats_table(bout: Bout, stats: dict[str, dict[str, float]]) -> str | None:
    if not bout.has_opponents:
        return None
    a, b = bout.fighters[0], bout.fighters[1]
    sa, sb = stats.get(a.id), stats.get(b.id)
    if not sa or not sb:
        return None

    def i(value: float) -> str:
        return str(int(value))

    rows = [
        ("Sig str", f"{i(sa['sig_l'])}/{i(sa['sig_a'])}", f"{i(sb['sig_l'])}/{i(sb['sig_a'])}"),
        ("Total", f"{i(sa['tot_l'])}/{i(sa['tot_a'])}", f"{i(sb['tot_l'])}/{i(sb['tot_a'])}"),
        ("KD", i(sa["kd"]), i(sb["kd"])),
        ("TD", f"{i(sa['td_l'])}/{i(sa['td_a'])}", f"{i(sb['td_l'])}/{i(sb['td_a'])}"),
        ("Sub att", i(sa["sub"]), i(sb["sub"])),
        ("Ctrl", mmss(sa["ctrl"]), mmss(sb["ctrl"])),
        ("Head", i(sa["head"]), i(sb["head"])),
        ("Body", i(sa["body"]), i(sb["body"])),
        ("Leg", i(sa["leg"]), i(sb["leg"])),
    ]
    return code_table(a.display_name, b.display_name, rows)


def _odds_line(
    first: Fighter, second: Fighter, odds: dict[str, int], label: str = "Odds", source: str | None = None
) -> str | None:
    if first.id not in odds or second.id not in odds:
        return None
    if source:
        label = f"{label} ({source})"
    return f"{label}: " + join(
        [f"{surname(first.display_name)} {fmt_odds(odds[first.id])}", f"{surname(second.display_name)} {fmt_odds(odds[second.id])}"]
    )


def _tape_table(a: Fighter, b: Fighter, careers: dict[str, FighterCareer], odds: dict[str, int]) -> str | None:
    """Tale of the tape: ESPN's bio plus the ufcstats.com career numbers when they are known."""
    left, right = careers.get(a.id), careers.get(b.id)
    ledgers = (left.ledger if left else None, right.ledger if right else None)
    infos = (left.info if left else None, right.info if right else None)
    rows: list[tuple[str, str, str]] = []

    def add(label: str, x: str | None, y: str | None) -> None:
        x, y = x or DASH, y or DASH
        if x != DASH or y != DASH:
            rows.append((label, x, y))

    def both(pick) -> tuple[str | None, str | None]:
        return pick(a, infos[0], ledgers[0]), pick(b, infos[1], ledgers[1])

    add("Odds", fmt_odds(odds.get(a.id)), fmt_odds(odds.get(b.id)))
    add("Record", *both(lambda f, i, _l: short_record(f.record)))
    add("UFC", *both(lambda _f, _i, ledger: ledger.record if ledger else None))
    add("Age", *both(lambda f, i, _l: age(i.dob) if i and i.dob else (str(f.age) if f.age else None)))
    add("Height", *both(lambda f, i, _l: inches(i.height_in) if i else f.height))
    add("Weight", *both(lambda _f, i, _l: f"{i.weight_lb:.0f} lb" if i and not is_nan(i.weight_lb) else None))
    add("Reach", *both(lambda f, i, _l: inches(i.reach_in) if i else f.reach))
    add(
        "Stance",
        *both(
            lambda f, i, _l: STANCE_SHORT.get(((i.stance if i else None) or f.stance or "").lower())
            or (i.stance if i and i.stance else f.stance)
        ),
    )
    add("From", *both(lambda f, _i, _l: truncate(f.citizenship, 12) if f.citizenship else None))

    if all(ledger and ledger.stat_fights for ledger in ledgers):
        la, lb = ledgers
        rows += [
            ("Streak", streak(la), streak(lb)),
            ("SLpM", num(la.slpm), num(lb.slpm)),
            ("Str acc", pct(la.str_acc), pct(lb.str_acc)),
            ("SApM", num(la.sapm), num(lb.sapm)),
            ("Str def", pct(la.str_def), pct(lb.str_def)),
            ("TD avg", num(la.td_avg), num(lb.td_avg)),
            ("TD acc", pct(la.td_acc), pct(lb.td_acc)),
            ("TD def", pct(la.td_def), pct(lb.td_def)),
            ("Sub avg", num(la.sub_avg, 1), num(lb.sub_avg, 1)),
            ("Finish", pct(la.finish_rate), pct(lb.finish_rate)),
        ]
    return code_table(a.display_name, b.display_name, rows) if rows else None


def live_open_embed(
    *,
    event_name: str,
    bout: Bout,
    record: PredictionRecord | None,
    odds: dict[str, int],
    odds_source: str | None = None,
    careers: dict[str, FighterCareer] | None = None,
) -> discord.Embed:
    a, b = (bout.fighters + [None, None])[:2]
    title = f"Up next: {a.display_name if a else 'TBA'} vs. {b.display_name if b else 'TBA'}"
    embed = discord.Embed(title=truncate(title, 256), colour=LIVE_GOLD)

    lines = []
    meta = [bout.weight_class, f"{bout.rounds} rounds" if bout.rounds else None, bout.segment]
    if any(meta):
        lines.append(join(meta))
    if a and b:
        lines.append(
            join(
                [
                    f"{surname(a.display_name)} {short_record(a.record) or ''}".strip(),
                    f"{surname(b.display_name)} {short_record(b.record) or ''}".strip(),
                ]
            )
        )
    if record is not None:
        lines.append(f"Pick: **{keep(record.favourite)}** {record.confidence:.0%}")
        if record.method:
            lines.append(f"Likeliest: {keep(outcome_label(record.method, record.technique))}")
    if a and b:
        odds_line = _odds_line(a, b, odds, source=odds_source)
        if odds_line:
            lines.append(odds_line)
    embed.description = "\n".join(lines)

    if a and b:
        tape = _tape_table(a, b, careers or {}, odds)
        if tape:
            embed.add_field(name="Tale of the tape", value=tape, inline=False)
    embed.set_footer(text=truncate(event_name, 2048))
    return stamp(embed)


def _when(period: int, clock: str | None) -> str:
    return f"R{period} {clock}" if clock and clock != "-" else f"R{period}"


def live_knockdown_text(
    bout: Bout, period: int, clock: str | None, scorer: Fighter | None, event_name: str | None = None
) -> str:
    when = _when(period, clock)
    opponent = bout.opponent(scorer.id) if scorer else None
    if scorer and opponent:
        text = f"💥 **Knockdown!** {scorer.display_name} drops {opponent.display_name} · {keep(when)}"
    else:
        text = f"💥 **Knockdown!** {bout.matchup} · {keep(when)}"
    return _on_card(text, event_name)


def live_pause_text(bout: Bout, period: int, clock: str | None, event_name: str | None = None) -> str:
    """A fight stopped mid-round: a foul, a doctor's look, a glove or mouthpiece.

    ESPN records that the clock stopped but never why, so this says only that.
    """
    return _on_card(f"⏸️ **Action paused** · {bout.matchup} · {keep(_when(period, clock))}", event_name)


def _on_card(text: str, event_name: str | None) -> str:
    """Name the card at the end of a plain message.

    The embeds around these two carry the card in their footer; a plain message
    has nowhere else to put it. It goes last and is left breakable, so it is the
    part that wraps on a narrow screen rather than the news.
    """
    return f"{text} · {event_name}" if event_name else text


def live_round_embed(
    *,
    event_name: str,
    bout: Bout,
    round_number: int,
    stats: dict[str, dict[str, float]],
    edge: int,
    stopped: bool = False,
) -> discord.Embed:
    heading = f"Round {round_number} (fight stopped)" if stopped else f"End of round {round_number}"
    embed = discord.Embed(title=truncate(f"{heading}: {bout.matchup}", 256), colour=LIVE_GOLD)
    if bout.has_opponents and edge:
        leader = bout.fighters[0] if edge > 0 else bout.fighters[1]
        embed.description = f"Stats edge: **{keep(leader.display_name)}**\n*Unofficial, from the numbers only*"
    else:
        embed.description = "Even round on the stats\n*Unofficial, from the numbers only*"
    table = _stats_table(bout, stats)
    if table:
        embed.add_field(name=f"Round {round_number} stats", value=table, inline=False)
    embed.set_footer(text=truncate(event_name, 2048))
    return stamp(embed)


def live_result_embed(
    *,
    event_name: str,
    bout: Bout,
    winner: Fighter | None,
    method: str | None,
    technique: str | None,
    round_number: int | None,
    clock: str | None,
    totals: dict[str, dict[str, float]],
    scorecards: dict[str, list[int]],
    record: PredictionRecord | None,
    odds: dict[str, int],
    odds_source: str | None = None,
) -> discord.Embed:
    loser = bout.opponent(winner.id) if winner else None
    if winner and loser:
        title = f"{winner.display_name} def. {loser.display_name}"
    elif method == "draw":
        title = f"Draw: {bout.matchup}"
    else:
        title = f"No contest: {bout.matchup}"
    embed = discord.Embed(title=truncate(title, 256), colour=UFC_RED)

    lines = []
    if method in METHOD_LABELS and winner:
        text = f"By **{keep(describe(method, technique))}**"
        if method in FINISHES and round_number:
            text += " · " + keep(f"R{round_number} {clock or ''}".strip())
        lines.append(text)

    if scorecards and bout.has_opponents:
        first = winner or bout.fighters[0]
        second = bout.opponent(first.id)
        if second and first.id in scorecards and second.id in scorecards:
            lines.append("Judges: " + " · ".join(f"{x}-{y}" for x, y in zip(scorecards[first.id], scorecards[second.id])))

    if record is not None and winner is not None:
        right = winner.id == record.favourite_athlete
        lines.append(f"Pick: **{keep(record.favourite)}** {record.confidence:.0%} {'✅' if right else '❌'}")
        if record.method:
            exact = right and record.method == method
            lines.append(f"Called: {keep(outcome_label(record.method, record.technique))} {'✅' if exact else '❌'}")

    if winner and loser:
        odds_line = _odds_line(winner, loser, odds, label="Closing odds", source=odds_source)
        if odds_line:
            if odds[winner.id] > odds[loser.id]:
                odds_line += " · **Upset**"
            lines.append(odds_line)

    embed.description = "\n".join(lines) or None
    table = _stats_table(bout, totals)
    if table:
        embed.add_field(name="Fight totals", value=table, inline=False)
    embed.set_footer(text=truncate(event_name, 2048))
    return stamp(embed)
