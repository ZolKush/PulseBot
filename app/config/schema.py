from __future__ import annotations

import ipaddress
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import ValidationError, ValidationInfo, field_validator, model_validator

from .json_files import JsonConfigError, load_json_object
from .schema_fields import SettingsFields
from .schema_rules import validate_settings_consistency
from .validators import is_uuid


class AppSettings(SettingsFields):
    @field_validator("DNS_RESOLVERS", "REMNAWAVE_HIDDEN_UUIDS", mode="before")
    @classmethod
    def _parse_list(cls, value: Any, info: ValidationInfo) -> list[str]:
        if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
            raise ValueError(f"{info.field_name} must be a JSON array of strings")
        return value

    @field_validator("REMNAWAVE_HIDDEN_UUIDS", mode="after")
    @classmethod
    def _validate_uuids(cls, value: list[str]) -> list[str]:
        unique = list(dict.fromkeys(value))
        for item in unique:
            if not is_uuid(item):
                raise ValueError("REMNAWAVE_HIDDEN_UUIDS: invalid UUID")
        return unique

    @field_validator("DNS_RESOLVERS", mode="after")
    @classmethod
    def _validate_dns_resolvers(cls, value: list[str]) -> list[str]:
        resolvers: list[str] = []
        for item in value:
            try:
                resolver = str(ipaddress.ip_address(item))
            except ValueError as exc:
                raise ValueError("DNS_RESOLVERS contains an invalid IP address") from exc
            if resolver not in resolvers:
                resolvers.append(resolver)
        if not resolvers:
            raise ValueError("DNS_RESOLVERS must contain at least one IP address")
        return resolvers

    @field_validator("TZ")
    @classmethod
    def _validate_tz(cls, value: str) -> str:
        timezone_name = (value or "").strip() or "UTC"
        try:
            ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("unknown timezone") from exc
        return timezone_name

    @field_validator("LOG_LEVEL", mode="before")
    @classmethod
    def _normalize_log_level(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("LOG_LEVEL must be a string")
        normalized = value.strip().upper() or "INFO"
        normalized = {"WARN": "WARNING", "FATAL": "CRITICAL"}.get(normalized, normalized)
        allowed = {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG", "NOTSET"}
        if normalized not in allowed:
            raise ValueError("LOG_LEVEL must be one of CRITICAL, ERROR, WARNING, INFO, DEBUG, NOTSET")
        return normalized

    @field_validator("FAIL2BAN_DAILY_AT", "DNS_DAILY_REFRESH_AT", "DAILY_NODE_STATUS_REFRESH_AT")
    @classmethod
    def _validate_hhmm(cls, value: str, info: ValidationInfo) -> str:
        normalized = (value or "").strip()
        try:
            hours, minutes = normalized.split(":", 1)
            hour, minute = int(hours), int(minutes)
            if not (0 <= hour <= 23 and 0 <= minute <= 59):
                raise ValueError
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{info.field_name} must be HH:MM") from exc
        return f"{hour:02d}:{minute:02d}"

    @field_validator(
        "SUBPROC_SHORT_TIMEOUT",
        "SUBPROC_MEDIUM_TIMEOUT",
        "DNS_STARTUP_REFRESH_DELAY_SEC",
        "MAINT_RESTART_NOTIFY_DELAY_SEC",
    )
    @classmethod
    def _positive_small_int(cls, value: int, info: ValidationInfo) -> int:
        normalized = int(value)
        if not 1 <= normalized <= 3600:
            raise ValueError(f"{info.field_name} must be in range 1..3600")
        return normalized

    @field_validator(
        "AUTH_FAIL_WINDOW_SEC",
        "AUTH_MAX_FAILS_IN_WINDOW",
        "AUTH_GLOBAL_MAX_FAILS_IN_WINDOW",
        "AUTH_LOCKOUT_SEC",
        "AUTH_PRUNE_INTERVAL_SEC",
        "ACCESS_REQUEST_COOLDOWN_SEC",
        "ERROR_NOTIFY_INTERVAL_SEC",
        "OUTBOX_PROCESS_INTERVAL_SEC",
        "MAINT_RESTART_REMINDER_INTERVAL_SEC",
        "STATUS_CACHE_TTL_SEC",
    )
    @classmethod
    def _positive_int(cls, value: int, info: ValidationInfo) -> int:
        normalized = int(value)
        if normalized < 1:
            raise ValueError(f"{info.field_name} must be >= 1")
        return normalized

    @field_validator("NAVIGATION_RETENTION_HOURS")
    @classmethod
    def _message_retention_hours(cls, value: int) -> int:
        normalized = int(value)
        if not 1 <= normalized <= 36:
            raise ValueError("NAVIGATION_RETENTION_HOURS must be in range 1..36")
        return normalized

    @field_validator("NAVIGATION_CLEANUP_INTERVAL_SEC")
    @classmethod
    def _message_cleanup_interval(cls, value: int) -> int:
        normalized = int(value)
        if not 60 <= normalized <= 3600:
            raise ValueError("NAVIGATION_CLEANUP_INTERVAL_SEC must be in range 60..3600")
        return normalized

    @field_validator(
        "DATA_DIR",
        "INSTANCE_LOCK_PATH",
        "PTB_PERSISTENCE_PATH",
        mode="before",
    )
    @classmethod
    def _normalize_path(cls, value: Any, info: ValidationInfo) -> str:
        if not isinstance(value, str):
            raise ValueError(f"{info.field_name} must be a string")
        normalized = value.strip()
        if "\x00" in normalized:
            raise ValueError(f"{info.field_name} must not contain NUL")
        if info.field_name == "DATA_DIR" and not normalized:
            raise ValueError("DATA_DIR must not be empty")
        return normalized

    @field_validator("REMNAWAVE_METRICS_URL", mode="before")
    @classmethod
    def _validate_metrics_url(cls, value: Any) -> str:
        if not isinstance(value, str):
            raise ValueError("REMNAWAVE_METRICS_URL must be a string")
        normalized = value.strip()
        if not normalized:
            return ""
        if any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in normalized):
            raise ValueError("REMNAWAVE_METRICS_URL must not contain whitespace or control characters")
        try:
            parsed = urlsplit(normalized)
            _ = parsed.port
        except ValueError:
            raise ValueError("REMNAWAVE_METRICS_URL has an invalid host or port") from None
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("REMNAWAVE_METRICS_URL must be an absolute HTTP(S) URL")
        if parsed.username or parsed.password or parsed.fragment:
            raise ValueError("REMNAWAVE_METRICS_URL must not contain credentials or a fragment")
        return normalized

    @field_validator("REMNAWAVE_METRICS_TIMEOUT_SEC", "REMNAWAVE_METRICS_CACHE_TTL_SEC")
    @classmethod
    def _positive_metrics_int(cls, value: int) -> int:
        normalized = int(value)
        if not 1 <= normalized <= 600:
            raise ValueError("metrics timeout/ttl out of range (1..600)")
        return normalized

    @field_validator("REMNAWAVE_METRICS_MAX_BYTES", "SUBPROC_MAX_OUTPUT_BYTES")
    @classmethod
    def _max_bytes(cls, value: int, info: ValidationInfo) -> int:
        normalized = int(value)
        if not 1024 <= normalized <= 20_000_000:
            raise ValueError(f"{info.field_name} must be in range 1024..20000000")
        return normalized

    @field_validator("FAIL2BAN_DIGEST_TAIL_LINES")
    @classmethod
    def _fail2ban_tail_lines(cls, value: int) -> int:
        normalized = int(value)
        if not 1 <= normalized <= 50_000:
            raise ValueError("FAIL2BAN_DIGEST_TAIL_LINES must be in range 1..50000")
        return normalized

    @field_validator("FAIL2BAN_DIGEST_MAX_BYTES")
    @classmethod
    def _fail2ban_max_bytes(cls, value: int) -> int:
        normalized = int(value)
        if not 1024 <= normalized <= 3_000_000:
            raise ValueError("FAIL2BAN_DIGEST_MAX_BYTES must be in range 1024..3000000")
        return normalized

    @model_validator(mode="after")
    def _validate_consistency(self) -> AppSettings:
        validate_settings_consistency(self)
        return self


class BotConfigDocument(AppSettings):
    version: Literal[1]


def load_app_settings(path: str | Path) -> AppSettings:
    raw = load_json_object(path, field_name="BOT_CONFIG_FILE")
    expected = {"version", *AppSettings.model_fields}
    missing = sorted(expected - set(raw))
    if missing:
        raise JsonConfigError(f"invalid BOT_CONFIG_FILE {Path(path)}: missing keys: {', '.join(missing)}")
    try:
        document = BotConfigDocument.model_validate(raw)
    except ValidationError as exc:
        raise JsonConfigError(f"invalid BOT_CONFIG_FILE {Path(path)}: {exc}") from None
    return AppSettings.model_validate(document.model_dump(exclude={"version"}))


__all__ = ["AppSettings", "BotConfigDocument", "load_app_settings"]
