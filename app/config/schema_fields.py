from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .locations import ROOT_DIR


class SettingsFields(BaseModel):
    """Non-secret process settings loaded from ``data/conf/bot.json``."""

    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True, hide_input_in_errors=True)

    TZ: str = "Europe/Moscow"
    LOG_LEVEL: str = "INFO"
    LOG_JSON: bool = False

    DATA_DIR: str = str(ROOT_DIR / "data")

    ZABBIX_API_URL: str
    CAPTCHA_ENABLED: bool = False
    CAPTCHA_URL: str = ""
    # The Docker template opts into the container interface. Compose publishes only on host loopback.
    CAPTCHA_BIND_HOST: Literal["127.0.0.1", "0.0.0.0"] = "127.0.0.1"  # nosec B104
    CAPTCHA_PORT: int = Field(default=8094, ge=1, le=65535)
    TURNSTILE_SITE_KEY: str = Field(default="", pattern=r"^[a-zA-Z0-9_-]{0,128}$")

    MAINT_RESTART_NOTIFY_DELAY_SEC: int = 2
    MAINT_RESTART_REMINDER_INTERVAL_SEC: int = 1800

    AUTH_FAIL_WINDOW_SEC: int = 300
    AUTH_MAX_FAILS_IN_WINDOW: int = 5
    AUTH_GLOBAL_MAX_FAILS_IN_WINDOW: int = 40
    AUTH_LOCKOUT_SEC: int = 600
    AUTH_PRUNE_INTERVAL_SEC: int = 300
    ACCESS_REQUEST_COOLDOWN_SEC: int = 300
    ERROR_NOTIFY_INTERVAL_SEC: int = 300
    OUTBOX_PROCESS_INTERVAL_SEC: int = 10

    NAVIGATION_CLEANUP_ENABLED: bool = True
    NAVIGATION_RETENTION_HOURS: int = 24
    NAVIGATION_CLEANUP_INTERVAL_SEC: int = 1800

    INSTANCE_LOCK_PATH: str = ""
    PTB_PERSISTENCE_PATH: str = ""


__all__ = ["SettingsFields"]
