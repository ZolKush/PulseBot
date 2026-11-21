"""Background reminders and activation of scheduled maintenance."""

from __future__ import annotations

from datetime import datetime

from telegram.ext import ContextTypes

from ..bot.guards import authorized_ids, maintenance_manager_ids
from ..config import TZ, logger
from ..messaging.outbox import message_payload
from ..monitoring.zabbix import ZabbixError
from ..storage import get_active_maintenance, get_scheduled_maintenance, make_outbox_event
from .notifications import make_maintenance_notice_event
from .operations import (
    activate_scheduled_maintenance,
    clear_invalid_schedule,
    expire_schedule,
    mark_schedule_thresholds,
    queue_active_reminder,
)
from .policy import (
    MAINT_SCOPE_ALL,
    MAINT_WARN_THRESHOLDS_MIN,
    due_thresholds,
    initial_notified_thresholds,
    scope_is_current,
    server_items,
)
from .views import (
    maintenance_active_reminder_text,
    maintenance_scheduled_soon_notice,
    maintenance_scheduled_start_notice,
)


async def maint_restart_notify(context: ContextTypes.DEFAULT_TYPE) -> None:
    del context
    maintenance = get_active_maintenance()
    if not maintenance or not str(maintenance.get("id", "") or ""):
        return
    expected_end = str(maintenance.get("expected_end") or "")
    try:
        deadline = datetime.fromisoformat(expected_end)
    except ValueError:
        logger.warning("Active maintenance has invalid expected end id=%s", maintenance.get("id"))
        return
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=TZ)
    checked_at = datetime.now(TZ)
    if checked_at <= deadline or maintenance.get("overdue_reminded_for") == expected_end:
        return
    admin_ids = maintenance_manager_ids()
    if not admin_ids:
        return
    maintenance_id = str(maintenance.get("id") or "")
    reminder_kind = f"maintenance_admin_reminder_{maintenance_id}"
    event = make_outbox_event(
        kind=reminder_kind,
        recipient_ids=admin_ids,
        payload=message_payload(
            maintenance_active_reminder_text(maintenance) + "\n\nОткройте «/maint» для управления.",
            reply_markup=[[{"text": "🏠 Меню", "callback_data": "menu:home"}]],
        ),
    )
    await queue_active_reminder(maintenance_id, expected_end, checked_at, reminder_kind, event)


async def maint_schedule_tick(context: ContextTypes.DEFAULT_TYPE) -> None:
    scheduled = get_scheduled_maintenance()
    if not scheduled:
        return

    try:
        start_at = datetime.fromisoformat(str(scheduled.get("scheduled_start") or ""))
        end_at = datetime.fromisoformat(str(scheduled.get("scheduled_end") or ""))
    except (TypeError, ValueError):
        logger.warning("Scheduled maintenance has invalid timestamps, clearing record")
        await clear_invalid_schedule(str(scheduled.get("id") or ""))
        return

    if start_at.tzinfo is None:
        start_at = start_at.replace(tzinfo=TZ)
    if end_at.tzinfo is None:
        end_at = end_at.replace(tzinfo=TZ)
    now = datetime.now(TZ)

    if now >= end_at:
        logger.warning(
            "Scheduled maintenance %s expired (end=%s), clearing",
            scheduled.get("id"),
            scheduled.get("scheduled_end"),
        )
        admin_ids = authorized_ids(role_filter="admin")
        users_were_notified = bool(scheduled.get("announced_thresholds")) or bool(scheduled.get("notified_before"))
        user_ids = authorized_ids(role_filter="user") if users_were_notified else []
        recipients = sorted(set(admin_ids + user_ids))
        event = (
            make_outbox_event(
                kind="maintenance_schedule_expired",
                recipient_ids=recipients,
                payload=message_payload(
                    "⚠️ <b>Запланированные техработы отменены</b>\n"
                    "Окно работ прошло, пока они не были запущены "
                    "(бот был недоступен или шли другие работы).",
                    reply_markup=[[{"text": "🏠 Меню", "callback_data": "menu:home"}]],
                ),
            )
            if recipients
            else None
        )
        await expire_schedule(scheduled, event)
        return

    current = scope_is_current(scheduled)
    if current and scheduled.get("scope", MAINT_SCOPE_ALL) != MAINT_SCOPE_ALL:
        try:
            current = scheduled["scope"] in await server_items()
        except ZabbixError:
            logger.warning("Zabbix unavailable while checking scheduled maintenance scope")
            return
    if not current:
        warned = bool(scheduled.get("announced_thresholds") or scheduled.get("notified_before"))
        recipients = authorized_ids() if warned else maintenance_manager_ids()
        event = (
            make_outbox_event(
                kind="maintenance_schedule_invalid",
                recipient_ids=recipients,
                payload=message_payload(
                    "⚠️ <b>Запланированные техработы отменены</b>\n"
                    "Сервер удалён или его конфигурация изменилась. Администратору необходимо создать план заново."
                ),
            )
            if recipients
            else None
        )
        await expire_schedule(scheduled, event)
        logger.warning("Cancelled maintenance schedule for obsolete server id=%s", scheduled.get("id"))
        return

    notified_raw = scheduled.get("notified_thresholds")
    if isinstance(notified_raw, list):
        notified = [int(value) for value in notified_raw if isinstance(value, int)]
    elif scheduled.get("notified_before"):
        notified = list(MAINT_WARN_THRESHOLDS_MIN)
    else:
        notified = initial_notified_thresholds(int((start_at - now).total_seconds() // 60))

    remaining_min = int((start_at - now).total_seconds() // 60)
    due = due_thresholds(notified, remaining_min)
    if due and now < start_at:
        event, _users_count, _admins_count = make_maintenance_notice_event(
            author_id=scheduled.get("author_id") if isinstance(scheduled.get("author_id"), int) else None,
            text=maintenance_scheduled_soon_notice(scheduled, remaining_min),
            kind="maintenance_schedule_warning",
        )
        updated_schedule = await mark_schedule_thresholds(
            scheduled,
            updated_notified=sorted(set(notified) | set(due)),
            due=due,
            updated_at=now,
            notice_event=event,
        )
        if not isinstance(updated_schedule, dict):
            return
        scheduled = updated_schedule

    if now < start_at or bool(scheduled.get("notified_start", False)):
        return
    active = get_active_maintenance()
    if active:
        logger.info(
            "Scheduled maintenance %s deferred: another maintenance %s is active",
            scheduled.get("id"),
            active.get("id"),
        )
        return

    event, _users_count, _admins_count = make_maintenance_notice_event(
        author_id=scheduled.get("author_id") if isinstance(scheduled.get("author_id"), int) else None,
        text=maintenance_scheduled_start_notice(scheduled),
        kind="maintenance_schedule_started",
    )
    activated = await activate_scheduled_maintenance(scheduled, event)
    if not activated:
        return
    logger.info(
        "Scheduled maintenance activated id=%s start=%s end=%s",
        scheduled.get("id"),
        scheduled.get("scheduled_start"),
        scheduled.get("scheduled_end"),
    )


__all__ = ["maint_restart_notify", "maint_schedule_tick"]
