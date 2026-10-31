"""Scheduled job registration for the Telegram application."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from datetime import time as dtime
from typing import Any

from telegram.ext import Application

from ..access.security import auth_prune_task
from ..config import (
    AUTH_PRUNE_INTERVAL_SEC,
    DAILY_NODE_STATUS_REFRESH_AT,
    DNS_DAILY_REFRESH_AT,
    DNS_STARTUP_REFRESH_DELAY_SEC,
    FAIL2BAN_DAILY_AT,
    MAINT_RESTART_NOTIFY_DELAY_SEC,
    MAINT_RESTART_REMINDER_INTERVAL_SEC,
    NAVIGATION_CLEANUP_ENABLED,
    NAVIGATION_CLEANUP_INTERVAL_SEC,
    OUTBOX_PROCESS_INTERVAL_SEC,
    SERVERS,
    TZ,
    logger,
)
from ..maintenance.jobs import maint_restart_notify, maint_schedule_tick
from ..messaging.outbox import process_outbox_job
from ..monitoring.fail2ban.jobs import fail2ban_daily_digest
from ..monitoring.status.jobs import (
    DOCKER_STATUS_REFRESH_INTERVAL_SEC,
    DOCKER_STATUS_STARTUP_DELAY_SEC,
    daily_node_status_refresh,
    dns_daily_refresh,
    docker_status_refresh,
)
from ..monitoring.tls.jobs import (
    TLS_DEADLINE_EVALUATION_INTERVAL_SEC,
    TLS_DEADLINE_EVALUATION_STARTUP_DELAY_SEC,
    TLS_NETWORK_REFRESH_INTERVAL_SEC,
    TLS_NETWORK_STARTUP_DELAY_SEC,
    tls_certificate_check_job,
    tls_deadline_evaluation_job,
)
from ..subscriptions.requests.lifecycle import subscription_lifecycle_job
from ..tickets.jobs import release_orphaned_tickets
from .job_queue import ManagedJobQueue

JobCallback = Callable[[Any], Any]


def _log_monitoring_configuration() -> None:
    local_count = sum(server.mode == "local" for server in SERVERS.values())
    panel_count = sum(server.mode == "panel" for server in SERVERS.values())
    metrics_count = sum(server.monitoring_source == "remnawave" for server in SERVERS.values())
    dns_count = sum(len(server.check_a_domains) for server in SERVERS.values())
    tls_count = sum(len(server.tls_endpoints) for server in SERVERS.values())
    container_count = sum(len(server.monitor_containers) for server in SERVERS.values())
    logger.info(
        "Monitoring config loaded servers=%s local=%s panel=%s metrics=%s dns_domains=%s tls_endpoints=%s containers=%s",
        len(SERVERS),
        local_count,
        panel_count,
        metrics_count,
        dns_count,
        tls_count,
        container_count,
        extra={"action": "monitoring_config_loaded", "source": "startup", "total": len(SERVERS)},
    )
    for server in SERVERS.values():
        logger.info(
            "Monitoring server key=%s transport=%s source=%s dns=%s tls=%s containers=%s fail2ban=%s",
            server.key,
            server.mode,
            server.monitoring_source,
            len(server.check_a_domains),
            len(server.tls_endpoints),
            len(server.monitor_containers),
            server.fail2ban_enabled,
            extra={"action": "monitoring_server_loaded", "source": "startup", "server_key": server.key},
        )


def parse_schedule_hhmm(raw: str, *, field_name: str, fallback: str) -> tuple[int, int]:
    """Parse an HH:MM setting, logging and using a known-safe fallback."""
    try:
        parsed = datetime.strptime(raw, "%H:%M").time()
        return parsed.hour, parsed.minute
    except Exception:
        logger.warning("Invalid %s=%s, fallback to %s", field_name, raw, fallback)
        parsed = datetime.strptime(fallback, "%H:%M").time()
        return parsed.hour, parsed.minute


def _register_daily_jobs(application: Application, *, bot_mode: str) -> None:
    job_queue = application.job_queue
    if job_queue is None:
        raise RuntimeError("JobQueue is required to register daily jobs")
    hour, minute = parse_schedule_hhmm(
        FAIL2BAN_DAILY_AT,
        field_name="FAIL2BAN_DAILY_AT",
        fallback="12:00",
    )
    dns_hour, dns_minute = parse_schedule_hhmm(
        DNS_DAILY_REFRESH_AT,
        field_name="DNS_DAILY_REFRESH_AT",
        fallback="03:05",
    )
    job_queue.run_daily(
        fail2ban_daily_digest,
        time=dtime(hour=hour, minute=minute, tzinfo=TZ),
        name="fail2ban_digest",
    )
    job_queue.run_daily(
        dns_daily_refresh,
        time=dtime(hour=dns_hour, minute=dns_minute, tzinfo=TZ),
        name="dns_daily_refresh",
    )
    job_queue.run_once(
        dns_daily_refresh,
        when=DNS_STARTUP_REFRESH_DELAY_SEC,
        name="dns_refresh_startup",
    )
    if bot_mode != "mixed":
        return

    status_hour, status_minute = parse_schedule_hhmm(
        DAILY_NODE_STATUS_REFRESH_AT,
        field_name="DAILY_NODE_STATUS_REFRESH_AT",
        fallback="12:00",
    )
    job_queue.run_daily(
        daily_node_status_refresh,
        time=dtime(hour=status_hour, minute=status_minute, tzinfo=TZ),
        name="daily_node_status_refresh",
    )
    job_queue.run_once(
        daily_node_status_refresh,
        when=DNS_STARTUP_REFRESH_DELAY_SEC + 5,
        name="daily_node_status_startup",
    )


def _register_repeating_jobs(
    application: Application,
    *,
    message_cleanup_job: JobCallback,
) -> None:
    job_queue = application.job_queue
    if job_queue is None:
        raise RuntimeError("JobQueue is required to register repeating jobs")
    job_queue.run_repeating(
        maint_restart_notify,
        interval=MAINT_RESTART_REMINDER_INTERVAL_SEC,
        first=MAINT_RESTART_NOTIFY_DELAY_SEC,
        name="maint_active_reminder",
    )
    job_queue.run_repeating(
        maint_schedule_tick,
        interval=60,
        first=10,
        name="maint_schedule_tick",
    )
    job_queue.run_repeating(
        auth_prune_task,
        interval=AUTH_PRUNE_INTERVAL_SEC,
        first=AUTH_PRUNE_INTERVAL_SEC,
        name="auth_prune",
    )
    job_queue.run_repeating(
        process_outbox_job,
        interval=OUTBOX_PROCESS_INTERVAL_SEC,
        first=1,
        name="outbox_delivery",
    )
    job_queue.run_repeating(
        release_orphaned_tickets,
        interval=60,
        first=3,
        name="ticket_orphan_release",
    )
    job_queue.run_repeating(
        subscription_lifecycle_job,
        interval=60,
        first=5,
        name="subscription_lifecycle",
    )
    job_queue.run_repeating(
        docker_status_refresh,
        interval=DOCKER_STATUS_REFRESH_INTERVAL_SEC,
        first=DOCKER_STATUS_STARTUP_DELAY_SEC,
        name="docker_status_refresh",
    )
    job_queue.run_once(
        tls_certificate_check_job,
        when=TLS_NETWORK_STARTUP_DELAY_SEC,
        name="tls_certificate_check_startup",
    )
    job_queue.run_repeating(
        tls_certificate_check_job,
        interval=TLS_NETWORK_REFRESH_INTERVAL_SEC,
        first=TLS_NETWORK_REFRESH_INTERVAL_SEC,
        name="tls_certificate_check",
    )
    job_queue.run_repeating(
        tls_deadline_evaluation_job,
        interval=TLS_DEADLINE_EVALUATION_INTERVAL_SEC,
        first=TLS_DEADLINE_EVALUATION_STARTUP_DELAY_SEC,
        name="tls_deadline_evaluation",
    )
    if NAVIGATION_CLEANUP_ENABLED:
        job_queue.run_repeating(
            message_cleanup_job,
            interval=NAVIGATION_CLEANUP_INTERVAL_SEC,
            first=NAVIGATION_CLEANUP_INTERVAL_SEC,
            name="message_cleanup",
        )


def register_jobs(
    application: Application,
    *,
    bot_mode: str,
    message_cleanup_job: JobCallback,
) -> None:
    """Register jobs in their stable persistence and inspection order."""
    if application.job_queue is None:
        logger.warning("JobQueue недоступен: для ежедневной выжимки установите python-telegram-bot[job-queue].")
        return
    _log_monitoring_configuration()
    _register_daily_jobs(application, bot_mode=bot_mode)
    _register_repeating_jobs(application, message_cleanup_job=message_cleanup_job)
    if isinstance(application.job_queue, ManagedJobQueue):
        for job in application.job_queue.jobs():
            job.callback = application.job_queue.managed(job.callback)
