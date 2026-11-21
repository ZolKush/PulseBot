"""Conversation states and transient context for maintenance flows."""

from __future__ import annotations

from typing import Any

from telegram.ext import ContextTypes

(
    STATE_MAINT_MODE,
    STATE_MAINT_SCOPE,
    STATE_MAINT_URGENCY,
    STATE_MAINT_DURATION,
    STATE_MAINT_EXTEND,
    STATE_MAINT_SCHEDULE_RANGE,
    STATE_MAINT_SCHEDULE_DATE,
) = range(7)

MAINTENANCE_CONTEXT_KEYS = (
    "maint_mode",
    "maint_scope",
    "maint_scope_label",
    "maint_scope_fingerprint",
    "maint_urgency",
    "maint_panel_chat_id",
    "maint_panel_msg_id",
    "maint_extend_id",
    "maint_sched_date",
    "maint_base_scheduled_id",
)


def maintenance_context(context: ContextTypes.DEFAULT_TYPE) -> dict[Any, Any]:
    data = context.user_data
    if data is None:
        raise RuntimeError("Maintenance flows require per-user context data")
    return data


def clear_maintenance_context(context: ContextTypes.DEFAULT_TYPE) -> None:
    data = maintenance_context(context)
    for key in MAINTENANCE_CONTEXT_KEYS:
        data.pop(key, None)


__all__ = [
    "MAINTENANCE_CONTEXT_KEYS",
    "STATE_MAINT_DURATION",
    "STATE_MAINT_EXTEND",
    "STATE_MAINT_MODE",
    "STATE_MAINT_SCHEDULE_DATE",
    "STATE_MAINT_SCHEDULE_RANGE",
    "STATE_MAINT_SCOPE",
    "STATE_MAINT_URGENCY",
    "clear_maintenance_context",
    "maintenance_context",
]
