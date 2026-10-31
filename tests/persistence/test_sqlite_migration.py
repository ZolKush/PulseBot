from __future__ import annotations

import pickle
import sqlite3
from pathlib import Path

import pytest
from telegram.ext import ExtBot

from app.bot.persistence_sqlite import SQLitePersistence
from app.persistence import MigrationError, SchemaError, SplitJsonBackend, SQLiteBackend, StorageConflictError
from app.persistence.layout import default_store_data
from app.persistence.migrate_sqlite import migrate_to_sqlite
from app.runtime.lock import InstanceAlreadyRunning, SingleInstanceLock


def _files(root: Path) -> dict[str, bytes]:
    return {path.relative_to(root).as_posix(): path.read_bytes() for path in root.rglob("*") if path.is_file()}


def _pickle() -> bytes:
    return pickle.dumps(
        {
            "user_data": {42: {"pending_reply": 3}},
            "chat_data": {-42: {"panel": 17}},
            "bot_data": {"navigation": {"42": [17, 18]}},
            "callback_data": ([("button", 1.0, {"request": 5})], {"query": "button"}),
            "conversations": {"support": {(42, 42): 8}},
        }
    )


@pytest.mark.asyncio
async def test_split_sqlite_migration_dry_run_backup_restart_and_idempotence(tmp_path: Path) -> None:
    root = tmp_path / "data"
    stores = default_store_data()
    stores["users.profiles"] = {"42": {"user_id": 42, "nickname": "Example"}}
    stores["access.grants"] = {"42": {"role": "user", "access_state": "approved"}}
    stores["subscriptions.accounts"] = {"42": {"service_tier": "basic"}}
    stores["subscriptions.requests"] = {
        "next_id": 5,
        "items": {"5": {"id": 5, "user_id": 42, "kind": "purchase", "status": "pending", "amount_rub": 300}},
    }
    stores["subscriptions.billing_settings"] = {"standard_price_rub": 300, "payment_bank": "Example Bank"}
    stores["settings.help_and_contacts"] = {"help_text": "Помощь", "support_email": "support@example.com"}
    stores["support.tickets"] = {"next_id": 7, "items": {"7": {"id": 7, "user_id": 42}}}
    stores["support.ticket_messages"] = {
        "7": [{"text": "Нужна помощь", "attachments": [{"type": "photo", "file_id": "example-photo"}]}]
    }
    stores["maintenance.state"] = {
        "active": {"active": True, "id": "work-1", "expected_end": "2026-11-01T12:00:00+00:00"},
        "scheduled": {"id": "work-2", "start_at": "2026-11-02T10:00:00+00:00"},
    }
    stores["messaging.outbox"] = {
        "event-1": {
            "id": "event-1",
            "origin": "important",
            "kind": "message",
            "payload": {"text": "Уведомление"},
            "recipients": {"42": {"status": "pending", "attempts": 2, "part_index": 1}},
            "completion": {"type": "fail2ban_cursor", "server_key": "node", "cursor": {"offset": 123}},
        }
    }
    stores["audit.events"] = [{"action": "request_created", "target_user_id": 42}]
    stores["monitoring.dns_cache"] = {"node": {"status": "ok", "addresses": ["192.0.2.1"]}}
    stores["monitoring.node_status_cache"] = {"node": {"online": False, "cpu": None}}
    stores["monitoring.docker_cache"] = {
        "node": {"containers": [{"name": "example", "running": True, "status": "Up", "restarts": "2"}]}
    }
    stores["monitoring.tls_state"] = {"example.com:443": {"domain": "example.com", "status": "expiring"}}
    stores["monitoring.fail2ban_cursors"] = {"node": {"inode": 100, "offset": 123}}
    assert all(value != default_store_data()[name] for name, value in stores.items())
    SplitJsonBackend(root).bootstrap(stores=stores)
    ptb = root / "telegram/persistence.pickle"
    ptb.parent.mkdir()
    ptb.write_bytes(_pickle())
    before = _files(root)

    dry_run = migrate_to_sqlite(root, dry_run=True)
    assert dry_run.dry_run and dry_run.telegram_imported
    assert _files(root) == before

    report = migrate_to_sqlite(root)
    assert not report.already_migrated
    snapshot = SQLiteBackend(root).inspect()
    assert {name: snapshot.data(name) for name in stores} == stores
    for name, payload in before.items():
        if name != "runtime/state.lock":
            assert (root / name).read_bytes() == payload
            assert (Path(report.backup_path) / name).read_bytes() == payload
    with sqlite3.connect(report.database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM telegram_conversations").fetchone()[0] == 1
    telegram = SQLitePersistence(report.database)
    telegram.set_bot(ExtBot("123:LOCAL_ONLY"))
    legacy_state = pickle.loads(_pickle())
    assert await telegram.get_user_data() == legacy_state["user_data"]
    assert await telegram.get_chat_data() == legacy_state["chat_data"]
    assert await telegram.get_bot_data() == legacy_state["bot_data"]
    assert await telegram.get_callback_data() == legacy_state["callback_data"]
    assert await telegram.get_conversations("support") == legacy_state["conversations"]["support"]
    await telegram.update_user_data(42, {"pending_reply": 9})
    with SQLiteBackend(root).unit_of_work() as uow:
        uow.profiles.put(43, {"user_id": 43})
        uow.access.put(43, {"role": "user", "access_state": "approved"})
        uow.subscriptions.put(43, {"service_tier": "basic"})
        uow.commit()
    assert migrate_to_sqlite(root).already_migrated
    assert SQLiteBackend(root).inspect().data("users.profiles")["43"] == {"user_id": 43}
    restarted = SQLitePersistence(report.database)
    restarted.set_bot(ExtBot("456:RESTARTED"))
    assert await restarted.get_user_data() == {42: {"pending_reply": 9}}


@pytest.mark.parametrize("dry_run", [True, False])
def test_monolithic_source_is_rejected_without_import_or_backup(tmp_path: Path, dry_run: bool) -> None:
    root = tmp_path / "data"
    root.mkdir()
    sources = {name: b"{}\n" for name in ("user_data.json", "important_data.json")}
    for name, payload in sources.items():
        (root / name).write_bytes(payload)

    with pytest.raises(MigrationError, match="storage_layout.json"):
        migrate_to_sqlite(root, dry_run=dry_run)

    assert all((root / name).read_bytes() == payload for name, payload in sources.items())
    assert not SQLiteBackend(root).exists()
    assert not (root / "sqlite-backups").exists()
    assert not (root / "storage_layout.json").exists()


def test_split_layout_ignores_obsolete_monolithic_files(tmp_path: Path) -> None:
    before = SplitJsonBackend(tmp_path).bootstrap()
    for name in ("user_data.json", "important_data.json"):
        (tmp_path / name).write_bytes(b"unsupported obsolete source")

    report = migrate_to_sqlite(tmp_path)

    assert report.source_format == "split-json-v1"
    imported = SQLiteBackend(tmp_path).inspect()
    assert {name: item.data for name, item in imported.stores.items()} == {
        name: item.data for name, item in before.stores.items()
    }
    for name in ("user_data.json", "important_data.json"):
        assert (tmp_path / name).read_bytes() == b"unsupported obsolete source"
        assert not (Path(report.backup_path) / name).exists()


def test_missing_split_store_is_rejected_without_database_or_backup(tmp_path: Path) -> None:
    SplitJsonBackend(tmp_path).bootstrap()
    (tmp_path / "support/tickets.json").unlink()

    with pytest.raises(FileNotFoundError):
        migrate_to_sqlite(tmp_path)

    assert not SQLiteBackend(tmp_path).exists()
    assert not (tmp_path / "sqlite-backups").exists()


def test_configured_external_telegram_pickle_is_backed_up_and_imported(tmp_path: Path) -> None:
    root = tmp_path / "data"
    SplitJsonBackend(root).bootstrap()
    ptb = tmp_path / "custom-telegram.pickle"
    ptb.write_bytes(_pickle())
    report = migrate_to_sqlite(root, ptb_path=ptb)
    assert report.telegram_imported
    assert (Path(report.backup_path) / "_external/telegram/persistence.pickle").read_bytes() == _pickle()
    assert ptb.read_bytes() == _pickle()


def test_invalid_pickle_never_publishes_an_application_only_database(tmp_path: Path) -> None:
    root = tmp_path / "data"
    SplitJsonBackend(root).bootstrap()
    path = root / "telegram/persistence.pickle"
    path.parent.mkdir()
    path.write_bytes(b"invalid")
    with pytest.raises(ValueError):
        migrate_to_sqlite(root)
    assert not SQLiteBackend(root).exists()


@pytest.mark.parametrize("dry_run", [True, False])
def test_incomplete_user_projections_are_rejected_before_backup(tmp_path: Path, dry_run: bool) -> None:
    stores = default_store_data()
    stores["users.profiles"] = {"42": {"user_id": 42}}
    SplitJsonBackend(tmp_path).bootstrap(stores=stores)
    with pytest.raises(SchemaError, match="access.grants.42"):
        migrate_to_sqlite(tmp_path, dry_run=dry_run)
    assert not SQLiteBackend(tmp_path).exists()
    assert not (tmp_path / "sqlite-backups").exists()


def test_sqlite_migration_respects_running_bot_lock(tmp_path: Path) -> None:
    SplitJsonBackend(tmp_path).bootstrap()
    with SingleInstanceLock(tmp_path / "runtime/instance.lock"), pytest.raises(InstanceAlreadyRunning):
        migrate_to_sqlite(tmp_path)
    assert not SQLiteBackend(tmp_path).exists()


def test_migration_refuses_fresh_unrelated_sqlite_database(tmp_path: Path) -> None:
    SQLiteBackend(tmp_path).bootstrap()
    with pytest.raises(StorageConflictError, match="not created by this migration"):
        migrate_to_sqlite(tmp_path)


def test_pending_legacy_bootstrap_is_not_bypassed_when_manifest_is_missing(tmp_path: Path) -> None:
    root = tmp_path / "data"
    root.mkdir()
    for name in ("user_data.json", "important_data.json"):
        (root / name).write_text("{}\n", encoding="utf-8")
    pending = root / ".transactions/pending"
    pending.mkdir(parents=True)
    with pytest.raises(StorageConflictError, match="pending JSON transaction"):
        migrate_to_sqlite(root, dry_run=True)
    assert not SQLiteBackend(root).exists()


def test_source_change_during_backup_does_not_publish(tmp_path: Path, monkeypatch) -> None:
    from app.persistence import migrate_sqlite

    SplitJsonBackend(tmp_path).bootstrap()
    original = migrate_sqlite.backup_source

    def change_source(root, source):
        backup = original(root, source)
        with SplitJsonBackend(root).unit_of_work() as uow:
            uow.profiles.put(42, {"user_id": 42})
            uow.commit()
        return backup

    monkeypatch.setattr(migrate_sqlite, "backup_source", change_source)
    with pytest.raises(MigrationError, match="source changed"):
        migrate_to_sqlite(tmp_path)
    assert not SQLiteBackend(tmp_path).exists()


def test_migration_refuses_a_source_directory_symlink(tmp_path: Path) -> None:
    root = tmp_path / "data"
    SplitJsonBackend(root).bootstrap()
    users = root / "users"
    outside = tmp_path / "outside-users"
    users.rename(outside)
    try:
        users.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks require platform support")
    with pytest.raises(MigrationError, match="symbolic links"):
        migrate_to_sqlite(root, dry_run=True)
    assert not SQLiteBackend(root).exists()
