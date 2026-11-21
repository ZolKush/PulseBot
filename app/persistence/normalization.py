"""Normalization rules for application aggregate projections."""

from __future__ import annotations

import copy
from typing import Any

from ..subscriptions.policy import standard_price
from ..users.validation import normalize_email

ACCESS_STATES = {"pending", "approved", "blocked", "logged_out", "rejected"}
SERVICE_TIERS = {"basic", "subscriber", "unlimited_trial"}
ADMIN_LEVELS = {"admin", "owner"}
SERVICE_REQUEST_KINDS = {"trial", "purchase", "renewal"}
SERVICE_REQUEST_STATUSES = {
    "pending",
    "claimed",
    "awaiting_link",
    "requisites_sent",
    "payment_reported",
    "approved",
    "rejected",
    "cancelled",
}
OUTBOX_RECIPIENT_STATUSES = frozenset(
    {"pending", "delivered_pending_registration", "delivered", "terminal", "dead_letter"}
)


def normalize_bool(value: Any, truthy: set[str]) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in truthy
    return bool(value)


def optional_text(value: Any, *, limit: int = 4096) -> str | None:
    if value in (None, ""):
        return None
    text = str(value).strip()
    return text[:limit] if text else None


def optional_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if parsed > 0 else None


def normalize_product_settings(raw: Any = None) -> dict[str, Any]:
    source = dict(raw) if isinstance(raw, dict) else {}
    return {
        "payment_bank": optional_text(source.get("payment_bank"), limit=160),
        "payment_recipient": optional_text(source.get("payment_recipient"), limit=160),
        "payment_phone": optional_text(source.get("payment_phone"), limit=80),
        "payment_message": optional_text(source.get("payment_message"), limit=3800),
        "standard_price_rub": standard_price(source),
        "current_period_end": optional_text(source.get("current_period_end"), limit=80),
        "next_period_end": optional_text(source.get("next_period_end"), limit=80),
        "period_setup_reminder_for": optional_text(source.get("period_setup_reminder_for"), limit=80),
        "period_missing_notice_for": optional_text(source.get("period_missing_notice_for"), limit=80),
        "help_text": optional_text(source.get("help_text"), limit=3500),
        "help_updated_at": optional_text(source.get("help_updated_at"), limit=80),
        "help_updated_by_id": optional_int(source.get("help_updated_by_id")),
        "help_updated_by_name": optional_text(source.get("help_updated_by_name"), limit=160),
        "support_email": normalize_email(source.get("support_email")),
    }


def normalize_review_messages(raw: Any) -> dict[str, list[dict[str, Any]]]:
    """Normalize Telegram message references used for review-card synchronization."""

    if not isinstance(raw, dict):
        return {}
    result: dict[str, list[dict[str, Any]]] = {}
    for raw_admin_id, raw_refs in raw.items():
        try:
            admin_id = int(raw_admin_id)
        except (TypeError, ValueError, OverflowError):
            continue
        if admin_id <= 0:
            continue
        candidates = raw_refs if isinstance(raw_refs, list) else [raw_refs]
        clean_refs: list[dict[str, Any]] = []
        seen: set[tuple[int, int, str | None]] = set()
        for raw_ref in candidates[-20:]:
            if not isinstance(raw_ref, dict):
                continue
            try:
                chat_id = int(raw_ref.get("chat_id", 0) or 0)
                message_id = int(raw_ref.get("message_id", 0) or 0)
            except (TypeError, ValueError, OverflowError):
                continue
            generation = optional_text(raw_ref.get("generation"), limit=100)
            identity = (chat_id, message_id, generation)
            if chat_id == 0 or message_id <= 0 or identity in seen:
                continue
            seen.add(identity)
            clean_refs.append(
                {
                    "chat_id": chat_id,
                    "message_id": message_id,
                    "generation": generation,
                }
            )
        if clean_refs:
            result[str(admin_id)] = clean_refs
    return result


def normalize_service_requests(raw: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(raw, dict):
        return {}
    normalized: dict[str, dict[str, Any]] = {}
    for raw_id, raw_request in raw.items():
        if not isinstance(raw_request, dict):
            continue
        try:
            request_id = int(str(raw_request.get("id", raw_id)))
            user_id = int(raw_request.get("user_id", 0))
        except (TypeError, ValueError, OverflowError):
            continue
        kind = str(raw_request.get("kind") or "")
        status = str(raw_request.get("status") or "pending")
        if request_id <= 0 or user_id <= 0 or kind not in SERVICE_REQUEST_KINDS:
            continue
        if status not in SERVICE_REQUEST_STATUSES:
            status = "pending"
        item = copy.deepcopy(raw_request)
        item.pop("used_app", None)
        item.pop("used_application", None)
        item.update({"id": request_id, "user_id": user_id, "kind": kind, "status": status})
        item["review_messages"] = normalize_review_messages(item.get("review_messages"))
        resume_status = str(item.get("resume_status") or "")
        item["resume_status"] = resume_status if resume_status in {"pending", "payment_reported"} else None
        for key in (
            "created_at",
            "updated_at",
            "comment",
            "claimed_at",
            "reviewed_at",
            "target_end_at",
            "payment_reported_at",
            "decision_reason",
        ):
            item[key] = optional_text(item.get(key), limit=3200 if key == "comment" else 500)
        for key in ("claimed_by_id", "reviewed_by_id"):
            try:
                item[key] = int(item[key]) if item.get(key) not in (None, "") else None
            except (TypeError, ValueError, OverflowError):
                item[key] = None
        normalized[str(request_id)] = item
    return normalized


def normalize_audit_log(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    result: list[dict[str, Any]] = []
    for item in raw[-2000:]:
        if not isinstance(item, dict):
            continue
        action = optional_text(item.get("action"), limit=100)
        timestamp = optional_text(item.get("ts"), limit=80)
        if not action or not timestamp:
            continue
        result.append(
            {
                "ts": timestamp,
                "action": action,
                "actor_id": item.get("actor_id"),
                "actor_public": optional_text(item.get("actor_public"), limit=160),
                "actor_internal": optional_text(item.get("actor_internal"), limit=240),
                "target_user_id": item.get("target_user_id"),
                "details": copy.deepcopy(item.get("details")) if isinstance(item.get("details"), dict) else {},
            }
        )
    return result[-2000:]


def normalize_outbox(raw: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(raw, dict):
        return {}
    normalized: dict[str, dict[str, Any]] = {}
    for raw_id, raw_event in raw.items():
        if not isinstance(raw_event, dict):
            continue
        event_id = str(raw_event.get("id") or raw_id).strip()
        payload = raw_event.get("payload")
        recipients = raw_event.get("recipients")
        if not event_id or not isinstance(payload, dict) or not isinstance(recipients, dict):
            continue
        clean_recipients: dict[str, dict[str, Any]] = {}
        for raw_uid, raw_state in recipients.items():
            try:
                uid = int(raw_uid)
            except (TypeError, ValueError):
                continue
            if uid <= 0:
                continue
            state = dict(raw_state) if isinstance(raw_state, dict) else {}
            status = str(state.get("status") or "pending")
            if status not in OUTBOX_RECIPIENT_STATUSES:
                status = "pending"
            try:
                attempts = max(0, int(state.get("attempts", 0) or 0))
                part_index = max(0, int(state.get("part_index", 0) or 0))
            except (TypeError, ValueError):
                attempts, part_index = 0, 0
            clean_recipients[str(uid)] = {
                "status": status,
                "attempts": attempts,
                "part_index": part_index,
                "next_attempt_at": str(state.get("next_attempt_at") or ""),
                "last_error": str(state.get("last_error") or "")[:500],
                "delivered_at": str(state.get("delivered_at") or ""),
                "delivered_chat_id": optional_int(state.get("delivered_chat_id")),
                "delivered_message_id": optional_int(state.get("delivered_message_id")),
                "dead_lettered_at": str(state.get("dead_lettered_at") or ""),
            }
            if state.get("retry_started_at"):
                clean_recipients[str(uid)]["retry_started_at"] = str(state["retry_started_at"])
        if not clean_recipients:
            continue
        normalized[event_id] = {
            "id": event_id,
            "kind": str(raw_event.get("kind") or "message")[:100],
            "created_at": str(raw_event.get("created_at") or ""),
            "payload": copy.deepcopy(payload),
            "recipients": clean_recipients,
            "completion": copy.deepcopy(raw_event.get("completion"))
            if isinstance(raw_event.get("completion"), dict)
            else {},
            "allow_blocked_delivery": bool(raw_event.get("allow_blocked_delivery", False)),
        }
    return normalized


__all__ = [
    "ACCESS_STATES",
    "ADMIN_LEVELS",
    "OUTBOX_RECIPIENT_STATUSES",
    "SERVICE_REQUEST_KINDS",
    "SERVICE_REQUEST_STATUSES",
    "SERVICE_TIERS",
    "normalize_audit_log",
    "normalize_bool",
    "normalize_outbox",
    "normalize_product_settings",
    "normalize_review_messages",
    "normalize_service_requests",
    "optional_int",
    "optional_text",
]
