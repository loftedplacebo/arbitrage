from __future__ import annotations

import argparse
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from strategy.config import DEFAULT_CONFIG


STATE_FILES = [
    "positions.csv",
    "slices.csv",
    "fills.csv",
    "decisions.csv",
    "processed_scans.csv",
]


def archive_strategy_state(data_dir: Path) -> Path | None:
    data_dir.mkdir(parents=True, exist_ok=True)
    existing_files = [
        data_dir / name
        for name in STATE_FILES
        if (data_dir / name).exists()
    ]

    if not existing_files:
        print(f"No strategy state files found in {data_dir}. Nothing to archive.")
        return None

    archive_root = data_dir / "archive"
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    archive_dir = archive_root / timestamp
    archive_dir.mkdir(parents=True, exist_ok=True)

    for source in existing_files:
        target = archive_dir / source.name
        shutil.move(str(source), str(target))
        print(f"Moved {source} -> {target}")

    print(f"Archived {len(existing_files)} strategy state files to {archive_dir}")
    return archive_dir


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Archive current paper strategy state CSVs.")
    parser.add_argument("--data-dir", default=str(DEFAULT_CONFIG.data_dir))
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    archive_strategy_state(Path(args.data_dir))
