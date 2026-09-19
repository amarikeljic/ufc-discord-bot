"""Picks boards, head-to-head predictions, results recaps and the scorecard."""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING

import discord

from ..models import Event
from ..stats.prediction import Prediction
from ..stats.techniques import FINISHES, METHOD_LABELS, METHOD_SHORT
from ..util import truncate
from .common import (
    DASH,
    EMBED_BUDGET,
    FIELD_LIMIT,
    PICKS_PURPLE,
    SHORT_TECHNIQUE,
    STANCE_SHORT,
    UFC_RED,
    add_chunked_fields,
    age,
    bar,
    code_table,
    fmt_odds,
    inches,
    join,
    keep,
    num,
    outcome_label,
    pct,
    plural,
    stamp,
    streak,
    surname,
)

if TYPE_CHECKING:
    from ..features.tracking import GradedEvent, Scorecard
    from ..stats.prediction import Evaluation
    from ..stats.service import FighterCareer
    from ..storage import PredictionRecord


def pick_value(
    name_a: str,
    name_b: str,
    *,
    favourite: str,
    confidence: float,
    prediction: Prediction | None,
    odds_a: int | None,
    odds_b: int | None,
    extra_lines: list[str],
    compact: bool,
    odds_source: str | None = None,
) -> str:
    """One fight on a picks board, a handful of short lines.

    ``odds_source`` is only passed when a card's lines come from more than one
    place, so the usual card says where its odds came from once, at the top.
    """
    lines = [f"🎯 **{keep(favourite)}** {confidence:.0%}  {bar(confidence, 8)}"]
    if prediction is not None and prediction.has_methods:
        routes = prediction.outcomes(prediction.favourite_side)
        if routes:
            _, method, technique, prob = routes[0]
            likeliest = f"{keep(outcome_label(method, technique))} {prob:.0%}"
            if not compact and len(routes) > 1:
                # The runners-up ride along on the same line rather than taking
                # a line of their own; on a phone every line costs two.
                likeliest += " · also " + join(f"{METHOD_SHORT[m]} {p:.0%}" for _, m, _t, p in routes[1:])
            lines.append(likeliest)
    if odds_a is not None and odds_b is not None:
        odds = [f"{surname(name_a)} {fmt_odds(odds_a)}", f"{surname(name_b)} {fmt_odds(odds_b)}"]
        if odds_source:
            odds.append(odds_source)
        lines.append("💰 " + join(odds))
    lines.extend(extra_lines)
    return truncate("\n".join(lines), FIELD_LIMIT)


def result_lines(record: PredictionRecord) -> list[str]:
    if record.graded_at is None:
        return []
    if record.correct is None:
        return [f"Result: {METHOD_LABELS.get(record.result_method or '', 'no result')} · void"]
    how = outcome_label(record.result_method, record.result_technique)
    when = ""
    if record.result_round and record.result_method in FINISHES:
        when = f" · R{record.result_round} {record.result_time or ''}".rstrip()
    marks = ["Pick ✅" if record.correct else "Pick ❌"]
    if record.method_correct is not None:
        marks.append("Method ✅" if record.method_correct else "Method ❌")
    if record.technique_correct is not None:
        marks.append("Technique ✅" if record.technique_correct else "Technique ❌")
    winner = surname(record.winner_name or "")
    return [f"Result: **{keep(winner)}** by {keep(how)}{when}", join(marks)]


def picks_board_embed(
    event_name: str,
    start,
    records: list[PredictionRecord],
    *,
    locked: bool,
    espn_url: str | None = None,
) -> discord.Embed:
    """The on-record picks for one card, as posted in the picks channel."""

    # Cards usually take every line from one book, and say so once at the top.
    # A card drawing on more than one says so fight by fight instead, because
    # one line naming both leaves you unable to tell which fight is which.
    sources = {r.odds_source for r in records if r.odds_source}
    mixed = len(sources) > 1
    shared = next(iter(sources)) if len(sources) == 1 else None

    def build(compact: bool) -> discord.Embed:
        embed = discord.Embed(title=truncate(event_name, 256), url=espn_url, colour=PICKS_PURPLE)

        header = [f"🗓️ {discord.utils.format_dt(start, 'F')}"]
        scored = [r for r in records if r.correct is not None]
        if scored:
            hits = sum(1 for r in scored if r.correct)
            header.append(f"Picks: **{hits}/{len(scored)}** correct ({hits / len(scored):.0%})")
            with_method = [r for r in scored if r.method_correct is not None]
            if with_method:
                header.append(f"Method: **{sum(1 for r in with_method if r.method_correct)}/{len(with_method)}** correct")
        elif locked:
            header.append("🔒 Picks locked at first bell")
        else:
            header.append(f"⏱️ {discord.utils.format_dt(start, 'R')} · {plural(len(records), 'pick')}")
        if mixed:
            header.append(f"Odds from {keep(' · '.join(sorted(sources)))}, marked per fight")
        elif shared:
            header.append(f"Odds from {keep(shared)}")
        embed.description = "\n".join(header)

        for record in records[:25]:
            prediction = Prediction.from_dict(record.name_a, record.name_b, record.detail) if record.detail else None
            embed.add_field(
                name=truncate(f"{record.name_a} vs. {record.name_b}", 256),
                value=pick_value(
                    record.name_a,
                    record.name_b,
                    favourite=record.favourite,
                    confidence=record.confidence,
                    prediction=prediction,
                    odds_a=record.odds_a,
                    odds_b=record.odds_b,
                    extra_lines=result_lines(record),
                    compact=compact,
                    odds_source=record.odds_source if mixed else None,
                ),
                inline=False,
            )
        if not records:
            embed.add_field(name="Picks", value="No picks yet.", inline=False)

        return stamp(embed)

    embed = build(compact=False)
    return embed if len(embed) <= EMBED_BUDGET else build(compact=True)


def predictions_embed(event: Event, picks: dict[str, Prediction]) -> discord.Embed:
    """Every pick for a card, with how each fight is likely to end and the current odds."""
    sources = {bout.odds_provider for bout in event.bouts if bout.odds_provider}
    mixed = len(sources) > 1

    def build(compact: bool) -> discord.Embed:
        embed = discord.Embed(title=truncate(f"Picks: {event.name}", 256), url=event.espn_url, colour=PICKS_PURPLE)
        header = [
            f"🗓️ {discord.utils.format_dt(event.start, 'D')}",
            f"{len(picks)} of {len(event.bouts)} bouts predicted",
        ]
        if mixed:
            header.append(f"Odds from {keep(' · '.join(sorted(sources)))}, marked per fight")
        elif sources:
            header.append(f"Odds from {keep(next(iter(sources)))}")
        embed.description = "\n".join(header)
        for bout in event.ordered_bouts()[:25]:
            if not bout.has_opponents:
                continue
            a, b = bout.fighters[0], bout.fighters[1]
            pick = picks.get(bout.id)
            if pick is None:
                value = "No pick: not enough UFC history"
            else:
                value = pick_value(
                    a.display_name,
                    b.display_name,
                    favourite=a.display_name if pick.prob_a >= 0.5 else b.display_name,
                    confidence=pick.confidence,
                    prediction=pick,
                    odds_a=bout.odds.get(a.id),
                    odds_b=bout.odds.get(b.id),
                    extra_lines=[],
                    compact=compact,
                    odds_source=bout.odds_provider if mixed else None,
                )
            embed.add_field(name=truncate(f"{a.display_name} vs. {b.display_name}", 256), value=value, inline=False)
        return stamp(embed)

    embed = build(compact=False)
    return embed if len(embed) <= EMBED_BUDGET else build(compact=True)


def _how_breakdown(prediction: Prediction, side: str) -> str:
    techniques = prediction.techniques_a if side == "a" else prediction.techniques_b
    lines = []
    for _, method, _technique, prob in prediction.outcomes(side):
        line = f"{METHOD_SHORT[method]} **{prob:.0%}**"
        if method in FINISHES and techniques.get(method):
            line += " · " + join(f"{SHORT_TECHNIQUE.get(name, name)} {share:.0%}" for name, share in techniques[method][:2])
        lines.append(line)
    return "\n".join(lines) or DASH


def prediction_embed(
    prediction: Prediction,
    career_a: FighterCareer,
    career_b: FighterCareer,
    *,
    on: date | None = None,
) -> discord.Embed:
    """Head-to-head with win probability, how it ends, and a tale-of-the-tape table."""
    a, b = career_a.ledger, career_b.ledger
    ia, ib = career_a.info, career_b.info

    embed = discord.Embed(title=truncate(f"{a.name} vs. {b.name}", 256), colour=PICKS_PURPLE)
    lines = [
        f"{keep(surname(a.name))} {prediction.prob_a:.0%} {bar(prediction.prob_a)} {prediction.prob_b:.0%} {keep(surname(b.name))}",
        f"Pick: **{keep(prediction.favourite)}**",
    ]
    outcome = prediction.pick_outcome
    if outcome:
        method, technique, prob = outcome
        lines.append(f"Likeliest: {keep(outcome_label(method, technique))} {prob:.0%}")
    if prediction.draw >= 0.005:
        lines.append(f"Draw: about {prediction.draw:.0%}")
    embed.description = "\n".join(lines)

    if prediction.has_methods:
        embed.add_field(name=truncate(f"How {surname(a.name)} wins", 256), value=_how_breakdown(prediction, "a"), inline=False)
        embed.add_field(name=truncate(f"How {surname(b.name)} wins", 256), value=_how_breakdown(prediction, "b"), inline=False)

    def stance(info) -> str:
        return STANCE_SHORT.get((info.stance or "").lower(), info.stance or DASH) if info else DASH

    rows = [
        ("Record", f"{a.wins}-{a.losses}-{a.draws}", f"{b.wins}-{b.losses}-{b.draws}"),
        ("Age", age(ia.dob if ia else None, on), age(ib.dob if ib else None, on)),
        ("Height", inches(ia.height_in) if ia else DASH, inches(ib.height_in) if ib else DASH),
        ("Reach", inches(ia.reach_in) if ia else DASH, inches(ib.reach_in) if ib else DASH),
        ("Stance", stance(ia), stance(ib)),
        ("SLpM", num(a.slpm), num(b.slpm)),
        ("Str acc", pct(a.str_acc), pct(b.str_acc)),
        ("SApM", num(a.sapm), num(b.sapm)),
        ("Str def", pct(a.str_def), pct(b.str_def)),
        ("TD avg", num(a.td_avg), num(b.td_avg)),
        ("TD acc", pct(a.td_acc), pct(b.td_acc)),
        ("TD def", pct(a.td_def), pct(b.td_def)),
        ("Sub avg", num(a.sub_avg, 1), num(b.sub_avg, 1)),
        ("Finish", pct(a.finish_rate), pct(b.finish_rate)),
        ("Streak", streak(a), streak(b)),
    ]
    embed.add_field(name="Tale of the tape", value=code_table(a.name, b.name, rows), inline=False)

    return stamp(embed)


# -- accuracy channel ------------------------------------------------------------------


def recap_embed(event: GradedEvent, card: Scorecard) -> discord.Embed:
    """Posted to the accuracy channel once a card is fully graded."""
    embed = discord.Embed(title=truncate(f"Results: {event.name}", 256), colour=UFC_RED)
    lines = [discord.utils.format_dt(event.start, "D")]
    if event.total:
        lines.append(f"Picks: **{event.correct}/{event.total}** correct ({event.correct / event.total:.0%})")
    else:
        lines.append("No scorable fights")
    if event.method_total:
        lines.append(f"Method: **{event.method_hits}/{event.method_total}** correct")
    embed.description = "\n".join(lines)

    fight_lines = []
    for record in event.records:
        if record.correct is None:
            label = METHOD_LABELS.get(record.result_method or "", "no result")
            fight_lines.append(f"➖ {join([record.matchup, label])}")
            continue
        mark = "✅" if record.correct else "❌"
        winner = surname(record.winner_name or "")
        facts = [
            f"{surname(record.favourite)} {record.confidence:.0%}",
            f"{winner} by {outcome_label(record.result_method, record.result_technique)}",
        ]
        if record.method_correct:
            facts.append("method ✅")
        fight_lines.append(f"{mark} {join(facts)}")

    add_chunked_fields(embed, "Fights", fight_lines or [DASH])

    embed.add_field(name="Running scorecard", value=_scorecard_summary(card), inline=False)
    return stamp(embed)


def _scorecard_summary(card: Scorecard) -> str:
    if not card.total:
        return "No graded picks yet."
    lines = [f"Record: **{card.correct}-{card.total - card.correct}** ({card.accuracy:.0%})"]
    if card.since:
        lines.append(f"Since {card.since:%b %d, %Y}")
    lines.append(f"Avg confidence: {card.avg_confidence:.0%}")
    if card.method_total:
        lines.append(f"Winner + method: **{card.method_hits}/{card.method_total}** ({card.method_hits / card.method_total:.0%})")
    if card.market_total:
        lines.append(f"Betting favourites: **{card.market_hits}/{card.market_total}** ({card.market_hits / card.market_total:.0%})")
    if card.disagree_total:
        lines.append(f"Model vs. the odds: **{card.disagree_hits}/{card.disagree_total}**")
    if card.streak >= 2:
        lines.append(f"🔥 {card.streak} in a row")
    elif card.streak <= -2:
        lines.append(f"🧊 {-card.streak} straight misses")
    if card.void:
        lines.append(f"{card.void} void (draw, NC or cancelled)")
    return "\n".join(lines)


def scorecard_embed(card: Scorecard, *, evaluation: Evaluation | None) -> discord.Embed:
    """Running accuracy summary, kept as the latest message in the accuracy channel."""
    embed = discord.Embed(title="📊 Model scorecard", colour=PICKS_PURPLE)
    embed.description = _scorecard_summary(card)

    if card.bands:
        embed.add_field(
            name="By confidence",
            value="\n".join(f"{label}: **{hits}/{total}** ({hits / total:.0%})" for label, hits, total in card.bands),
            inline=False,
        )
    if card.events:
        embed.add_field(
            name="Recent cards",
            value="\n".join(f"{e.start:%b %d} · **{e.correct}/{e.total}** · {truncate(e.name, 30)}" for e in card.events[:10]),
            inline=False,
        )

    # Kept well away from the record above. Naming the technique is a flourish on
    # a call that was already right, and it barely beats guessing the commonest
    # one, so it is never counted towards how the model is doing.
    if card.technique_total:
        note = f"**{card.technique_hits}/{card.technique_total}** named exactly, out of the finishes it called correctly"
        if evaluation and evaluation.technique_accuracy is not None and evaluation.technique_baseline is not None:
            note += (
                f"\nBacktest {evaluation.technique_accuracy:.0%} against {evaluation.technique_baseline:.0%} "
                "for always guessing the commonest one. Not counted towards the record above."
            )
        embed.add_field(name="Finishing technique", value=note, inline=False)

    if evaluation:
        text = f"Winner {evaluation.accuracy:.0%} on {evaluation.fights} past fights"
        if evaluation.exact_accuracy is not None:
            text += f"\nWinner + method {evaluation.exact_accuracy:.0%}"
        embed.add_field(name="Backtest", value=text, inline=False)
    return stamp(embed)
