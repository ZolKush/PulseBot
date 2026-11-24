"""Transactional SQLite persistence behind the application repository boundary."""

from __future__ import annotations

import contextlib
import copy
import json
import os
import sqlite3
import tempfile
import uuid
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from .errors import CommittedTransactionError, SchemaError, StorageConflictError
from .io import decode_json, fsync_directory, secure_directory, tighten_file_permissions
from .layout import STORE_SPECS, default_store_data
from .locking import StateLock
from .schema import build_store_document
from .snapshots import BackendSnapshot, StoreSnapshot
from .sqlite_records import read_store, write_store
from .sqlite_schema import DATABASE_FILENAME, INDEXED_STORES, SCHEMA_VERSION, create_schema
from .unit_of_work import JsonUnitOfWork


class SQLiteBackend:
    """One SQL transaction publishes entities, delivery state and revisions.

    Construction and imports never create files. SQLite owns crash recovery;
    startup must still acquire the whole-process lock before calling snapshot.
    The rollback journal with synchronous EXTRA also syncs journal deletion.
    """

    def __init__(
        self,
        data_root: Path | str,
        *,
        database_path: Path | str | None = None,
        busy_timeout: float = 5,
        failpoint: Callable[[str], None] | None = None,
    ) -> None:
        self.data_root = Path(data_root).resolve()
        self.database_path = (
            Path(database_path).resolve() if database_path is not None else self.data_root / DATABASE_FILENAME
        )
        if not 0 < busy_timeout <= 60:
            raise ValueError("SQLite busy_timeout must be greater than zero and at most 60 seconds")
        self.busy_timeout = busy_timeout
        self._failpoint = failpoint

    def exists(self) -> bool:
        # A malformed or partial target must be inspected, never replaced.
        return self.database_path.exists()

    def has_pending_transactions(self) -> bool:
        journal = Path(str(self.database_path) + "-journal")
        return journal.is_file() and journal.stat().st_size > 0

    def recover(self) -> list[str]:
        self.snapshot()
        return []

    def verify_recovery(self) -> list[str]:
        """Read-only verification; a hot journal is deliberately not recovered."""

        self.inspect()
        return []

    def inspect(self) -> BackendSnapshot:
        return self._read_snapshot(read_only=True)

    def snapshot(self) -> BackendSnapshot:
        return self._read_snapshot(read_only=False)

    def _read_snapshot(self, *, read_only: bool) -> BackendSnapshot:
        with contextlib.closing(self.connect(read_only=read_only)) as connection:
            connection.execute("BEGIN")
            try:
                result = self._load(connection)
            finally:
                connection.rollback()
            return result

    def bootstrap(
        self,
        *,
        stores: Mapping[str, Any] | None = None,
        migration: dict[str, Any] | None = None,
        transaction_id: str | None = None,
    ) -> BackendSnapshot:
        """Publish a complete new database once; leave existing targets alone."""

        initial = default_store_data() if stores is None else copy.deepcopy(dict(stores))
        if set(initial) != set(STORE_SPECS):
            raise SchemaError("initial store set mismatch")
        for name, data in initial.items():
            build_store_document(name, 1, data)
        if migration is not None and not isinstance(migration, dict):
            raise SchemaError("migration must be an object or null")
        try:
            migration_json = json.dumps(migration, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise SchemaError("migration contains a non-JSON value") from exc
        tx_id = transaction_id or uuid.uuid4().hex
        if not isinstance(tx_id, str) or not tx_id.strip():
            raise SchemaError("transaction_id must be a non-empty string")
        with StateLock(self.data_root):
            if self.exists():
                raise StorageConflictError(f"SQLite database already exists: {self.database_path}")
            secure_directory(self.database_path.parent)
            descriptor, raw_path = tempfile.mkstemp(prefix=".sqlite-bootstrap-", dir=self.database_path.parent)
            os.close(descriptor)
            temporary = Path(raw_path)
            try:
                with contextlib.closing(self._connect_path(temporary, mode="rw")) as connection:
                    connection.execute("PRAGMA journal_mode=DELETE")
                    connection.execute("BEGIN IMMEDIATE")
                    try:
                        create_schema(connection)
                        connection.execute(
                            "INSERT INTO storage_metadata VALUES (1, ?, 1, ?, ?)",
                            (SCHEMA_VERSION, tx_id, migration_json),
                        )
                        for name, data in initial.items():
                            next_id = data["next_id"] if name in INDEXED_STORES else None
                            connection.execute("INSERT INTO store_revisions VALUES (?, 1, ?)", (name, next_id))
                            write_store(connection, name, data)
                        self._load(connection)
                        connection.commit()
                    except BaseException:
                        connection.rollback()
                        raise
                tighten_file_permissions(temporary)
                with temporary.open("rb+") as handle:
                    os.fsync(handle.fileno())
                os.replace(temporary, self.database_path)
                fsync_directory(self.database_path.parent)
            finally:
                with contextlib.suppress(FileNotFoundError):
                    temporary.unlink()
                with contextlib.suppress(FileNotFoundError):
                    Path(str(temporary) + "-journal").unlink()
            return self.snapshot()

    def commit(self, *, base_revision: int, changes: Mapping[str, Any]) -> BackendSnapshot:
        unknown = sorted(set(changes) - set(STORE_SPECS))
        if unknown:
            raise SchemaError(f"unknown stores in commit: {unknown}")
        # Validate the whole mutation before acquiring the SQL writer lock.
        for name, data in changes.items():
            build_store_document(name, 1, data)
        with contextlib.closing(self.connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            committed = False
            tx_id = ""
            try:
                current = self._load(connection)
                if current.revision != base_revision:
                    raise StorageConflictError(
                        f"stale storage revision {base_revision}; current revision is {current.revision}"
                    )
                effective = {name: data for name, data in changes.items() if data != current.stores[name].data}
                if not effective:
                    connection.rollback()
                    return current
                tx_id = uuid.uuid4().hex
                for name, data in effective.items():
                    write_store(connection, name, data)
                    next_id = data["next_id"] if name in INDEXED_STORES else None
                    connection.execute(
                        "UPDATE store_revisions SET revision=revision+1, next_id=? WHERE store_name=?", (next_id, name)
                    )
                    self._checkpoint(f"after_store:{name}")
                connection.execute(
                    "UPDATE storage_metadata SET revision=revision+1, transaction_id=? WHERE singleton=1", (tx_id,)
                )
                result = self._load(connection)
                self._checkpoint("before_commit")
                connection.commit()
                committed = True
                self._checkpoint("after_commit")
                return result
            except BaseException as exc:
                if committed:
                    raise CommittedTransactionError(tx_id) from exc
                connection.rollback()
                raise

    def unit_of_work(self) -> JsonUnitOfWork:
        return JsonUnitOfWork(self)

    def connect(self, *, read_only: bool = False) -> sqlite3.Connection:
        if not self.database_path.is_file():
            raise StorageConflictError(f"SQLite database does not exist: {self.database_path}")
        return self._connect_path(self.database_path, mode="ro" if read_only else "rw")

    def _connect_path(self, path: Path, *, mode: str) -> sqlite3.Connection:
        connection = sqlite3.connect(
            f"{path.as_uri()}?mode={mode}", uri=True, timeout=self.busy_timeout, isolation_level=None
        )
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA synchronous=EXTRA")
            if mode == "ro":
                connection.execute("PRAGMA query_only=ON")
        except BaseException:
            connection.close()
            raise
        return connection

    def _load(self, connection: sqlite3.Connection) -> BackendSnapshot:
        try:
            row = connection.execute(
                "SELECT schema_version, revision, transaction_id, migration FROM storage_metadata WHERE singleton=1"
            ).fetchone()
            if row is None or row[0] != SCHEMA_VERSION:
                raise SchemaError("unsupported or incomplete SQLite storage schema")
            _, revision, tx_id, raw_migration = row
            if not isinstance(revision, int) or revision < 1 or not isinstance(tx_id, str) or not tx_id:
                raise SchemaError("invalid SQLite storage metadata")
            migration = decode_json(raw_migration.encode("utf-8"), source="SQLite migration") if raw_migration else None
            if migration is not None and not isinstance(migration, dict):
                raise SchemaError("SQLite migration must be an object or null")
            versions = dict(
                (name, (version, next_id))
                for name, version, next_id in connection.execute(
                    "SELECT store_name, revision, next_id FROM store_revisions"
                )
            )
            if set(versions) != set(STORE_SPECS):
                raise SchemaError("SQLite store set mismatch")
            stores = {}
            for name, (version, next_id) in versions.items():
                data = read_store(connection, name, next_id)
                build_store_document(name, version, data)
                if version < 1 or version > revision:
                    raise SchemaError(f"invalid SQLite store revision: {name}")
                stores[name] = StoreSnapshot(name, version, data)
            return BackendSnapshot(revision, tx_id, stores, migration)
        except sqlite3.DatabaseError as exc:
            raise SchemaError(f"invalid SQLite database: {exc}") from exc

    def _checkpoint(self, name: str) -> None:
        if self._failpoint is not None:
            self._failpoint(name)


__all__ = ["DATABASE_FILENAME", "SQLiteBackend"]
