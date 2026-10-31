"""Idempotent SQLite initializer which refuses to hide existing legacy data."""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from ..runtime.lock import InstanceAlreadyRunning, SingleInstanceLock
from .errors import PersistenceError, StorageConflictError
from .io import secure_directory
from .layout import LAYOUT_FILE, STORE_SPECS
from .sqlite_backend import SQLiteBackend


@dataclass(frozen=True, slots=True)
class BootstrapReport:
    already_initialized: bool
    revision: int
    store_count: int
    data_dir: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def initialize_empty_layout(data_root: Path | str, *, instance_lock: Path | None = None) -> BootstrapReport:
    """Create SQLite only when no legacy or partially migrated source exists."""
    root = Path(data_root).resolve()
    with SingleInstanceLock(instance_lock or root / "runtime/instance.lock"):
        return _initialize(root)


def _initialize(root: Path) -> BootstrapReport:
    backend = SQLiteBackend(root)
    if backend.exists():
        snapshot = backend.snapshot()
        return BootstrapReport(
            already_initialized=True,
            revision=snapshot.revision,
            store_count=len(snapshot.stores),
            data_dir=str(root),
        )
    if not (root / LAYOUT_FILE).exists() and any(
        (root / name).exists() for name in ("user_data.json", "important_data.json")
    ):
        raise StorageConflictError("unsupported monolithic data found; only split JSON layout can be migrated")
    legacy_paths = (
        LAYOUT_FILE,
        ".transactions",
        "telegram-migration.pickle",
        "telegram/persistence.pickle",
        "telegram/persistence.pickle.bak",
        "ptb_persistence",
        "ptb_persistence.bak",
        *(spec.relative_path for spec in STORE_SPECS.values()),
    )
    existing = [name for name in legacy_paths if (root / name).exists()]
    if existing:
        raise StorageConflictError(f"legacy data found ({', '.join(existing)}); use app.persistence.migrate_sqlite")
    secure_directory(root)
    snapshot = backend.bootstrap()
    return BootstrapReport(
        already_initialized=False,
        revision=snapshot.revision,
        store_count=len(snapshot.stores),
        data_dir=str(root),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Initialize a new SQLite database without replacing legacy data.")
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--instance-lock", type=Path)
    args = parser.parse_args()
    try:
        report = initialize_empty_layout(args.data_dir, instance_lock=args.instance_lock)
    except (PersistenceError, InstanceAlreadyRunning, OSError) as exc:
        parser.exit(2, f"initialization refused: {exc}\n")
    print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["BootstrapReport", "initialize_empty_layout", "main"]
