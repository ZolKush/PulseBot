"""Stateful application façade backed by transactional SQLite repositories."""

from __future__ import annotations

import asyncio
import copy
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar

from ..runtime.logging import logger
from ..users.staff import is_owner_meta, staff_public_signature
from .aggregate_mapping import (
    apply_important_data,
    apply_user_data,
    important_data_from_uow,
    user_data_from_uow,
)
from .aggregates import ImportantData, UpdateAborted, UserData
from .errors import PersistenceError
from .normalization import normalize_outbox
from .sqlite_backend import SQLiteBackend

T = TypeVar("T")


class StorageNotInitializedError(PersistenceError):
    """Storage was used before explicit post-lock initialization."""


class StorageFacade:
    def __init__(self) -> None:
        self._backend: SQLiteBackend | None = None
        self._data_dir: Path | None = None
        self._user = UserData()
        self._important = ImportantData()
        self._revision = 0
        self._update_lock: asyncio.Lock | None = None
        self._publish_lock = threading.RLock()

    def initialize(self, data_dir: Path | str, *, create: bool = False) -> int:
        backend = SQLiteBackend(data_dir)
        if create and not backend.exists():
            from .bootstrap import initialize_empty_layout

            initialize_empty_layout(data_dir)
        with backend.unit_of_work() as uow:
            user = user_data_from_uow(uow)
            important = important_data_from_uow(uow)
            revision = uow.current_snapshot.revision
        with self._publish_lock:
            self._backend = backend
            self._data_dir = backend.data_root
            self._user = user
            self._important = important
            self._revision = revision
            self._update_lock = None
        return revision

    def is_initialized(self) -> bool:
        return self._backend is not None

    def data_dir(self) -> Path:
        self._require_backend()
        data_dir = self._data_dir
        if data_dir is None:
            raise StorageNotInitializedError("storage data directory is unavailable")
        return data_dir

    def revision(self) -> int:
        self._require_backend()
        return self._revision

    async def update_user_data(self, update_fn: Callable[[UserData], T]) -> T:
        backend = self._require_backend()
        async with self._lock():
            try:
                async with backend.unit_of_work() as uow:
                    aggregate = user_data_from_uow(uow)
                    result = update_fn(aggregate)
                    apply_user_data(uow, aggregate)
                    try:
                        await uow.commit_async()
                    except asyncio.CancelledError:
                        self._publish_from_uow(uow)
                        raise
                    self._publish_from_uow(uow)
                    return result
            except UpdateAborted:
                raise
            except Exception:
                logger.exception("Не удалось обновить SQLite user state")
                raise

    async def update_important_data(self, update_fn: Callable[[ImportantData], T]) -> T:
        backend = self._require_backend()
        async with self._lock():
            try:
                async with backend.unit_of_work() as uow:
                    aggregate = important_data_from_uow(uow)
                    result = update_fn(aggregate)
                    apply_important_data(uow, aggregate)
                    try:
                        await uow.commit_async()
                    except asyncio.CancelledError:
                        self._publish_from_uow(uow)
                        raise
                    self._publish_from_uow(uow)
                    return result
            except UpdateAborted:
                raise
            except Exception:
                logger.exception("Не удалось обновить SQLite important state")
                raise

    def user_meta(self, user_id: int) -> dict[str, Any] | None:
        with self._publish_lock:
            meta = self._user.authorized_users.get(str(user_id))
            return copy.deepcopy(meta) if isinstance(meta, dict) else None

    def authorized_users(self) -> dict[str, dict[str, Any]]:
        with self._publish_lock:
            return copy.deepcopy(self._user.authorized_users)

    def service_requests(self) -> dict[str, dict[str, Any]]:
        with self._publish_lock:
            return copy.deepcopy(self._user.service_requests)

    def product_settings(self) -> dict[str, Any]:
        with self._publish_lock:
            return copy.deepcopy(self._user.product_settings)

    def audit_log(self) -> list[dict[str, Any]]:
        with self._publish_lock:
            return copy.deepcopy(self._user.audit_log)

    def important_snapshot(self) -> ImportantData:
        with self._publish_lock:
            return copy.deepcopy(self._important)

    def owner_meta(self) -> dict[str, Any] | None:
        for meta in self.authorized_users().values():
            if is_owner_meta(meta):
                return meta
        return None

    def user_audit_entries(self, user_id: int, *, limit: int = 5) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for item in reversed(self.audit_log()):
            try:
                target = int(item.get("target_user_id", 0) or 0)
            except (TypeError, ValueError):
                continue
            if target == int(user_id):
                result.append(item)
                if len(result) >= max(1, int(limit)):
                    break
        return result

    def active_maintenance(self) -> dict[str, Any] | None:
        item = self.important_snapshot().maintenance
        return item if item.get("active") else None

    def scheduled_maintenance(self) -> dict[str, Any] | None:
        item = self.important_snapshot().scheduled_maintenance
        return item if item.get("id") else None

    def ticket(self, ticket_id: int) -> dict[str, Any] | None:
        with self._publish_lock:
            item = self._important.tickets.get(str(ticket_id))
            return copy.deepcopy(item) if isinstance(item, dict) else None

    def all_tickets(self) -> dict[str, dict[str, Any]]:
        return {
            key: copy.deepcopy(value)
            for key, value in self.important_snapshot().tickets.items()
            if isinstance(value, dict)
        }

    def user_open_tickets(self, user_id: int) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for ticket in self.all_tickets().values():
            try:
                owner_id = int(ticket.get("user_id", 0) or 0)
            except (TypeError, ValueError):
                continue
            if owner_id == user_id and str(ticket.get("status", "open")) != "closed":
                result.append(ticket)
        return result

    def admin_name(self, admin_id: int) -> str | None:
        meta = self.user_meta(admin_id)
        if (
            not isinstance(meta, dict)
            or meta.get("role") != "admin"
            or meta.get("access_state") != "approved"
            or not bool(meta.get("enabled"))
        ):
            return None
        return staff_public_signature(meta)

    def outbox_event(self, source: str, event_id: str) -> dict[str, Any] | None:
        if source not in {"user", "important"}:
            raise ValueError(f"unknown outbox source: {source}")
        with self._publish_lock:
            aggregate = self._user if source == "user" else self._important
            return copy.deepcopy(aggregate.outbox.get(event_id))

    def outbox(self) -> list[tuple[str, dict[str, Any]]]:
        with self._publish_lock:
            events = [
                *[("user", copy.deepcopy(event)) for event in self._user.outbox.values()],
                *[("important", copy.deepcopy(event)) for event in self._important.outbox.values()],
            ]
        events.sort(key=lambda item: str(item[1].get("created_at") or ""))
        return events

    async def mutate_user_meta(
        self,
        user_id: int,
        mutate_fn: Callable[[dict[str, Any]], dict[str, Any]],
    ) -> dict[str, Any] | None:
        def apply(aggregate: UserData) -> dict[str, Any]:
            current = aggregate.authorized_users.get(str(user_id))
            if not isinstance(current, dict):
                raise UpdateAborted()
            updated = UserData._normalize_user(mutate_fn(copy.deepcopy(current)))
            updated["user_id"] = int(user_id)
            aggregate.authorized_users[str(user_id)] = updated
            return copy.deepcopy(updated)

        try:
            return await self.update_user_data(apply)
        except UpdateAborted:
            return None

    async def mutate_outbox(
        self,
        source: str,
        event_id: str,
        mutate_fn: Callable[[dict[str, Any]], dict[str, Any] | None],
    ) -> dict[str, Any] | None:
        if source not in {"user", "important"}:
            raise ValueError(f"unknown outbox source: {source}")

        def apply(aggregate: UserData | ImportantData) -> dict[str, Any] | None:
            current = aggregate.outbox.get(event_id)
            if not isinstance(current, dict):
                raise UpdateAborted()
            updated = mutate_fn(copy.deepcopy(current))
            if updated is None:
                aggregate.outbox.pop(event_id, None)
                return None
            clean = normalize_outbox({event_id: updated}).get(event_id)
            if not clean:
                raise ValueError("outbox mutation produced an invalid event")
            aggregate.outbox[event_id] = clean
            return copy.deepcopy(clean)

        try:
            if source == "user":
                return await self.update_user_data(apply)
            return await self.update_important_data(apply)
        except UpdateAborted:
            return None

    def _require_backend(self) -> SQLiteBackend:
        if self._backend is None:
            raise StorageNotInitializedError(
                "SQLite storage is not initialized; call initialize_storage after acquiring the process lock"
            )
        return self._backend

    def _lock(self) -> asyncio.Lock:
        if self._update_lock is None:
            self._update_lock = asyncio.Lock()
        return self._update_lock

    def _publish_from_uow(self, uow) -> None:
        user = user_data_from_uow(uow)
        important = important_data_from_uow(uow)
        with self._publish_lock:
            self._user = user
            self._important = important
            self._revision = uow.current_snapshot.revision


__all__ = ["StorageFacade", "StorageNotInitializedError"]
