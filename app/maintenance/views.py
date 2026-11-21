"""Maintenance texts and inline keyboards."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from ..bot.ui import SEP, html_escape, humanize_hhmm, humanize_until
from ..config import TZ, TZ_NAME
from .policy import MAINT_SCOPE_ALL, maint_heading, minutes_to_hhmm, scope_line


def format_datetime_short(value: datetime) -> str:
    return value.astimezone(TZ).strftime("%d.%m.%Y %H:%M")


def format_maintenance(scope: str, urgency: str, hours: int, minutes: int, author: str, *, label: str = "") -> str:
    now = datetime.now(TZ).strftime("%d.%m.%Y %H:%M")
    urgency_label = "срочные" if urgency == "urgent" else "плановые"
    return (
        f"{maint_heading(urgency, 'Работы начались')}\n"
        f"{SEP}\n"
        f"{scope_line(scope, label)}\n"
        f"• Тип: <b>{html_escape(urgency_label)}</b>\n"
        f"• Оценка простоя: <b>{html_escape(humanize_hhmm(hours, minutes))}</b>\n"
        f"• Ответственный: <b>{html_escape(author)}</b>\n"
        f"• Старт: <code>{html_escape(now)}</code> ({html_escape(TZ_NAME)})"
    )


def format_scheduled_maintenance(
    scope: str, start_at: datetime, end_at: datetime, author: str, *, label: str = ""
) -> str:
    return (
        f"{maint_heading('planned', 'Работы запланированы')}\n"
        f"{SEP}\n"
        f"{scope_line(scope, label)}\n"
        f"• Начало: <code>{html_escape(format_datetime_short(start_at))}</code> ({html_escape(TZ_NAME)})\n"
        f"• Окончание: <code>{html_escape(format_datetime_short(end_at))}</code> ({html_escape(TZ_NAME)})\n"
        f"• Ответственный: <b>{html_escape(author)}</b>"
    )


def maintenance_control_keyboard(maintenance_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Завершить", callback_data=f"maint:endconfirm:{maintenance_id}"),
                InlineKeyboardButton("⏳ Продлить", callback_data=f"maint:extend:{maintenance_id}"),
            ],
            [InlineKeyboardButton("🏠 Меню", callback_data="menu:home")],
        ]
    )


def maintenance_end_confirm_keyboard(maintenance_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("✅ Подтвердить завершение", callback_data=f"maint:end:{maintenance_id}")],
            [InlineKeyboardButton("⬅️ Назад", callback_data=f"maint:cancelend:{maintenance_id}")],
            [InlineKeyboardButton("🏠 Меню", callback_data="menu:home")],
        ]
    )


def scheduled_control_keyboard(scheduled_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🚨 Объявить техработы сейчас", callback_data="maint:mode:announce")],
            [InlineKeyboardButton("❌ Отменить план", callback_data=f"maint:schedcancel:{scheduled_id}")],
            [InlineKeyboardButton("🏠 Меню", callback_data="menu:home")],
        ]
    )


def scheduled_cancel_confirm_keyboard(scheduled_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "✅ Подтвердить отмену",
                    callback_data=f"maint:schedcancelconfirm:{scheduled_id}",
                )
            ],
            [InlineKeyboardButton("⬅️ Назад", callback_data=f"maint:schedcancelback:{scheduled_id}")],
            [InlineKeyboardButton("🏠 Меню", callback_data="menu:home")],
        ]
    )


def maintenance_notice_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Меню", callback_data="menu:home")]])


def maintenance_delivery_status(users_count: int, admins_count: int) -> str:
    return (
        "Уведомления сохранены в надёжной очереди:\n"
        f"• Пользователи: {users_count}\n"
        f"• Админы (кроме инициатора): {admins_count}"
    )


def maintenance_menu_text(scheduled: dict[str, Any] | None = None) -> str:
    lines = ["<b>Техработы</b>", "", "Выберите действие:"]
    if scheduled:
        lines.extend(["", scheduled_panel_text(scheduled)])
    return "\n".join(lines)


def maintenance_panel_text(maintenance: dict[str, Any]) -> str:
    urgency_label = "срочные" if maintenance.get("urgency") == "urgent" else "плановые"
    hours, minutes = minutes_to_hhmm(int(maintenance.get("duration_min", 0) or 0))
    started_at = _parse_datetime(maintenance.get("started_at"))
    expected_end = _parse_datetime(maintenance.get("expected_end"))
    lines = [
        maint_heading(maintenance.get("urgency"), "Работы сейчас активны"),
        SEP,
        scope_line(str(maintenance.get("scope", MAINT_SCOPE_ALL)), str(maintenance.get("scope_label") or "")),
        f"• Тип: <b>{html_escape(urgency_label)}</b>",
        f"• Оценка простоя: <b>{html_escape(humanize_hhmm(hours, minutes))}</b>",
    ]
    if started_at:
        lines.append(f"• Старт: <code>{html_escape(format_datetime_short(started_at))}</code> ({html_escape(TZ_NAME)})")
    if expected_end:
        lines.append(
            f"• Окончание: <code>{html_escape(format_datetime_short(expected_end))}</code> ({html_escape(TZ_NAME)})"
        )
    lines.extend(["", "Выберите действие:"])
    return "\n".join(lines)


def scheduled_panel_text(scheduled: dict[str, Any]) -> str:
    start_at = _parse_datetime(scheduled.get("scheduled_start"))
    end_at = _parse_datetime(scheduled.get("scheduled_end"))
    lines = [
        maint_heading("planned", "Работы запланированы"),
        SEP,
        scope_line(str(scheduled.get("scope", MAINT_SCOPE_ALL)), str(scheduled.get("scope_label") or "")),
    ]
    if start_at:
        lines.append(f"• Начало: <code>{html_escape(format_datetime_short(start_at))}</code> ({html_escape(TZ_NAME)})")
    if end_at:
        lines.append(f"• Окончание: <code>{html_escape(format_datetime_short(end_at))}</code> ({html_escape(TZ_NAME)})")
    lines.append("• Уведомления уйдут за 3 суток, 12 часов и 30 минут до начала, а также в момент старта.")
    return "\n".join(lines)


def maintenance_extend_notice(
    maintenance: dict[str, Any],
    hours: int,
    minutes: int,
    author: str,
) -> str:
    expected_end = _parse_datetime(maintenance.get("expected_end"))
    end_text = format_datetime_short(expected_end) if expected_end else "-"
    return (
        f"{maint_heading(maintenance.get('urgency'), 'Срок работ продлён')}\n"
        f"{SEP}\n"
        f"{scope_line(str(maintenance.get('scope', MAINT_SCOPE_ALL)), str(maintenance.get('scope_label') or ''))}\n"
        f"• Новый ориентир простоя: <b>{html_escape(humanize_hhmm(hours, minutes))}</b>\n"
        f"• Окончание: <code>{html_escape(end_text)}</code> ({html_escape(TZ_NAME)})\n"
        f"• Ответственный: <b>{html_escape(author)}</b>\n\n"
        "Спасибо за понимание 🙏"
    )


def maintenance_end_notice(
    maintenance: dict[str, Any],
    author: str,
    ended_at: datetime | None = None,
) -> str:
    ended_at = ended_at or datetime.now(TZ)
    return (
        f"{maint_heading(maintenance.get('urgency'), 'Работы завершены')}\n"
        f"{SEP}\n"
        f"{scope_line(str(maintenance.get('scope', MAINT_SCOPE_ALL)), str(maintenance.get('scope_label') or ''))}\n"
        f"• Время: <code>{html_escape(format_datetime_short(ended_at))}</code> ({html_escape(TZ_NAME)})\n"
        f"• Ответственный: <b>{html_escape(author)}</b>\n\n"
        "Спасибо за терпение 🙌"
    )


def maintenance_active_reminder_text(maintenance: dict[str, Any]) -> str:
    return "⚠️ <b>Техработы превысили заявленный срок простоя</b>\n\n" + maintenance_panel_text(maintenance)


def maintenance_scheduled_soon_notice(scheduled: dict[str, Any], remaining_min: int) -> str:
    start_at = _parse_datetime(scheduled.get("scheduled_start"))
    end_at = _parse_datetime(scheduled.get("scheduled_end"))
    author = scheduled.get("author_name") if scheduled.get("author_signature_version") == 1 else "Техническая поддержка"
    return (
        f"{maint_heading('planned', f'Начнутся через {humanize_until(remaining_min)}')}\n"
        f"{SEP}\n"
        f"{scope_line(str(scheduled.get('scope', MAINT_SCOPE_ALL)), str(scheduled.get('scope_label') or ''))}\n"
        f"• Начало: <code>{html_escape(format_datetime_short(start_at) if start_at else '-')}</code> ({html_escape(TZ_NAME)})\n"
        f"• Окончание: <code>{html_escape(format_datetime_short(end_at) if end_at else '-')}</code> ({html_escape(TZ_NAME)})\n"
        f"• Ответственный: <b>{html_escape(str(author))}</b>"
    )


def maintenance_scheduled_cancel_notice(scheduled: dict[str, Any]) -> str:
    start_at = _parse_datetime(scheduled.get("scheduled_start"))
    return (
        f"{maint_heading('planned', 'Запланированные работы отменены')}\n"
        f"{SEP}\n"
        f"{scope_line(str(scheduled.get('scope', MAINT_SCOPE_ALL)), str(scheduled.get('scope_label') or ''))}\n"
        f"• Планировались на: <code>{html_escape(format_datetime_short(start_at) if start_at else '-')}</code> ({html_escape(TZ_NAME)})\n\n"
        "Ранее анонсированные работы проводиться не будут."
    )


def maintenance_scheduled_start_notice(scheduled: dict[str, Any]) -> str:
    end_at = _parse_datetime(scheduled.get("scheduled_end"))
    author = scheduled.get("author_name") if scheduled.get("author_signature_version") == 1 else "Техническая поддержка"
    return (
        f"{maint_heading('planned', 'Работы начались')}\n"
        f"{SEP}\n"
        f"{scope_line(str(scheduled.get('scope', MAINT_SCOPE_ALL)), str(scheduled.get('scope_label') or ''))}\n"
        f"• Плановое окончание: <code>{html_escape(format_datetime_short(end_at) if end_at else '-')}</code> ({html_escape(TZ_NAME)})\n"
        f"• Ответственный: <b>{html_escape(str(author))}</b>"
    )


def _parse_datetime(value: object) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value or ""))
    except (TypeError, ValueError):
        return None


format_maint = format_maintenance
format_scheduled_maint = format_scheduled_maintenance
_fmt_dt_short = format_datetime_short
_maint_control_kb = maintenance_control_keyboard
_maint_end_confirm_kb = maintenance_end_confirm_keyboard
_scheduled_control_kb = scheduled_control_keyboard
_scheduled_cancel_confirm_kb = scheduled_cancel_confirm_keyboard
_maint_panel_text = maintenance_panel_text
_scheduled_panel_text = scheduled_panel_text
_maint_extend_notice = maintenance_extend_notice
_maint_end_notice = maintenance_end_notice
_maint_active_reminder_text = maintenance_active_reminder_text
_maint_scheduled_soon_notice = maintenance_scheduled_soon_notice
_maint_scheduled_cancel_notice = maintenance_scheduled_cancel_notice
_maint_scheduled_start_notice = maintenance_scheduled_start_notice
