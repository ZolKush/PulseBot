"""Calendar, scheduling, and schedule-cancellation handlers."""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta

from telegram import Update
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import ContextTypes, ConversationHandler

from ..bot.guards import get_user_id, require_maintenance, staff_title
from ..bot.ui import html_escape
from ..config import TZ, logger
from ..messaging.message_cleanup import record_navigation_result
from ..storage import get_scheduled_maintenance
from .calendar import parse_clock_range, schedule_calendar_kb
from .operations import cancel_scheduled_maintenance, schedule_maintenance
from .policy import MAINT_SCOPE_ALL, normalize_scope, scope_fingerprint
from .records import build_scheduled_maintenance_record
from .state import (
    STATE_MAINT_SCHEDULE_DATE,
    STATE_MAINT_SCHEDULE_RANGE,
    clear_maintenance_context,
    maintenance_context,
)
from .views import (
    format_scheduled_maintenance,
    maintenance_delivery_status,
    maintenance_notice_menu_keyboard,
    scheduled_cancel_confirm_keyboard,
    scheduled_control_keyboard,
    scheduled_panel_text,
)


@require_maintenance
async def maint_cal_noop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query:
        await query.answer()
    return STATE_MAINT_SCHEDULE_DATE


@require_maintenance
async def maint_cal_nav(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return ConversationHandler.END
    await query.answer()
    match = re.fullmatch(r"maint:cal:nav:(\d{4})-(\d{2})", query.data or "")
    if not match:
        return STATE_MAINT_SCHEDULE_DATE
    year, month = int(match.group(1)), int(match.group(2))
    today = datetime.now(TZ).date()
    if (year, month) < (today.year, today.month):
        year, month = today.year, today.month
    await query.edit_message_text(
        "Выберите дату техработ:",
        reply_markup=schedule_calendar_kb(year, month, today=today),
    )
    return STATE_MAINT_SCHEDULE_DATE


@require_maintenance
async def maint_cal_day(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return ConversationHandler.END
    await query.answer()
    match = re.fullmatch(r"maint:cal:day:(\d{4})-(\d{2})-(\d{2})", query.data or "")
    if not match:
        return STATE_MAINT_SCHEDULE_DATE
    today = datetime.now(TZ).date()
    try:
        chosen = date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        chosen = None
    if chosen is None or not today <= chosen <= today + timedelta(days=365):
        await query.edit_message_text(
            "Выберите корректную дату техработ в пределах следующего года:",
            reply_markup=schedule_calendar_kb(today.year, today.month, today=today),
        )
        return STATE_MAINT_SCHEDULE_DATE
    data = maintenance_context(context)
    data["maint_sched_date"] = chosen.isoformat()
    if query.message and query.message.chat:
        data["maint_panel_chat_id"] = query.message.chat.id
        data["maint_panel_msg_id"] = query.message.message_id
    await query.edit_message_text(
        f"Дата: <code>{html_escape(chosen.strftime('%d.%m.%Y'))}</code>\n\n"
        "Введите интервал в формате ЧЧ:ММ - ЧЧ:ММ (например, 17:00 - 18:00).",
        parse_mode=ParseMode.HTML,
    )
    return STATE_MAINT_SCHEDULE_RANGE


@require_maintenance
async def maint_schedule_range(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    parsed = parse_clock_range((message.text if message else "") or "")
    if not parsed:
        if message:
            await message.reply_text("Некорректно. Введите интервал в формате ЧЧ:ММ - ЧЧ:ММ, например 17:00 - 18:00.")
        return STATE_MAINT_SCHEDULE_RANGE

    start_hour, start_minute, end_hour, end_minute = parsed
    now = datetime.now(TZ)
    data = maintenance_context(context)
    try:
        chosen = date.fromisoformat(str(data.get("maint_sched_date") or ""))
    except ValueError:
        chosen = now.date()
    start_at = now.replace(
        year=chosen.year,
        month=chosen.month,
        day=chosen.day,
        hour=start_hour,
        minute=start_minute,
        second=0,
        microsecond=0,
    )
    end_at = start_at.replace(hour=end_hour, minute=end_minute)
    if start_at <= now:
        if message:
            await message.reply_text("Время начала уже прошло. Выберите более позднее время.")
        return STATE_MAINT_SCHEDULE_RANGE
    if end_at <= start_at:
        if message:
            await message.reply_text("Окончание должно быть позже начала в рамках одного дня, например 17:00 - 18:00.")
        return STATE_MAINT_SCHEDULE_RANGE

    scope = normalize_scope(str(data.get("maint_scope", MAINT_SCOPE_ALL)))
    author = staff_title(update)
    author_id = get_user_id(update)
    if scope != MAINT_SCOPE_ALL and data.get("maint_scope_fingerprint") != scope_fingerprint(scope):
        if message:
            await message.reply_text("Выберите узел техработ заново через /maint.")
        clear_maintenance_context(context)
        return ConversationHandler.END
    label = str(data.get("maint_scope_label") or "")
    scheduled = build_scheduled_maintenance_record(scope, start_at, end_at, author_id, author, label=label)
    if not await schedule_maintenance(scheduled):
        if message:
            await message.reply_text(
                "Другой администратор уже создал план или запустил работы. Откройте /maint заново."
            )
        clear_maintenance_context(context)
        return ConversationHandler.END
    logger.info(
        "Maintenance scheduled by user_id=%s scope=%s start=%s end=%s",
        author_id,
        scope,
        scheduled.get("scheduled_start"),
        scheduled.get("scheduled_end"),
    )

    panel_text = (
        format_scheduled_maintenance(scope, start_at, end_at, author, label=label)
        + "\n\nУведомления будут поставлены в очередь за 3 суток, 12 часов и 30 минут, а также в момент начала."
    )
    panel_chat_id = data.get("maint_panel_chat_id")
    panel_message_id = data.get("maint_panel_msg_id")
    if panel_chat_id and panel_message_id:
        try:
            await context.bot.edit_message_text(
                chat_id=panel_chat_id,
                message_id=panel_message_id,
                text=panel_text,
                parse_mode=ParseMode.HTML,
                reply_markup=maintenance_notice_menu_keyboard(),
            )
            clear_maintenance_context(context)
            return ConversationHandler.END
        except BadRequest as exc:
            if "message is not modified" in str(exc).lower():
                clear_maintenance_context(context)
                return ConversationHandler.END
            logger.warning("Не удалось обновить панель техработ (%s), отправляю новое сообщение", exc)
        except Exception as exc:
            logger.warning("Не удалось обновить панель техработ (%s), отправляю новое сообщение", exc)

    if message:
        result = await message.reply_text(
            panel_text,
            parse_mode=ParseMode.HTML,
            reply_markup=maintenance_notice_menu_keyboard(),
        )
        await record_navigation_result(update, result)
    clear_maintenance_context(context)
    return ConversationHandler.END


@require_maintenance
async def maint_sched_cancel_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return
    await query.answer()
    match = re.fullmatch(r"maint:schedcancel:([0-9a-f]+)", query.data or "")
    if not match:
        return
    schedule_id = match.group(1)
    scheduled = get_scheduled_maintenance()
    if not scheduled or str(scheduled.get("id") or "") != schedule_id:
        await query.edit_message_text("Запланированные техработы не найдены или уже неактуальны.")
        return
    text = scheduled_panel_text(scheduled) + "\n\n<b>Отменить запланированные техработы?</b>"
    await query.edit_message_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=scheduled_cancel_confirm_keyboard(schedule_id),
    )


@require_maintenance
async def maint_sched_cancel_back_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return
    await query.answer()
    match = re.fullmatch(r"maint:schedcancelback:([0-9a-f]+)", query.data or "")
    if not match:
        return
    schedule_id = match.group(1)
    scheduled = get_scheduled_maintenance()
    if not scheduled or str(scheduled.get("id") or "") != schedule_id:
        await query.edit_message_text("Запланированные техработы не найдены или уже неактуальны.")
        return
    await query.edit_message_text(
        scheduled_panel_text(scheduled),
        parse_mode=ParseMode.HTML,
        reply_markup=scheduled_control_keyboard(schedule_id),
    )


@require_maintenance
async def maint_sched_cancel_confirm_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return
    await query.answer()
    match = re.fullmatch(r"maint:schedcancelconfirm:([0-9a-f]+)", query.data or "")
    if not match:
        return
    schedule_id = match.group(1)
    scheduled, users_count, admins_count, queued_notice = await cancel_scheduled_maintenance(
        schedule_id,
        actor_id=get_user_id(update),
    )
    if not isinstance(scheduled, dict):
        await query.edit_message_text("Запланированные техработы не найдены или уже неактуальны.")
        return

    logger.info("Scheduled maintenance cancelled by user_id=%s sched_id=%s", get_user_id(update), schedule_id)
    if queued_notice:
        await query.edit_message_text(
            "✅ Запланированные техработы отменены; уведомление сохранено в очереди.\n\n"
            + maintenance_delivery_status(users_count, admins_count)
        )
    else:
        await query.edit_message_text("✅ Запланированные техработы отменены.")


__all__ = [
    "maint_cal_day",
    "maint_cal_nav",
    "maint_cal_noop",
    "maint_sched_cancel_back_cb",
    "maint_sched_cancel_cb",
    "maint_sched_cancel_confirm_cb",
    "maint_schedule_range",
]
