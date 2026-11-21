"""Factories and transitions for maintenance records."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4

from ..config import TZ
from .models import Maintenance, ScheduledMaintenance
from .policy import MAINT_SCOPE_ALL, hhmm_to_minutes, initial_notified_thresholds, normalize_scope, scope_fingerprint


def build_maintenance_record(
    scope: str,
    urgency: str,
    hours: int,
    minutes: int,
    author_id: int | None,
    author_name: str,
    *,
    label: str = "",
) -> dict[str, Any]:
    now = datetime.now(TZ)
    duration_min = hhmm_to_minutes(hours, minutes)
    expected_end = now + timedelta(minutes=duration_min)
    return {
        "id": uuid4().hex,
        "active": True,
        "scope": normalize_scope(scope),
        "scope_label": label,
        "scope_fingerprint": scope_fingerprint(scope),
        "urgency": urgency,
        "duration_min": duration_min,
        "started_at": now.isoformat(),
        "expected_end": expected_end.isoformat(),
        "author_id": author_id,
        "author_name": author_name,
        "author_signature_version": 1,
        "updated_at": now.isoformat(),
    }


def build_scheduled_maintenance_record(
    scope: str,
    start_at: datetime,
    end_at: datetime,
    author_id: int | None,
    author_name: str,
    *,
    label: str = "",
) -> ScheduledMaintenance:
    duration_min = max(1, int((end_at - start_at).total_seconds() // 60))
    now = datetime.now(TZ)
    remaining_min = int((start_at - now).total_seconds() // 60)
    return ScheduledMaintenance(
        id=uuid4().hex,
        scope=normalize_scope(scope),
        scope_label=label,
        scope_fingerprint=scope_fingerprint(scope),
        urgency="planned",
        duration_min=duration_min,
        scheduled_start=start_at.isoformat(),
        scheduled_end=end_at.isoformat(),
        author_id=author_id,
        author_name=author_name,
        author_signature_version=1,
        created_at=now.isoformat(),
        updated_at=now.isoformat(),
        notified_thresholds=initial_notified_thresholds(remaining_min),
        announced_thresholds=[],
        notified_start=False,
    )


def scheduled_to_active_record(scheduled: ScheduledMaintenance) -> Maintenance:
    start_at = datetime.now(TZ)
    duration_min = int(scheduled.get("duration_min", 0) or 0)
    try:
        expected_end = datetime.fromisoformat(str(scheduled.get("scheduled_end") or ""))
        if expected_end.tzinfo is None:
            expected_end = expected_end.replace(tzinfo=TZ)
    except (TypeError, ValueError):
        expected_end = start_at + timedelta(minutes=max(duration_min, 1))
    return Maintenance(
        id=str(scheduled.get("id") or uuid4().hex),
        active=True,
        scope=normalize_scope(str(scheduled.get("scope") or MAINT_SCOPE_ALL)),
        scope_label=str(scheduled.get("scope_label") or ""),
        scope_fingerprint=str(scheduled.get("scope_fingerprint") or ""),
        urgency="planned",
        duration_min=max(duration_min, 1),
        started_at=start_at.isoformat(),
        expected_end=expected_end.isoformat(),
        author_id=scheduled.get("author_id"),
        author_name=(
            str(scheduled.get("author_name") or "Техническая поддержка")
            if scheduled.get("author_signature_version") == 1
            else "Техническая поддержка"
        ),
        author_signature_version=1,
        updated_at=start_at.isoformat(),
    )


_build_maint_record = build_maintenance_record
_build_scheduled_maint_record = build_scheduled_maintenance_record
_scheduled_to_active_record = scheduled_to_active_record
