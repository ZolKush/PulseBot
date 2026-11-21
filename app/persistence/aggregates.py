"""Aggregate application-state projections assembled from domain stores."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..users.staff import (
    STAFF_DISPLAY_TITLE,
    normalize_staff_alias,
    normalize_staff_display_mode,
    normalize_staff_title,
)
from ..users.validation import normalize_email
from .normalization import (
    ACCESS_STATES,
    ADMIN_LEVELS,
    SERVICE_TIERS,
    normalize_audit_log,
    normalize_bool,
    normalize_outbox,
    normalize_product_settings,
    normalize_review_messages,
    normalize_service_requests,
    optional_int,
    optional_text,
)


class UpdateAborted(Exception):
    """Abort an aggregate update without committing or logging an error."""


@dataclass
class UserData:
    authorized_users: dict[str, dict[str, Any]] = field(default_factory=dict)
    outbox: dict[str, dict[str, Any]] = field(default_factory=dict)
    request_seq: int = 0
    service_requests: dict[str, dict[str, Any]] = field(default_factory=dict)
    product_settings: dict[str, Any] = field(default_factory=normalize_product_settings)
    audit_log: list[dict[str, Any]] = field(default_factory=list)

    @staticmethod
    def _normalize_user(meta: dict[str, Any]) -> dict[str, Any]:
        meta = dict(meta) if isinstance(meta, dict) else {}
        meta.pop("used_app", None)
        meta.pop("used_application", None)
        try:
            uid = int(meta["user_id"]) if meta.get("user_id") is not None else None
        except (TypeError, ValueError, OverflowError):
            uid = None
        role = meta.get("role", "user")
        if role not in ("user", "admin"):
            role = "user"
        enabled = normalize_bool(meta.get("enabled", True), {"1", "true", "yes", "y", "on", "enabled"})
        state = str(meta.get("access_state") or "").strip().lower()
        if state not in ACCESS_STATES:
            state = "approved" if role == "admin" or enabled else "blocked"
        meta.update({"user_id": uid, "role": role, "access_state": state, "enabled": state == "approved"})
        for key, raw_value, limit in (
            ("nickname", meta.get("nickname") or meta.get("nick"), 160),
            ("username", meta.get("username"), 64),
            ("first_name", meta.get("first_name"), 256),
            ("last_name", meta.get("last_name"), 256),
            ("auth_at", meta.get("auth_at"), 80),
        ):
            meta[key] = optional_text(raw_value, limit=limit)
        meta["contact_email"] = normalize_email(meta.get("contact_email"))
        meta.pop("nick", None)
        for key, limit in (
            ("access_requested_at", 80),
            ("access_reviewed_at", 80),
            ("access_reviewed_by_name", 160),
            ("blocked_at", 80),
            ("blocked_by_name", 160),
            ("blocked_reason", 500),
            ("logged_out_at", 80),
        ):
            meta[key] = optional_text(meta.get(key), limit=limit)
        for key in ("access_reviewed_by_id", "blocked_by_id"):
            meta[key] = optional_int(meta.get(key))
        meta["review_messages"] = normalize_review_messages(meta.get("review_messages"))

        is_admin = role == "admin"
        admin_level = str(meta.get("admin_level") or "admin") if is_admin else "none"
        if admin_level not in ADMIN_LEVELS:
            admin_level = "admin" if is_admin else "none"
        meta["admin_level"] = admin_level
        meta["staff_title"] = (
            normalize_staff_title(meta.get("staff_title"), owner=admin_level == "owner") if is_admin else None
        )
        meta["staff_alias"] = normalize_staff_alias(meta.get("staff_alias")) if is_admin else None
        display_mode = normalize_staff_display_mode(meta.get("staff_display_mode")) if is_admin else STAFF_DISPLAY_TITLE
        meta["staff_display_mode"] = display_mode if meta["staff_alias"] else STAFF_DISPLAY_TITLE

        tier = str(meta.get("service_tier") or ("subscriber" if is_admin else "basic"))
        if tier not in SERVICE_TIERS:
            tier = "basic"
        meta["service_tier"] = tier
        meta["is_paid"] = normalize_bool(meta.get("is_paid", False), {"1", "true", "yes", "y", "on", "paid"})
        if tier != "subscriber":
            meta["is_paid"] = False
        connection = meta.get("connection_url")
        if connection in (None, ""):
            connection = meta.get("subscription_text")
        meta["connection_url"] = optional_text(connection, limit=1_000_000)
        meta.pop("subscription_text", None)

        text_fields = {
            "subscription_updated_at": 80,
            "subscription_updated_by_name": 160,
            "paid_at": 80,
            "payment_confirmed_by_name": 160,
            "subscription_end_at": 80,
            "trial_issued_at": 80,
            "trial_issued_by_name": 160,
            "trial_end_at": 80,
            "last_auto_payment_reminder_at": 80,
            "last_auto_payment_reminder_type": 40,
            "last_manual_payment_reminder_at": 80,
            "last_manual_payment_reminder_by_name": 160,
            "service_tier_updated_at": 80,
            "service_tier_updated_by_name": 160,
        }
        for key, limit in text_fields.items():
            meta[key] = optional_text(meta.get(key), limit=limit)
        for key in (
            "subscription_updated_by_id",
            "payment_confirmed_by_id",
            "trial_issued_by_id",
            "last_manual_payment_reminder_by_id",
            "service_tier_updated_by_id",
        ):
            meta[key] = optional_int(meta.get(key))
        duration = optional_int(meta.get("trial_duration_hours"))
        meta["trial_duration_hours"] = min(duration, 8760) if duration is not None else None
        if tier == "unlimited_trial":
            meta["subscription_end_at"] = None
        reminders = meta.get("payment_auto_reminders")
        meta["payment_auto_reminders"] = (
            {
                str(key)[:180]: str(value)[:80]
                for key, value in list(reminders.items())[-200:]
                if str(key).strip() and str(value).strip()
            }
            if isinstance(reminders, dict)
            else {}
        )
        if is_admin and admin_level == "owner":
            meta.update(
                {
                    "service_tier": "subscriber",
                    "is_paid": True,
                    "subscription_end_at": None,
                    "payment_auto_reminders": {},
                }
            )
        return meta

    def normalize(self) -> None:
        self.authorized_users = {
            str(uid): self._normalize_user(meta)
            for uid, meta in self.authorized_users.items()
            if isinstance(meta, dict)
        }
        self.outbox = normalize_outbox(self.outbox)
        self.request_seq = max(0, int(self.request_seq or 0))
        self.service_requests = normalize_service_requests(self.service_requests)
        if self.service_requests:
            self.request_seq = max(self.request_seq, max(map(int, self.service_requests)))
        self.product_settings = normalize_product_settings(self.product_settings)
        self.audit_log = normalize_audit_log(self.audit_log)


@dataclass
class ImportantData:
    tickets_seq: int = 0
    tickets: dict[str, Any] = field(default_factory=dict)
    maintenance: dict[str, Any] = field(default_factory=dict)
    scheduled_maintenance: dict[str, Any] = field(default_factory=dict)
    outbox: dict[str, dict[str, Any]] = field(default_factory=dict)

    def normalize(self) -> None:
        self.tickets_seq = max(0, int(self.tickets_seq or 0))
        self.tickets = copy_mapping(self.tickets)
        self.maintenance = copy_mapping(self.maintenance)
        self.scheduled_maintenance = copy_mapping(self.scheduled_maintenance)
        self.outbox = normalize_outbox(self.outbox)


def copy_mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


__all__ = ["ImportantData", "UpdateAborted", "UserData"]
