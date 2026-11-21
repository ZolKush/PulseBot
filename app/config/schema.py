from __future__ import annotations

from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import ValidationError, ValidationInfo, field_validator

from .json_files import JsonConfigError, load_json_object
from .schema_fields import SettingsFields


class AppSettings(SettingsFields):
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

    @field_validator("MAINT_RESTART_NOTIFY_DELAY_SEC")
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

    @field_validator("ZABBIX_API_URL")
    @classmethod
    def _validate_zabbix_url(cls, value: str) -> str:
        if any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError("ZABBIX_API_URL contains whitespace or control characters")
        try:
            parsed = urlsplit(value)
            _ = parsed.port
        except ValueError:
            raise ValueError("ZABBIX_API_URL has an invalid host or port") from None
        if parsed.scheme != "https" or not parsed.hostname:
            raise ValueError("ZABBIX_API_URL must be an absolute HTTPS URL")
        if parsed.username is not None or parsed.password is not None or parsed.fragment or parsed.query:
            raise ValueError("ZABBIX_API_URL must not contain credentials, a query or a fragment")
        if not parsed.path.endswith("/api_jsonrpc.php"):
            raise ValueError("ZABBIX_API_URL must point to api_jsonrpc.php")
        return value


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
