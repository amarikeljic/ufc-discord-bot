"""Download the latest fight data and train the prediction model.

Run with:  python train.py            (refresh data if changed, retrain)
           python train.py --force    (retrain even when nothing changed)

The bot runs this same job on its own, in a process of its own, so this is only
needed to train ahead of first launch or to inspect the evaluation numbers.
"""

from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path

from dotenv import load_dotenv


def main(force: bool) -> int:
    from ufcbot.stats.scorer import MODEL_FILE, CompiledModel
    from ufcbot.stats.worker import refresh

    load_dotenv()
    data_dir = Path(os.getenv("DATA_DIR", "data"))
    model_dir = Path(os.getenv("MODEL_DIR", "models"))

    outcome = refresh(str(data_dir), str(model_dir), force_retrain=force)

    print()
    print(outcome.message or "Nothing to do.")
    if outcome.error:
        return 1
    print(f"Dataset: {outcome.fight_count:,} fights through {outcome.newest_event}")

    model_path = model_dir / MODEL_FILE
    if not model_path.exists():
        return 1
    model = CompiledModel.load(model_path)
    if model.evaluation:
        print(model.evaluation.summary())
        method = model.evaluation.method_summary()
        if method:
            print(method)
    if model.importances:
        print("\nMost influential differences between fighters:")
        for name, value in model.importances[:10]:
            print(f"  {name[2:]:<26} {value:+.4f}")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--force", action="store_true", help="retrain even if the data is unchanged")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("sklearn").setLevel(logging.WARNING)
    raise SystemExit(main(args.force))
