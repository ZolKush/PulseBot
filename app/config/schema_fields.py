from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from .locations import ROOT_DIR


class SettingsFields(BaseModel):
    """Non-secret process settings loaded from ``data/conf/bot.json``."""

    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True, hide_input_in_errors=True)

    TZ: str = "Europe/Moscow"
    LOG_LEVEL: str = "INFO"
    LOG_JSON: bool = False

    DATA_DIR: str = str(ROOT_DIR / "data")

    DNS_RESOLVERS: list[str] = Field(default_factory=lambda: ["1.1.1.1", "8.8.8.8", "77.88.8.8"])
    DNS_DAILY_REFRESH_AT: str = "03:05"
    DNS_STARTUP_REFRESH_DELAY_SEC: int = 5

    FAIL2BAN_DAILY_AT: str = "12:00"
    FAIL2BAN_DIGEST_TAIL_LINES: int = 20000
    FAIL2BAN_DIGEST_MAX_BYTES: int = 3_000_000

    MAINT_RESTART_NOTIFY_DELAY_SEC: int = 2
    MAINT_RESTART_REMINDER_INTERVAL_SEC: int = 1800

    SUBPROC_SHORT_TIMEOUT: int = 3
    SUBPROC_MEDIUM_TIMEOUT: int = 8
    SUBPROC_MAX_OUTPUT_BYTES: int = 1_000_000

    PRIVILEGED_HELPER_BIN: str = "/usr/local/libexec/maintbot-helper"

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
    STATUS_CACHE_TTL_SEC: int = 5

    REMNAWAVE_METRICS_URL: str = ""
    REMNAWAVE_METRICS_TIMEOUT_SEC: int = 3
    REMNAWAVE_METRICS_CACHE_TTL_SEC: int = 8
    REMNAWAVE_METRICS_MAX_BYTES: int = 2_000_000
    REMNAWAVE_HIDDEN_UUIDS: list[str] = Field(default_factory=list)
    DAILY_NODE_STATUS_REFRESH_AT: str = "12:00"


__all__ = ["SettingsFields"]
