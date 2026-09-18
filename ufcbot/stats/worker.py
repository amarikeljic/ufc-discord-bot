"""The refresh job: download, validate, rebuild career totals, retrain, compile.

This is the only part of the bot that needs pandas and scikit-learn, and it runs
in a process of its own so their ~150 MB never lands in the bot. It leaves two
files behind, both of which the bot can read with nothing but the standard
library:

``career.pkl``  every fighter's career totals and tale of the tape
``ufc_model.pkl``  the trained model, compiled to plain numbers

Everything here is blocking and is called through
:class:`~ufcbot.stats.service.StatsService`.
"""

from __future__ import annotations

import hashlib
import logging
import pickle
import shutil
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

from .career import FighterInfo, Ledger
from .scorer import MODEL_FILE

log = logging.getLogger(__name__)

CAREER_FILE = "career.pkl"
CAREER_VERSION = 1

# How far back the honest evaluation window reaches when training.
HOLDOUT_MONTHS = 18

# A download that parses to fewer fights than this is not the real dataset.
MIN_FIGHTS = 5000
# Upstream occasionally drops a handful of rows; more than this is a bad file.
MAX_FIGHT_LOSS = 50


@dataclass(slots=True)
class CareerData:
    """What the bot needs from the dataset, with the data frames left behind."""

    ledgers: dict[str, Ledger] = field(default_factory=dict)
    """Final career totals keyed by normalised name."""
    fighters: dict[str, FighterInfo] = field(default_factory=dict)
    """Tale of the tape keyed by normalised name."""
    fight_count: int = 0
    newest_event: date | None = None
    fingerprint: str = ""
    """Digest of the CSVs these totals were built from."""
    version: int = CAREER_VERSION

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".part")
        tmp.write_bytes(pickle.dumps(self, protocol=pickle.HIGHEST_PROTOCOL))
        tmp.replace(path)

    @classmethod
    def load(cls, path: Path) -> CareerData:
        data = pickle.loads(path.read_bytes())
        if not isinstance(data, cls) or data.version != CAREER_VERSION:
            raise ValueError("Career file was written by a different version; rebuild it.")
        return data


@dataclass(slots=True)
class RefreshOutcome:
    """What a refresh did, in terms the bot can report without loading anything."""

    downloaded: bool = False
    retrained: bool = False
    rebuilt: bool = False
    fight_count: int = 0
    newest_event: date | None = None
    message: str = ""
    error: str | None = None


def fingerprint(data_dir: Path) -> str:
    """A digest of the dataset CSVs, so a rebuild is skipped when nothing moved."""
    from .dataset import FILES

    digest = hashlib.sha1()
    for name in FILES:
        path = data_dir / name
        digest.update(name.encode("utf-8"))
        digest.update(path.read_bytes() if path.exists() else b"")
    return digest.hexdigest()


def _existing_career(path: Path) -> CareerData | None:
    try:
        return CareerData.load(path)
    except (OSError, ValueError, pickle.UnpicklingError, AttributeError, ImportError):
        return None


def _validate(candidate, previous: CareerData | None) -> str | None:
    """Reject a download that would lose data or looks malformed."""
    if candidate.fight_count < MIN_FIGHTS:
        return f"only {candidate.fight_count} fights parsed"
    if candidate.newest_event is None:
        return "no event dates parsed"
    if candidate.newest_event > date.today() + timedelta(days=1):
        return f"newest event is in the future ({candidate.newest_event})"
    if previous is not None:
        if candidate.fight_count < previous.fight_count - MAX_FIGHT_LOSS:
            return f"fight count fell from {previous.fight_count} to {candidate.fight_count}"
        if previous.newest_event and candidate.newest_event < previous.newest_event:
            return f"newest event went backwards to {candidate.newest_event}"
    if len(candidate.fight_stats) < candidate.fight_count:
        return "fight statistics table is incomplete"
    return None


def refresh(data_dir: str, model_dir: str, *, force_retrain: bool = False, download: bool = True) -> RefreshOutcome:
    """Bring the dataset, career totals and model up to date. Blocking; no bot state involved."""
    # Imported here rather than at module scope so that merely importing this
    # module, which the bot does to read the file formats, costs nothing.
    from . import dataset as ds
    from .career import build_history
    from .model import train
    from .scorer import CompiledModel

    if not logging.getLogger().handlers:
        # A spawned worker starts with no logging set up, and training has
        # things worth saying.
        logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    data_path, model_path = Path(data_dir), Path(model_dir)
    career_path = data_path / CAREER_FILE
    model_file = model_path / MODEL_FILE

    previous = _existing_career(career_path)
    outcome = RefreshOutcome(
        fight_count=previous.fight_count if previous else 0,
        newest_event=previous.newest_event if previous else None,
    )

    staging = data_path / ".staging"
    shutil.rmtree(staging, ignore_errors=True)
    try:
        if download:
            outcome.downloaded = ds.download(data_path, staging=staging)
            if outcome.downloaded:
                candidate = ds.load(staging)
                problem = _validate(candidate, previous)
                if problem:
                    outcome.error = f"Rejected upstream data: {problem}"
                    outcome.message = outcome.error
                    log.warning(outcome.error)
                    # The ETags were recorded as the files came down. Forgetting
                    # them is what lets the next attempt fetch the file again
                    # instead of being told it has not changed and never seeing
                    # the fix.
                    (data_path / ds.ETAG_FILE).unlink(missing_ok=True)
                    return outcome
                del candidate
                ds.promote(staging, data_path)
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    if not (data_path / ds.FILES[0]).exists():
        outcome.error = "No dataset on disk and nothing downloaded."
        outcome.message = outcome.error
        return outcome

    digest = fingerprint(data_path)
    needs_rebuild = previous is None or previous.fingerprint != digest

    saved_model = None
    if model_file.exists():
        try:
            saved_model = CompiledModel.load(model_file)
        except Exception as exc:  # a bad model file is not fatal; retrain over it
            log.warning("Saved model unusable (%s); it will be retrained.", exc)

    needs_retrain = (
        force_retrain
        or saved_model is None
        or needs_rebuild
        or (previous is not None and saved_model.dataset_newest != previous.newest_event)
    )

    if not needs_rebuild and not needs_retrain:
        outcome.message = "Dataset is current."
        return outcome

    dataset = ds.load(data_path)
    newest = dataset.newest_event
    history = build_history(dataset, keep_snapshots=needs_retrain)

    if needs_retrain:
        holdout = date.today() - timedelta(days=30 * HOLDOUT_MONTHS)
        model = train(
            history,
            dataset.fighters,
            dataset_newest=newest,
            holdout_from=holdout,
            fights=dataset.fights,
        )
        model.save(model_file)
        outcome.retrained = True
        if model.evaluation:
            outcome.message = model.evaluation.summary()
        history.snapshots.clear()

    CareerData(
        ledgers=history.ledgers,
        # The tale of the tape covers every fighter ufcstats.com lists, including
        # ones with no UFC fight. Those are looked up by the same key as a career
        # ledger, so without one they can never be reached; they only make the
        # file, and the memory the bot holds it in, bigger.
        fighters={key: info for key, info in dataset.fighters.items() if key in history.ledgers},
        fight_count=dataset.fight_count,
        newest_event=newest,
        fingerprint=digest,
    ).save(career_path)

    outcome.rebuilt = True
    outcome.fight_count = dataset.fight_count
    outcome.newest_event = newest
    prefix = "Downloaded new data and " if outcome.downloaded else ""
    action = "retrained the model" if outcome.retrained else "rebuilt career stats"
    outcome.message = f"{prefix}{action}. {outcome.message}".strip()
    log.info("Stats refresh: %s", outcome.message)
    return outcome
