"""Typed active and scheduled maintenance records."""

from __future__ import annotations

from typing import Any, Literal, TypedDict

MaintScope = str
MaintUrgency = Literal["urgent", "planned"]


class Maintenance(TypedDict, total=False):
    id: str
    active: bool
    scope: MaintScope
    scope_label: str
    scope_fingerprint: str
    urgency: MaintUrgency
    duration_min: int
    started_at: str
    expected_end: str
    overdue_reminded_for: str
    author_id: int | None
    author_name: str
    author_signature_version: int
    updated_at: str


class ScheduledMaintenance(TypedDict, total=False):
    id: str
    scope: MaintScope
    scope_label: str
    scope_fingerprint: str
    urgency: MaintUrgency
    duration_min: int
    scheduled_start: str
    scheduled_end: str
    author_id: int | None
    author_name: str
    author_signature_version: int
    created_at: str
    updated_at: str
    notified_thresholds: list[int]
    announced_thresholds: list[int]
    notified_start: bool


class MaintenanceKeys:
    ID = "id"
    ACTIVE = "active"
    SCOPE = "scope"
    URGENCY = "urgency"
    DURATION_MIN = "duration_min"
    STARTED_AT = "started_at"
    EXPECTED_END = "expected_end"
    AUTHOR_ID = "author_id"
    AUTHOR_NAME = "author_name"
    AUTHOR_SIGNATURE_VERSION = "author_signature_version"
    UPDATED_AT = "updated_at"


class ScheduledMaintenanceKeys:
    ID = "id"
    SCOPE = "scope"
    URGENCY = "urgency"
    DURATION_MIN = "duration_min"
    SCHEDULED_START = "scheduled_start"
    SCHEDULED_END = "scheduled_end"
    AUTHOR_ID = "author_id"
    AUTHOR_NAME = "author_name"
    AUTHOR_SIGNATURE_VERSION = "author_signature_version"
    CREATED_AT = "created_at"
    UPDATED_AT = "updated_at"
    NOTIFIED_THRESHOLDS = "notified_thresholds"
    ANNOUNCED_THRESHOLDS = "announced_thresholds"
    NOTIFIED_START = "notified_start"


def coerce_maintenance(raw: dict[str, Any] | None) -> Maintenance:
    if not isinstance(raw, dict):
        return Maintenance()
    return raw  # type: ignore[return-value]


def coerce_scheduled_maintenance(raw: dict[str, Any] | None) -> ScheduledMaintenance:
    if not isinstance(raw, dict):
        return ScheduledMaintenance()
    return raw  # type: ignore[return-value]
