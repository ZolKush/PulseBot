"""Maintenance scope, duration, and notification-threshold policy."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from typing import Any

from ..bot.ui import clip_text, html_escape
from ..config import ZABBIX_API_TOKEN, ZABBIX_API_URL
from ..monitoring.zabbix import ZabbixClient, ZabbixError

SERVER_KEY_PATTERN = r"z[1-9][0-9]{0,19}"

MAINT_SCOPE_ALL = "all"
MAX_MAINT_HOURS = 72
MAINT_WARN_THRESHOLDS_MIN = (4320, 720, 30)


def initial_notified_thresholds(remaining_min: int) -> list[int]:
    thresholds = set(MAINT_WARN_THRESHOLDS_MIN)
    armed = {threshold for threshold in thresholds if remaining_min > threshold}
    if not armed and remaining_min > 0:
        armed = {min(thresholds)}
    return sorted(thresholds - armed)


def due_thresholds(notified: list[int], remaining_min: int) -> list[int]:
    sent = set(notified)
    return [
        threshold for threshold in MAINT_WARN_THRESHOLDS_MIN if threshold not in sent and remaining_min <= threshold
    ]


async def server_items() -> dict[str, str]:
    hosts = await ZabbixClient(ZABBIX_API_URL, ZABBIX_API_TOKEN).hosts()
    if not hosts:
        raise ZabbixError("No monitored hosts visible in Zabbix")
    return {f"z{host.id}": host.name for host in hosts}


def normalize_scope(scope: str | None) -> str:
    value = (scope or "").strip().lower()
    return value or MAINT_SCOPE_ALL


def scope_fingerprint(scope: str) -> str:
    if scope == MAINT_SCOPE_ALL:
        return ""
    return hashlib.sha256(f"{ZABBIX_API_URL}/{scope}".encode()).hexdigest()


def scope_is_current(record: Mapping[str, Any]) -> bool:
    scope = normalize_scope(str(record.get("scope") or ""))
    if scope == MAINT_SCOPE_ALL:
        return True
    return bool(re.fullmatch(SERVER_KEY_PATTERN, scope)) and record.get("scope_fingerprint") == scope_fingerprint(scope)


def scope_label(scope: str | None, label: str = "") -> str:
    return "Все серверы" if normalize_scope(scope) == MAINT_SCOPE_ALL else clip_text(label or str(scope), limit=120)


def scope_line(scope: str | None, label: str = "") -> str:
    prefix = "Серверы" if normalize_scope(scope) == MAINT_SCOPE_ALL else "Сервер"
    return f"• {prefix}: <b>{html_escape(scope_label(scope, label))}</b>"


def maint_heading(urgency: object, status: str) -> str:
    if str(urgency or "").lower() == "urgent":
        return f"🚨🚨 <b>СРОЧНЫЕ ТЕХНИЧЕСКИЕ РАБОТЫ</b> 🚨🚨\n⚠️ <b>{html_escape(status)}</b>"
    return f"🗓 <b>ПЛАНОВЫЕ ТЕХНИЧЕСКИЕ РАБОТЫ</b>\nℹ️ <b>{html_escape(status)}</b>"


def hhmm_to_minutes(hours: int, minutes: int) -> int:
    return max(0, (int(hours) * 60) + int(minutes))


def minutes_to_hhmm(total: int) -> tuple[int, int]:
    value = max(0, int(total))
    return value // 60, value % 60
