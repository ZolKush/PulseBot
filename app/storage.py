"""Application storage API backed by the local SQLite database.

Importing this module performs no filesystem I/O. The launcher calls
``initialize_storage`` only after it owns the process-wide instance lock.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

from .config import DATA_DIR
from .persistence.aggregates import ImportantData, UpdateAborted, UserData
from .persistence.facade import StorageFacade, StorageNotInitializedError
from .persistence.operations import (
    append_audit_entry,
    enqueue_important_outbox,
    enqueue_user_outbox,
    make_outbox_event,
    next_service_request_id,
    suppress_user_outbox_recipient,
)

_FACADE = StorageFacade()


def initialize_storage(data_dir: Path | str = DATA_DIR, *, create: bool = False) -> int:
    """Open SQLite and let its transaction journal recover interrupted writes.

    Production must leave ``create`` false: an absent database is a deployment
    error and must be handled by the explicit migration CLI. Tests may create
    a fresh empty layout in an isolated temporary directory.
    """

    return _FACADE.initialize(data_dir, create=create)


def initialize_empty_storage_for_tests(data_dir: Path | str) -> int:
    return initialize_storage(data_dir, create=True)


def is_storage_initialized() -> bool:
    return _FACADE.is_initialized()


def storage_data_dir() -> Path:
    return _FACADE.data_dir()


def storage_revision() -> int:
    return _FACADE.revision()


update_user_data = _FACADE.update_user_data
update_important_data = _FACADE.update_important_data
get_user_meta_copy = _FACADE.user_meta
authorized_users_snapshot = _FACADE.authorized_users
service_requests_snapshot = _FACADE.service_requests
product_settings_snapshot = _FACADE.product_settings
audit_log_snapshot = _FACADE.audit_log
get_owner_meta_copy = _FACADE.owner_meta
get_user_audit_entries = _FACADE.user_audit_entries
get_active_maintenance = _FACADE.active_maintenance
get_scheduled_maintenance = _FACADE.scheduled_maintenance
get_ticket_copy = _FACADE.ticket
get_all_tickets_snapshot = _FACADE.all_tickets
get_user_open_tickets = _FACADE.user_open_tickets
get_admin_name_by_id = _FACADE.admin_name
outbox_snapshot = _FACADE.outbox
get_outbox_event = _FACADE.outbox_event
mutate_user_meta = _FACADE.mutate_user_meta
mutate_outbox_event = _FACADE.mutate_outbox


async def upsert_user_meta(user_id: int, meta: dict[str, Any]) -> dict[str, Any]:
    def apply(aggregate: UserData) -> dict[str, Any]:
        normalized = UserData._normalize_user(meta)
        normalized["user_id"] = int(user_id)
        aggregate.authorized_users[str(user_id)] = normalized
        return copy.deepcopy(normalized)

    return await update_user_data(apply)


async def remove_user_meta(user_id: int) -> dict[str, Any] | None:
    return await update_user_data(lambda aggregate: aggregate.authorized_users.pop(str(user_id), None))


async def set_maintenance_record(payload: dict[str, Any]) -> dict[str, Any]:
    def apply(aggregate: ImportantData) -> dict[str, Any]:
        aggregate.maintenance = copy.deepcopy(payload)
        return copy.deepcopy(payload)

    return await update_important_data(apply)


async def clear_maintenance_record() -> None:
    await update_important_data(lambda aggregate: setattr(aggregate, "maintenance", {}))


async def set_scheduled_maintenance_record(payload: dict[str, Any]) -> dict[str, Any]:
    def apply(aggregate: ImportantData) -> dict[str, Any]:
        aggregate.scheduled_maintenance = copy.deepcopy(payload)
        return copy.deepcopy(payload)

    return await update_important_data(apply)


async def clear_scheduled_maintenance_record() -> None:
    await update_important_data(lambda aggregate: setattr(aggregate, "scheduled_maintenance", {}))


async def next_ticket_seq() -> int:
    def apply(aggregate: ImportantData) -> int:
        aggregate.tickets_seq += 1
        return aggregate.tickets_seq

    return await update_important_data(apply)


async def set_ticket_record(ticket_id: int, payload: dict[str, Any]) -> dict[str, Any]:
    def apply(aggregate: ImportantData) -> dict[str, Any]:
        aggregate.tickets[str(ticket_id)] = copy.deepcopy(payload)
        return copy.deepcopy(payload)

    return await update_important_data(apply)


__all__ = [
    "ImportantData",
    "StorageNotInitializedError",
    "UpdateAborted",
    "UserData",
    "append_audit_entry",
    "authorized_users_snapshot",
    "enqueue_important_outbox",
    "enqueue_user_outbox",
    "get_active_maintenance",
    "get_admin_name_by_id",
    "get_all_tickets_snapshot",
    "get_owner_meta_copy",
    "get_scheduled_maintenance",
    "get_ticket_copy",
    "get_user_audit_entries",
    "get_user_meta_copy",
    "get_user_open_tickets",
    "initialize_empty_storage_for_tests",
    "initialize_storage",
    "is_storage_initialized",
    "make_outbox_event",
    "mutate_outbox_event",
    "mutate_user_meta",
    "next_service_request_id",
    "outbox_snapshot",
    "product_settings_snapshot",
    "service_requests_snapshot",
    "storage_data_dir",
    "storage_revision",
    "suppress_user_outbox_recipient",
    "update_important_data",
    "update_user_data",
]
