"""The bot-facing facade over career stats and the model.

Responsibilities:

* Load what the last refresh left on disk and serve stats and predictions from
  memory, using nothing heavier than the standard library.
* Hand the refresh -- download, validate, rebuild, retrain -- to a separate
  process, then pick up its two files and swap them in atomically.
* Know when the dataset is behind the real calendar so the bot can say so.

The split matters for more than tidiness: pandas and scikit-learn cost about
150 MB resident, and the bot would hold that for the sake of a job that runs
once a day. Keeping them in the refresh process leaves the bot with career
totals, a compiled model and its own data.
"""

from __future__ import annotations

import asyncio
import logging
import multiprocessing
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from functools import partial
from pathlib import Path

from ..models import Event
from .career import FighterInfo, Ledger
from .features import fighter_features
from .names import NameIndex
from .prediction import Prediction
from .scorer import MODEL_FILE, CompiledModel
from .worker import CAREER_FILE, CareerData, RefreshOutcome
from .worker import refresh as run_refresh

log = logging.getLogger(__name__)

# Upstream refreshes the morning after a card; allow a little slack before
# calling the dataset behind.
UPSTREAM_GRACE = timedelta(days=2)

# Cards whose picks are remembered before the oldest are dropped.
PICK_CACHE_CARDS = 64


@dataclass(slots=True)
class RefreshResult:
    downloaded: bool
    retrained: bool
    fights: int
    newest_event: date | None
    message: str


@dataclass(slots=True)
class FighterCareer:
    ledger: Ledger
    info: FighterInfo | None

    @property
    def name(self) -> str:
        return self.ledger.name


class StatsService:
    def __init__(
        self,
        data_dir: Path,
        model_dir: Path,
        *,
        refresh_interval_hours: int = 24,
    ) -> None:
        self.data_dir = data_dir
        self.model_dir = model_dir
        self.career_path = data_dir / CAREER_FILE
        self.model_path = model_dir / MODEL_FILE
        self.refresh_interval = timedelta(hours=refresh_interval_hours)

        self.careers: CareerData | None = None
        self.model: CompiledModel | None = None
        self.names: NameIndex | None = None
        self._resolved: dict[str, str | None] = {}
        self._card_picks: dict[tuple, dict[str, Prediction]] = {}

        self.last_check: datetime | None = None
        self.last_error: str | None = None
        self.expected_newest: date | None = None
        self._lock = asyncio.Lock()
        self._can_spawn = True
        """Cleared once this environment turns out not to allow worker processes."""

    # -- lifecycle ---------------------------------------------------------

    @property
    def ready(self) -> bool:
        return self.careers is not None

    @property
    def can_predict(self) -> bool:
        return self.ready and self.model is not None

    async def start(self) -> None:
        """Load what is already on disk. Never raises; the bot runs without stats."""
        try:
            await asyncio.to_thread(self._load_from_disk)
        except FileNotFoundError:
            log.info("No career stats on disk yet; the first refresh will build them.")
        except Exception as exc:  # keep the bot up regardless
            self.last_error = f"Failed to load stats: {exc}"
            log.exception("Failed to load stats from disk")

    def _load_from_disk(self) -> None:
        careers = CareerData.load(self.career_path)
        model = None
        if self.model_path.exists():
            try:
                model = CompiledModel.load(self.model_path)
                if model.dataset_newest != careers.newest_event:
                    log.info("Model predates the career stats; it will be retrained on next refresh.")
            except Exception as exc:  # a bad model file is not fatal
                log.warning("Saved model unusable (%s); it will be retrained.", exc)
        self._install(careers, model)
        log.info(
            "Stats ready: %d fights through %s, model %s",
            careers.fight_count,
            careers.newest_event,
            "loaded" if model else "missing",
        )

    def _install(self, careers: CareerData, model: CompiledModel | None) -> None:
        self.careers = careers
        self.model = model
        self.names = NameIndex(ledger.name for ledger in careers.ledgers.values())
        # New data or a new model makes every remembered answer stale.
        self._resolved.clear()
        self._card_picks.clear()
        self.last_error = None

    # -- refresh -------------------------------------------------------------

    @property
    def is_behind(self) -> bool:
        """True when a card has happened that the dataset does not yet include."""
        newest = self._newest()
        if newest is None or self.expected_newest is None:
            return False
        return newest + UPSTREAM_GRACE < self.expected_newest

    def needs_check(self, now: datetime | None = None) -> bool:
        now = now or datetime.now()
        if not self.can_predict:
            return True
        if self.last_check is None:
            return True
        if self.is_behind:
            # Upstream normally lands the day after a card; poll a little faster.
            return now - self.last_check >= timedelta(hours=6)
        return now - self.last_check >= self.refresh_interval

    async def refresh(self, *, force_retrain: bool = False) -> RefreshResult:
        """Pull upstream changes, validate, rebuild, retrain, swap. Safe to call often."""
        if self._lock.locked():
            return RefreshResult(False, False, self._fight_count(), self._newest(), "A refresh is already running.")
        async with self._lock:
            self.last_check = datetime.now()
            try:
                outcome = await self._run_refresh(force_retrain)
            except Exception as exc:  # surfaced through status
                self.last_error = f"{type(exc).__name__}: {exc}"
                log.exception("Stats refresh failed")
                return RefreshResult(False, False, self._fight_count(), self._newest(), self.last_error)

            # Either way this is the current state of things, so a refresh that
            # goes through clears whatever the last one complained about.
            self.last_error = outcome.error
            if outcome.rebuilt or (self.careers is None and self.career_path.exists()):
                try:
                    await asyncio.to_thread(self._load_from_disk)
                except Exception as exc:
                    self.last_error = f"Refresh finished but its output would not load: {exc}"
                    log.exception("Could not load refreshed stats")

            return RefreshResult(
                downloaded=outcome.downloaded,
                retrained=outcome.retrained,
                fights=self._fight_count(),
                newest_event=self._newest(),
                message=outcome.message or "Dataset is current.",
            )

    async def _run_refresh(self, force_retrain: bool) -> RefreshOutcome:
        """Run the refresh in its own process, falling back to this one if that fails.

        Only failures to *start* the worker fall back. Once it is running, what it
        raises is a real problem with the refresh and is reported as one rather
        than repeated here at the cost of another few minutes.
        """
        call = partial(run_refresh, str(self.data_dir), str(self.model_dir), force_retrain=force_retrain)
        if not self._can_spawn:
            return await asyncio.to_thread(call)

        loop = asyncio.get_running_loop()
        pool = None
        try:
            pool = ProcessPoolExecutor(max_workers=1, mp_context=multiprocessing.get_context("spawn"))
            # run_in_executor submits before it returns, so a worker that cannot
            # start fails here rather than at the await below.
            running = loop.run_in_executor(pool, call)
        except Exception as exc:
            if pool is not None:
                pool.shutdown(wait=False)
            self._can_spawn = False
            log.warning(
                "The refresh cannot run in a separate process here (%r); running it in the bot's "
                "instead, which will use noticeably more memory.",
                exc,
            )
            return await asyncio.to_thread(call)

        try:
            return await running
        finally:
            # Waiting for shutdown is what actually returns the worker's memory.
            await asyncio.to_thread(pool.shutdown, True)

    def _fight_count(self) -> int:
        return self.careers.fight_count if self.careers else 0

    def _newest(self) -> date | None:
        return self.careers.newest_event if self.careers else None

    # -- queries -----------------------------------------------------------

    def resolve(self, name: str) -> str | None:
        """Dataset key for a fighter name. Remembered, since fuzzy matching is the slow path."""
        if self.names is None:
            return None
        if name not in self._resolved:
            self._resolved[name] = self.names.resolve(name)
        return self._resolved[name]

    def suggest(self, name: str, limit: int = 5) -> list[str]:
        return self.names.suggest(name, limit) if self.names else []

    def career(self, name: str) -> FighterCareer | None:
        if self.careers is None:
            return None
        key = self.resolve(name)
        if key is None:
            return None
        ledger = self.careers.ledgers.get(key)
        if ledger is None:
            return None
        return FighterCareer(ledger=ledger, info=self.careers.fighters.get(key))

    def predict(
        self,
        name_a: str,
        name_b: str,
        *,
        title_fight: bool = False,
        scheduled_rounds: int = 3,
        on: date | None = None,
        weight_class: str | None = None,
    ) -> Prediction | None:
        """Win probability for A over B, or None if either fighter is unknown."""
        if not self.can_predict:
            return None
        a = self.career(name_a)
        b = self.career(name_b)
        if a is None or b is None:
            return None
        on = on or date.today()
        return self.model.predict(
            fighter_features(a.ledger, a.info, on),
            fighter_features(b.ledger, b.info, on),
            title_fight=title_fight,
            scheduled_rounds=scheduled_rounds,
            name_a=a.name,
            name_b=b.name,
            ledger_a=a.ledger,
            ledger_b=b.ledger,
            weight_class=weight_class,
        )

    def predict_event(self, event: Event) -> dict[str, Prediction]:
        """Picks for every bout on a card that has two known fighters, keyed by bout id.

        A card's picks only change when its fights or the model change, so they are
        remembered: board refreshes and card commands would otherwise re-run the
        model for every fight each time.
        """
        if not self.can_predict:
            return {}
        key = (
            event.id,
            event.start.date(),
            tuple(
                (
                    bout.id,
                    # Both, so that a fighter being replaced and a name being
                    # corrected each retire the remembered picks.
                    tuple((f.id, f.display_name) for f in bout.fighters[:2]),
                    bout.rounds,
                    bout.weight_class,
                    bout.completed,
                )
                for bout in event.bouts
            ),
        )
        cached = self._card_picks.get(key)
        if cached is None:
            if len(self._card_picks) > PICK_CACHE_CARDS:
                self._card_picks.clear()
            cached = self._card_picks[key] = self._predict_card(event)
        return dict(cached)

    def _predict_card(self, event: Event) -> dict[str, Prediction]:
        picks: dict[str, Prediction] = {}
        on = event.start.date()
        for bout in event.bouts:
            if not bout.has_opponents or bout.completed:
                continue
            title = bool(bout.weight_class and "title" in bout.weight_class.lower())
            prediction = self.predict(
                bout.fighters[0].display_name,
                bout.fighters[1].display_name,
                title_fight=title or bout.rounds == 5,
                scheduled_rounds=bout.rounds or 3,
                on=on,
                weight_class=bout.weight_class,
            )
            if prediction is not None:
                picks[bout.id] = prediction
        return picks

    # -- status ------------------------------------------------------------

    def status_lines(self) -> list[str]:
        lines = []
        if self.careers is None:
            lines.append("Dataset: not loaded")
        else:
            lines.append(f"Dataset: {self.careers.fight_count:,} fights through {self.careers.newest_event:%b %d, %Y}")
            if self.is_behind:
                lines.append(f"⚠️ Behind: a card on {self.expected_newest:%b %d} is not in the data yet")
        if self.model is None:
            lines.append("Model: not trained")
        else:
            lines.append(f"Model: trained {self.model.trained_at:%b %d, %Y %H:%M} on {self.model.training_fights:,} fights")
            if self.model.evaluation:
                lines.append(f"Accuracy: {self.model.evaluation.summary()}")
                method = self.model.evaluation.method_summary()
                if method:
                    lines.append(f"Method: {method}")
        if self.last_check:
            lines.append(f"Last upstream check: {self.last_check:%b %d, %Y %H:%M}")
        if self.last_error:
            lines.append(f"Last error: {self.last_error}")
        return lines
