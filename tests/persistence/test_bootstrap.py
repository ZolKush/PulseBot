from pathlib import Path

import pytest

from app.persistence import SQLiteBackend, StorageConflictError
from app.persistence.bootstrap import initialize_empty_layout
from app.persistence.layout import STORE_SPECS


def test_empty_layout_bootstrap_is_explicit_and_idempotent(tmp_path: Path) -> None:
    root = tmp_path / "data"

    first = initialize_empty_layout(root)
    second = initialize_empty_layout(root)
    snapshot = SQLiteBackend(root).inspect()

    assert first.already_initialized is False
    assert second.already_initialized is True
    assert first.revision == second.revision == snapshot.revision == 1
    assert first.store_count == second.store_count == len(STORE_SPECS)
    assert (root / "maintbot.sqlite3").is_file()
    assert set(snapshot.stores) == set(STORE_SPECS)
    assert all(store.revision == 1 for store in snapshot.stores.values())
    assert all(snapshot.data(name) == spec.default_data() for name, spec in STORE_SPECS.items())
    assert not (root / "storage_layout.json").exists()
    assert not any(root.rglob("*.json"))


@pytest.mark.parametrize("source_name", ["user_data.json", "important_data.json"])
def test_empty_layout_bootstrap_refuses_monolithic_source(tmp_path: Path, source_name: str) -> None:
    root = tmp_path / "data"
    root.mkdir()
    (root / source_name).write_text("{}\n", encoding="utf-8")

    with pytest.raises(StorageConflictError, match="unsupported monolithic data"):
        initialize_empty_layout(root)

    assert not (root / "maintbot.sqlite3").exists()


@pytest.mark.parametrize(
    "source_name",
    ["users/profiles.json", "telegram/persistence.pickle", "ptb_persistence.bak", "telegram-migration.pickle"],
)
def test_bootstrap_refuses_partial_legacy_data(tmp_path: Path, source_name: str) -> None:
    source = tmp_path / source_name
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"legacy")
    with pytest.raises(StorageConflictError, match="legacy data found"):
        initialize_empty_layout(tmp_path)
    assert source.read_bytes() == b"legacy"
    assert not (tmp_path / "maintbot.sqlite3").exists()
