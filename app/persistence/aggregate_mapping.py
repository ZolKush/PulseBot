"""Loss-detecting mapping between application aggregates and logical repositories."""

from __future__ import annotations

import copy
from typing import Any

from .aggregate_fields import (
    ACCESS_FIELDS,
    BILLING_FIELDS,
    HELP_FIELDS,
    KNOWN_USER_FIELDS,
    OUTBOX_ORIGIN_FIELD,
    OUTBOX_ORIGINS,
    PROFILE_FIELDS,
    SUBSCRIPTION_FIELDS,
)
from .aggregates import ImportantData, UserData
from .errors import SchemaError, StorageConflictError
from .normalization import (
    normalize_audit_log,
    normalize_outbox,
    normalize_product_settings,
    normalize_service_requests,
)
from .unit_of_work import JsonUnitOfWork


def user_data_from_uow(uow: JsonUnitOfWork) -> UserData:
    profiles = uow.profiles.export()
    grants = uow.access.export()
    accounts = uow.subscriptions.export()
    users: dict[str, dict[str, Any]] = {}
    for user_id in sorted(set(profiles) | set(grants) | set(accounts)):
        profile = _object(profiles.get(user_id), f"users.profiles.{user_id}")
        grant = _object(grants.get(user_id), f"access.grants.{user_id}")
        account = _object(accounts.get(user_id), f"subscriptions.accounts.{user_id}")
        meta = {**copy.deepcopy(profile), **copy.deepcopy(grant), **copy.deepcopy(account)}
        try:
            numeric_id = int(user_id)
        except (TypeError, ValueError) as exc:
            raise SchemaError(f"invalid user store key: {user_id!r}") from exc
        declared_id = meta.get("user_id", numeric_id)
        if declared_id != numeric_id:
            raise SchemaError(f"user id mismatch in split stores: key={user_id}, value={declared_id!r}")
        meta["user_id"] = numeric_id
        meta["enabled"] = meta.get("access_state") == "approved"
        users[user_id] = UserData._normalize_user(meta)
    _ensure_single_owner(users)

    requests = uow.service_requests.export()
    settings = {
        **uow.billing_settings.export(),
        **uow.help_and_contacts.export(),
    }
    return UserData(
        authorized_users=users,
        outbox=_outbox_for_origin(uow.outbox.export(), "user"),
        request_seq=requests["next_id"],
        service_requests=copy.deepcopy(requests["items"]),
        product_settings=normalize_product_settings(settings),
        audit_log=normalize_audit_log(uow.audit.export()),
    )


def important_data_from_uow(uow: JsonUnitOfWork) -> ImportantData:
    tickets_store = uow.tickets.export()
    raw_messages = uow.ticket_messages.export()
    tickets: dict[str, Any] = {}
    for ticket_id, raw_ticket in tickets_store["items"].items():
        ticket = copy.deepcopy(_object(raw_ticket, f"support.tickets.{ticket_id}"))
        messages = raw_messages.get(ticket_id, [])
        if not isinstance(messages, list):
            raise SchemaError(f"support.ticket_messages.{ticket_id} must be an array")
        ticket["messages"] = copy.deepcopy(messages)
        tickets[ticket_id] = ticket
    orphan_messages = sorted(set(raw_messages) - set(tickets))
    if orphan_messages:
        raise SchemaError(f"orphan ticket message groups: {orphan_messages}")
    maintenance = uow.maintenance.export()
    return ImportantData(
        tickets_seq=tickets_store["next_id"],
        tickets=tickets,
        maintenance=copy.deepcopy(_object(maintenance.get("active"), "maintenance.state.active")),
        scheduled_maintenance=copy.deepcopy(_object(maintenance.get("scheduled"), "maintenance.state.scheduled")),
        outbox=_outbox_for_origin(uow.outbox.export(), "important"),
    )


def apply_user_data(uow: JsonUnitOfWork, aggregate: UserData) -> None:
    profiles: dict[str, Any] = {}
    grants: dict[str, Any] = {}
    accounts: dict[str, Any] = {}
    for raw_key, raw_meta in aggregate.authorized_users.items():
        if not isinstance(raw_meta, dict):
            raise SchemaError(f"authorized_users.{raw_key} must be an object")
        meta = UserData._normalize_user(raw_meta)
        unknown = sorted(set(meta) - KNOWN_USER_FIELDS)
        if unknown:
            raise SchemaError(f"authorized_users.{raw_key} has fields without a split-store owner: {unknown}")
        try:
            user_id = int(meta.get("user_id", raw_key))
        except (TypeError, ValueError, OverflowError) as exc:
            raise SchemaError(f"authorized_users.{raw_key} has an invalid user_id") from exc
        if user_id <= 0 or str(user_id) != str(raw_key):
            raise SchemaError(f"authorized_users key/id mismatch for {raw_key!r}")
        profiles[str(user_id)] = _select(meta, PROFILE_FIELDS)
        grants[str(user_id)] = _select(meta, ACCESS_FIELDS)
        accounts[str(user_id)] = _select(meta, SUBSCRIPTION_FIELDS)
    _ensure_single_owner(grants)

    requests = normalize_service_requests(aggregate.service_requests)
    if set(requests) != set(aggregate.service_requests):
        raise SchemaError("service request update contains invalid records")
    try:
        request_seq = max(0, int(aggregate.request_seq or 0))
    except (TypeError, ValueError, OverflowError) as exc:
        raise SchemaError("request sequence must be a non-negative integer") from exc
    if requests:
        request_seq = max(request_seq, max(map(int, requests)))
    settings = normalize_product_settings(aggregate.product_settings)
    audit = normalize_audit_log(aggregate.audit_log)
    if len(audit) != min(len(aggregate.audit_log), 2000):
        raise SchemaError("audit update contains invalid records")

    uow.profiles.replace(profiles)
    uow.access.replace(grants)
    uow.subscriptions.replace(accounts)
    uow.service_requests.replace_all(next_id=request_seq, items=requests)
    uow.billing_settings.replace(_select(settings, BILLING_FIELDS))
    uow.help_and_contacts.replace(_select(settings, HELP_FIELDS))
    uow.audit.replace(audit)
    _replace_outbox_origin(uow, "user", aggregate.outbox)


def apply_important_data(uow: JsonUnitOfWork, aggregate: ImportantData) -> None:
    try:
        tickets_seq = max(0, int(aggregate.tickets_seq or 0))
    except (TypeError, ValueError, OverflowError) as exc:
        raise SchemaError("ticket sequence must be a non-negative integer") from exc
    tickets: dict[str, Any] = {}
    messages: dict[str, Any] = {}
    for ticket_id, raw_ticket in aggregate.tickets.items():
        ticket = copy.deepcopy(_object(raw_ticket, f"tickets.{ticket_id}"))
        declared_id = ticket.get("id")
        if isinstance(declared_id, bool) or not isinstance(declared_id, int) or str(declared_id) != str(ticket_id):
            raise SchemaError(f"tickets.{ticket_id} id does not match its key")
        raw_messages = ticket.pop("messages", [])
        if not isinstance(raw_messages, list):
            raise SchemaError(f"tickets.{ticket_id}.messages must be an array")
        tickets[str(ticket_id)] = ticket
        messages[str(ticket_id)] = copy.deepcopy(raw_messages)
        tickets_seq = max(tickets_seq, declared_id)

    uow.tickets.replace_all(next_id=tickets_seq, items=tickets)
    uow.ticket_messages.replace(messages)
    uow.maintenance.replace(
        {
            "active": copy.deepcopy(_object(aggregate.maintenance, "maintenance")),
            "scheduled": copy.deepcopy(_object(aggregate.scheduled_maintenance, "scheduled_maintenance")),
        }
    )
    _replace_outbox_origin(uow, "important", aggregate.outbox)


def _outbox_for_origin(raw: dict[str, Any], origin: str) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for event_id, raw_event in raw.items():
        event = copy.deepcopy(_object(raw_event, f"messaging.outbox.{event_id}"))
        event_origin = event.pop(OUTBOX_ORIGIN_FIELD, None)
        if event_origin not in OUTBOX_ORIGINS:
            raise SchemaError(f"messaging.outbox.{event_id} has no valid origin")
        if event_origin == origin:
            result[event_id] = event
    return normalize_outbox(result)


def _replace_outbox_origin(uow: JsonUnitOfWork, origin: str, raw: Any) -> None:
    events = normalize_outbox(raw)
    if not isinstance(raw, dict) or set(events) != set(raw):
        raise SchemaError(f"{origin} outbox update contains invalid events")
    current = uow.outbox.export()
    retained: dict[str, Any] = {}
    for event_id, raw_event in current.items():
        event = _object(raw_event, f"messaging.outbox.{event_id}")
        event_origin = event.get(OUTBOX_ORIGIN_FIELD)
        if event_origin not in OUTBOX_ORIGINS:
            raise SchemaError(f"messaging.outbox.{event_id} has no valid origin")
        if event_origin != origin:
            retained[event_id] = copy.deepcopy(event)
    collision = sorted(set(retained) & set(events))
    if collision:
        raise StorageConflictError(f"outbox event id collision between origins: {collision}")
    for event_id, event in events.items():
        tagged = copy.deepcopy(event)
        tagged[OUTBOX_ORIGIN_FIELD] = origin
        retained[event_id] = tagged
    uow.outbox.replace(retained)


def _select(source: dict[str, Any], fields: frozenset[str]) -> dict[str, Any]:
    return {key: copy.deepcopy(source[key]) for key in sorted(fields) if key in source}


def _object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SchemaError(f"{label} must be an object")
    return value


def _ensure_single_owner(users: dict[str, Any]) -> None:
    owners = sum(
        1
        for meta in users.values()
        if isinstance(meta, dict) and meta.get("role") == "admin" and meta.get("admin_level") == "owner"
    )
    if owners > 1:
        raise SchemaError("user state contains multiple service owners")


__all__ = [
    "apply_important_data",
    "apply_user_data",
    "important_data_from_uow",
    "user_data_from_uow",
]
