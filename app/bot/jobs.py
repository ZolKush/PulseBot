"""Scheduled job registration for the Telegram application."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from telegram.ext import Application

from ..access.security import auth_prune_task
from ..config import (
    AUTH_PRUNE_INTERVAL_SEC,
    MAINT_RESTART_NOTIFY_DELAY_SEC,
    MAINT_RESTART_REMINDER_INTERVAL_SEC,
    NAVIGATION_CLEANUP_ENABLED,
    NAVIGATION_CLEANUP_INTERVAL_SEC,
    OUTBOX_PROCESS_INTERVAL_SEC,
    logger,
)
from ..maintenance.jobs import maint_restart_notify, maint_schedule_tick
from ..messaging.outbox import process_outbox_job
from ..subscriptions.requests.lifecycle import subscription_lifecycle_job
from ..tickets.jobs import release_orphaned_tickets
from .job_queue import ManagedJobQueue

JobCallback = Callable[[Any], Any]


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
    message_cleanup_job: JobCallback,
) -> None:
    """Register jobs in their stable persistence and inspection order."""
    if application.job_queue is None:
        logger.warning("JobQueue недоступен: для фоновых задач установите python-telegram-bot[job-queue].")
        return
    _register_repeating_jobs(application, message_cleanup_job=message_cleanup_job)
    if isinstance(application.job_queue, ManagedJobQueue):
        for job in application.job_queue.jobs():
            job.callback = application.job_queue.managed(job.callback)
