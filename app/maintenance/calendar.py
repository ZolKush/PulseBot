"""Telegram calendar and input parsing for maintenance scheduling."""

from __future__ import annotations

import calendar as _calendar
import re
from datetime import date, timedelta

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from ..bot.ui import clip_text
from .policy import MAINT_SCOPE_ALL, MAX_MAINT_HOURS

CAL_NOOP = "maint:cal:noop"
_SCOPE_PAGE_SIZE = 10

_RU_MONTHS = [
    "",
    "Январь",
    "Февраль",
    "Март",
    "Апрель",
    "Май",
    "Июнь",
    "Июль",
    "Август",
    "Сентябрь",
    "Октябрь",
    "Ноябрь",
    "Декабрь",
]
_RU_WEEKDAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]


def scope_kb(servers: dict[str, str], page: int = 0) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton("🌐 Общие техработы", callback_data=f"maint:scope:{MAINT_SCOPE_ALL}")]
    ]
    ordered = sorted(servers.items(), key=lambda host: (host[1].casefold(), host[0]))
    last_page = max(0, (len(ordered) - 1) // _SCOPE_PAGE_SIZE)
    page = max(0, min(page, last_page))
    start = page * _SCOPE_PAGE_SIZE
    for key, label in ordered[start : start + _SCOPE_PAGE_SIZE]:
        rows.append([InlineKeyboardButton(f"🖥 {clip_text(label, limit=60)}", callback_data=f"maint:scope:{key}")])
    navigation: list[InlineKeyboardButton] = []
    if page > 0:
        navigation.append(InlineKeyboardButton("◀ Назад", callback_data=f"maint:scope:page:{page - 1}"))
    if page < last_page:
        navigation.append(InlineKeyboardButton("Далее ▶", callback_data=f"maint:scope:page:{page + 1}"))
    if navigation:
        rows.append(navigation)
    rows.append([InlineKeyboardButton("🏠 Меню", callback_data="menu:home")])
    return InlineKeyboardMarkup(rows)


def urgency_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🔥 Срочные", callback_data="maint:urgency:urgent"),
                InlineKeyboardButton("🗓 Плановые", callback_data="maint:urgency:planned"),
            ],
            [InlineKeyboardButton("🏠 Меню", callback_data="menu:home")],
        ]
    )


def maint_mode_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🗓 Запланировать техработы", callback_data="maint:mode:schedule")],
            [InlineKeyboardButton("🚨 Объявить техработы", callback_data="maint:mode:announce")],
            [InlineKeyboardButton("🏠 Меню", callback_data="menu:home")],
        ]
    )


def calendar_grid(year: int, month: int, today: date, horizon: date) -> list[list[tuple[str, str]]]:
    calendar = _calendar.Calendar(firstweekday=0)
    weeks: list[list[tuple[str, str]]] = []
    for week in calendar.monthdatescalendar(year, month):
        row: list[tuple[str, str]] = []
        for day in week:
            if day.month != month:
                row.append((" ", CAL_NOOP))
            elif day < today or day > horizon:
                row.append((f"·{day.day}", CAL_NOOP))
            else:
                row.append((str(day.day), f"maint:cal:day:{day.isoformat()}"))
        weeks.append(row)
    return weeks


def schedule_calendar_kb(year: int, month: int, *, today: date, horizon_days: int = 365) -> InlineKeyboardMarkup:
    horizon = today + timedelta(days=horizon_days)
    if not 1 <= month <= 12 or not (today.year, today.month) <= (year, month) <= (horizon.year, horizon.month):
        year, month = today.year, today.month
    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(f"{_RU_MONTHS[month]} {year}", callback_data=CAL_NOOP)],
        [InlineKeyboardButton(weekday, callback_data=CAL_NOOP) for weekday in _RU_WEEKDAYS],
    ]
    for week in calendar_grid(year, month, today, horizon):
        rows.append([InlineKeyboardButton(label, callback_data=callback) for label, callback in week])

    previous_available = (year, month) > (today.year, today.month)
    next_first = date(year + (1 if month == 12 else 0), 1 if month == 12 else month + 1, 1)
    next_available = next_first <= horizon
    navigation: list[InlineKeyboardButton] = []
    if previous_available:
        previous_year, previous_month = (year - 1, 12) if month == 1 else (year, month - 1)
        navigation.append(
            InlineKeyboardButton(
                "◀",
                callback_data=f"maint:cal:nav:{previous_year:04d}-{previous_month:02d}",
            )
        )
    if next_available:
        navigation.append(
            InlineKeyboardButton(
                "▶",
                callback_data=f"maint:cal:nav:{next_first.year:04d}-{next_first.month:02d}",
            )
        )
    if navigation:
        rows.append(navigation)
    rows.append([InlineKeyboardButton("🏠 Меню", callback_data="menu:home")])
    return InlineKeyboardMarkup(rows)


def parse_hhmm(text: str) -> tuple[int, int] | None:
    match = re.fullmatch(r"\s*(\d{1,3})\s*:\s*([0-5]\d)\s*", text or "")
    if not match:
        return None
    hours, minutes = int(match.group(1)), int(match.group(2))
    if hours == 0 and minutes == 0:
        return None
    if hours > MAX_MAINT_HOURS or (hours * 60 + minutes) > MAX_MAINT_HOURS * 60:
        return None
    return hours, minutes


def parse_clock_range(text: str) -> tuple[int, int, int, int] | None:
    match = re.fullmatch(
        r"\s*(\d{1,2})\s*:\s*([0-5]\d)\s*[-–]\s*(\d{1,2})\s*:\s*([0-5]\d)\s*",
        text or "",
    )
    if not match:
        return None
    start_hour, start_minute, end_hour, end_minute = (int(match.group(index)) for index in range(1, 5))
    if not (0 <= start_hour <= 23 and 0 <= end_hour <= 23):
        return None
    return start_hour, start_minute, end_hour, end_minute


_calendar_grid = calendar_grid
