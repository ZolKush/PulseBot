"""Shared Telegram presentation primitives.

This module is deliberately independent from feature handlers.  Feature-specific
views may build on these primitives, but the primitives never import features.
"""

from __future__ import annotations

import html
from datetime import datetime
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.error import BadRequest

from ..config import TZ, logger
from .text_limits import clip_html_message as clip_html_message
from .text_limits import clip_plain_text


def html_escape(value: str) -> str:
    return html.escape(value or "", quote=False)


UI_OK = "✅"
UI_WARN = "⚠️"
UI_ERR = "❌"
UI_INFO = "ℹ️"


def ui_ok_text(text: str) -> str:
    return f"{UI_OK} {text}"


def ui_warn_text(text: str) -> str:
    return f"{UI_WARN} {text}"


def ui_error_text(text: str) -> str:
    return f"{UI_ERR} Ошибка: {text}"


def ui_info_text(text: str) -> str:
    return f"{UI_INFO} {text}"


def breadcrumbs(*parts: str) -> str:
    items = [str(part).strip() for part in parts if str(part or "").strip()]
    return " > ".join(items)


def clip_text(value: str, limit: int = 3300) -> str:
    if value is None:
        return ""
    return clip_plain_text(str(value), limit)


def clip_html(value: str, limit: int = 3300) -> str:
    """Escape HTML and then truncate to the Telegram-oriented limit."""
    return clip_html_message(html_escape("" if value is None else str(value)), limit)


def wrap_as_codeblock_html(text: str, limit: int = 3300) -> str:
    return f"<pre><code>{clip_html(text, limit)}</code></pre>"


def format_dt_human(value: Any, *, empty: str = "-", tz_label: str = "по МСК") -> str:
    raw = str(value or "").strip()
    if not raw:
        return empty
    try:
        parsed = datetime.fromisoformat(raw)
        parsed = parsed.replace(tzinfo=TZ) if parsed.tzinfo is None else parsed.astimezone(TZ)
    except (ValueError, OverflowError):
        return raw
    return f"{parsed.strftime('%d.%m.%Y %H:%M')} {tz_label}"


async def safe_edit_or_reply(
    message: Any,
    text: str,
    *,
    reply_markup: InlineKeyboardMarkup | None = None,
    parse_mode: str = ParseMode.HTML,
) -> Any | None:
    """Edit a Telegram message and fall back to a reply without duplicates."""
    if message is None:
        return None
    text = clip_html_message(text) if parse_mode == ParseMode.HTML else clip_text(text, limit=4000)
    try:
        return await message.edit_text(text, parse_mode=parse_mode, reply_markup=reply_markup)
    except BadRequest as error:
        if "message is not modified" in str(error).lower():
            return message
        if not any(
            marker in str(error).lower()
            for marker in ("message to edit not found", "message can't be edited", "message can’t be edited")
        ):
            raise
        logger.warning("edit_text не удался (%s), отправляю новое сообщение", error)
    try:
        return await message.reply_text(text, parse_mode=parse_mode, reply_markup=reply_markup)
    except Exception as error:
        logger.error("Не удалось отправить сообщение после неудачного edit_text: %s", error)
        return None


# Existing formatters insert SEP on a separate line.  An empty value preserves
# vertical spacing without showing a decorative rule.
SEP = ""

URGENCY_LABELS = {
    "p1": "🔥 Критично",
    "p2": "⚠️ Важно",
    "p3": "📋 Обычно",
}


def urgency_label(code: str | None) -> str:
    key = str(code or "").strip().lower()
    return URGENCY_LABELS.get(key, str(code or "-"))


def urgency_emoji(code: str | None) -> str:
    key = str(code or "").strip().lower()
    label = URGENCY_LABELS.get(key)
    return label.split()[0] if label else "📋"


def btn_back(callback_data: str, label: str = "⬅️ Назад") -> InlineKeyboardButton:
    return InlineKeyboardButton(label, callback_data=callback_data)


def btn_home() -> InlineKeyboardButton:
    return InlineKeyboardButton("🏠 Меню", callback_data="menu:home")


def pager_row(cb_prefix: str, page: int, total_pages: int) -> list[InlineKeyboardButton]:
    row: list[InlineKeyboardButton] = []
    if page > 0:
        row.append(InlineKeyboardButton("◀", callback_data=f"{cb_prefix}{page - 1}"))
    row.append(InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data=f"{cb_prefix}{page}"))
    if page < total_pages - 1:
        row.append(InlineKeyboardButton("▶", callback_data=f"{cb_prefix}{page + 1}"))
    return row


def plural_ru(number: int, one: str, few: str, many: str) -> str:
    value = abs(number) % 100
    last = value % 10
    if 11 <= value <= 19:
        return many
    if last == 1:
        return one
    if 2 <= last <= 4:
        return few
    return many


def humanize_hhmm(hours: int, minutes: int) -> str:
    parts = []
    if hours:
        parts.append(f"{hours} {plural_ru(hours, 'час', 'часа', 'часов')}")
    if minutes:
        parts.append(f"{minutes} {plural_ru(minutes, 'минута', 'минуты', 'минут')}")
    return " ".join(parts) if parts else "0 минут"


def humanize_until(minutes: int) -> str:
    total = max(0, int(minutes))
    days, remainder = divmod(total, 1440)
    hours, mins = divmod(remainder, 60)
    if days:
        parts = [f"{days} {plural_ru(days, 'сутки', 'суток', 'суток')}"]
        if hours:
            parts.append(f"{hours} {plural_ru(hours, 'час', 'часа', 'часов')}")
        return " ".join(parts)
    if hours:
        parts = [f"{hours} {plural_ru(hours, 'час', 'часа', 'часов')}"]
        if mins:
            parts.append(f"{mins} {plural_ru(mins, 'минута', 'минуты', 'минут')}")
        return " ".join(parts)
    return f"{mins} {plural_ru(mins, 'минута', 'минуты', 'минут')}"
