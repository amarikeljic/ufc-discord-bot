"""Pick'em embeds: the card board, the private picker, the leaderboard and member stats."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

import discord

from ..features.pickem import (
    LOCKED,
    OPEN,
    bout_status,
    points_for,
    segment_locks,
)
from ..models import Bout, Event
from ..util import truncate
from .common import (
    FIELD_LIMIT,
    add_chunked_fields,
    fmt_odds,
    join,
    keep,
    plural,
    stamp,
    surname,
)

if TYPE_CHECKING:
    from ..storage import PickemCard, PickemRecord, PickemStanding, PickemSummary

PICKEM_TEAL = discord.Colour.from_str("#1ABC9C")
MEDALS = {1: "🥇", 2: "🥈", 3: "🥉"}
RESULT_ICON = {"win": "✅", "loss": "❌", "void": "➖", None: "⏳"}


def _lock_lines(event: Event, now: datetime) -> list[str]:
    lines = []
    for segment, when in segment_locks(event):
        if when > now:
            lines.append(f"🔒 {segment} locks {discord.utils.format_dt(when, 'R')}")
        else:
            lines.append(f"🔒 {segment} locked")
    return lines


def _standing_line(rank: int, standing: PickemStanding) -> str:
    badge = MEDALS.get(rank, f"`{rank:>2}`")
    record = f"{standing.wins}-{standing.losses} ({standing.win_rate:.0%})"
    return f"{badge} <@{standing.user_id}> · **{standing.points:,}** pts · {keep(record)}"


# -- the card board in the pick'em channel -------------------------------------------


def _board_bout_value(
    event: Event, bout: Bout, counts: dict[str, int], now: datetime, *, show_source: bool = False
) -> str:
    a, b = bout.fighters[0], bout.fighters[1]
    lines = []

    if bout.completed and bout.winner_id:
        winner = bout.fighter(bout.winner_id)
        if winner:
            lines.append(f"✅ **{keep(winner.display_name)}** won")

    for fighter in (a, b):
        odds = bout.odds.get(fighter.id)
        if odds is None:
            continue
        lines.append(join([f"{surname(fighter.display_name)} {fmt_odds(odds)}", f"+{points_for(odds)} pts"]))
    if not any(f.id in bout.odds for f in (a, b)):
        lines.append("Odds not posted yet")
    elif show_source and bout.odds_provider:
        # Only when the card's lines come from more than one place; otherwise the
        # board says so once, in its heading.
        lines.append(f"*{keep(bout.odds_provider)}*")

    total = sum(counts.values())
    if total:
        share_a = round(100 * counts.get(a.id, 0) / total)
        lines.append("👥 " + join([f"{share_a}% {surname(a.display_name)}", f"{100 - share_a}% {surname(b.display_name)}"]))

    if bout_status(event, bout, now) == LOCKED and not (bout.completed and bout.winner_id):
        lines.append("🔒 Locked")
    return truncate("\n".join(lines), FIELD_LIMIT)


def pickem_board_embed(event: Event, counts: dict[str, dict[str, int]], players: int, *, now: datetime) -> discord.Embed:
    embed = discord.Embed(title=truncate(f"🎯 Pick'em: {event.name}", 256), url=event.espn_url, colour=PICKEM_TEAL)
    lines = [f"🗓️ {discord.utils.format_dt(event.start, 'F')}", *_lock_lines(event, now)]
    lines.append(f"👥 {players} playing" if players else "👥 Be the first to pick")
    lines += ["", "Right pick: points from the odds", "Underdogs pay more · wrong pick: -100"]
    sources = {bout.odds_provider for bout in event.bouts if bout.odds_provider}
    mixed = len(sources) > 1
    if mixed:
        lines.append(f"Odds from {keep(' · '.join(sorted(sources)))}, marked per fight")
    elif sources:
        lines.append(f"Odds from {keep(next(iter(sources)))}")
    embed.description = "\n".join(lines)

    for bout in event.fights[:25]:
        embed.add_field(
            name=truncate(bout.matchup, 256),
            value=_board_bout_value(event, bout, counts.get(bout.id, {}), now, show_source=mixed),
            inline=False,
        )
    return stamp(embed)


# -- the private picker ---------------------------------------------------------------


def pickem_picker_embed(
    event: Event,
    picks: dict[str, PickemRecord],
    *,
    page: int,
    pages: int,
    now: datetime,
    notice: str | None = None,
) -> discord.Embed:
    embed = discord.Embed(title=truncate(f"🎯 Your picks: {event.name}", 256), colour=PICKEM_TEAL)
    fights = event.fights
    open_count = sum(1 for b in fights if bout_status(event, b, now) == OPEN)
    # Only fights still on the card count. A pick on one that has come off is not
    # a pick out of twelve, and it is not points to play for either.
    standing = [picks[bout.id] for bout in fights if bout.id in picks]
    potential = sum(p.points_if_right for p in standing if p.graded_at is None)

    lines = [notice, ""] if notice else []
    lines.append(f"Picked **{len(standing)}** of {len(fights)} · up to **{potential:,}** pts")
    lines.append(f"{plural(open_count, 'fight')} open for picks")
    lines += _lock_lines(event, now)
    lines += ["", "Choose a winner in each menu. Points shown", "are what you win if you're right.", "A wrong pick costs 100 pts."]
    if pages > 1:
        lines.append(f"Page {page + 1} of {pages}")
    embed.description = "\n".join(lines)
    return stamp(embed)


# -- leaderboard, stats and history ------------------------------------------------------


def pickem_leaderboard_embed(
    standings: list[PickemStanding],
    *,
    viewer_id: int | None = None,
    limit: int = 15,
    title: str = "🏆 Pick'em leaderboard",
) -> discord.Embed:
    embed = discord.Embed(title=title, colour=PICKEM_TEAL)
    if not standings:
        embed.description = "No settled picks yet.\nMake your picks on the next card to get on the board."
        return stamp(embed)

    lines = [_standing_line(rank, s) for rank, s in enumerate(standings[:limit], 1)]
    if viewer_id is not None:
        rank = next((i for i, s in enumerate(standings, 1) if s.user_id == viewer_id), None)
        if rank and rank > limit:
            lines += ["…", _standing_line(rank, standings[rank - 1])]
    embed.description = "\n".join(lines)
    return stamp(embed)


def pickem_stats_embed(user: discord.abc.User, summary: PickemSummary, cards: list[PickemCard]) -> discord.Embed:
    embed = discord.Embed(title=truncate(f"🎯 {user.display_name}'s pick'em", 256), colour=PICKEM_TEAL)
    embed.set_thumbnail(url=user.display_avatar.url)

    settled = summary.wins + summary.losses
    lines = []
    if summary.rank:
        lines.append(f"Rank: **#{summary.rank}** of {summary.players}")
    lines.append(f"Points: **{summary.points:,}**")
    lines.append(f"Record: **{summary.wins}-{summary.losses}**" + (f" ({summary.win_rate:.0%})" if settled else ""))
    if summary.underdog_wins:
        lines.append(f"Underdog wins: **{summary.underdog_wins}**")
    if summary.best_hit_name:
        lines.append(f"Best hit: **+{summary.best_hit}** on {keep(summary.best_hit_name)}")
    if summary.pending:
        lines.append(f"Pending: {plural(summary.pending, 'pick')}")
    if summary.voids:
        lines.append(f"Void: {summary.voids}")
    if not settled and not summary.pending:
        lines = ["No picks yet. Tap **Make your picks** in the pick'em channel."]
    embed.description = "\n".join(lines)

    if cards:
        history = []
        for card in cards:
            facts = [f"{card.wins}-{card.losses}", f"{card.points:,} pts"]
            if card.pending:
                facts.append(f"{card.pending} pending")
            history.append(f"**{truncate(card.event_name, 40)}**\n{card.event_start:%b %d} · {join(facts)}")
        embed.add_field(name="Card history", value=truncate("\n".join(history), FIELD_LIMIT), inline=False)
    return stamp(embed)


def pickem_card_embed(
    user: discord.abc.User,
    event_name: str,
    picks: list[PickemRecord],
    *,
    hidden: int = 0,
) -> discord.Embed:
    """One member's picks for one card, with each result."""
    embed = discord.Embed(title=truncate(f"🎯 {user.display_name}: {event_name}", 256), colour=PICKEM_TEAL)

    wins = sum(1 for p in picks if p.result == "win")
    losses = sum(1 for p in picks if p.result == "loss")
    points = sum(p.points or 0 for p in picks)
    pending = [p for p in picks if p.graded_at is None]
    summary = [f"Record: **{wins}-{losses}** · **{points:,}** pts"]
    if pending:
        summary.append(f"Pending: {plural(len(pending), 'pick')} · up to {sum(p.points_if_right for p in pending):,} pts to win")
    if hidden:
        summary.append(f"🔒 {hidden} more picks hidden until those fights lock")
    if not picks and not hidden:
        summary = ["No picks on this card."]
    embed.description = "\n".join(summary)

    lines = []
    for pick in picks:
        if pick.result in ("win", "loss"):
            score = f"{pick.points:+,} pts"
        elif pick.result == "void":
            score = "void"
        else:
            score = f"+{pick.points_if_right} pts if right"
        lines.append(
            f"{RESULT_ICON.get(pick.result, '⏳')} **{keep(pick.athlete_name)}** "
            + join([f"{fmt_odds(pick.odds)} over {surname(pick.opponent_name)}", score])
        )

    add_chunked_fields(embed, "Picks", lines)
    return stamp(embed)
