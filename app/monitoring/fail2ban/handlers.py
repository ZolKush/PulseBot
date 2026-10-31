"""Interactive Fail2Ban Telegram callbacks."""

from __future__ import annotations

from datetime import datetime, timedelta

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from ...bot.guards import require_admin
from ...bot.ui import SEP, html_escape, ui_error_text, wrap_as_codeblock_html
from ...config import (
    FAIL2BAN_DIGEST_MAX_BYTES,
    FAIL2BAN_DIGEST_TAIL_LINES,
    TZ,
)
from ...messaging.message_cleanup import record_navigation_result
from ..status.presenter import build_status_message
from .local import tail_text_file_with_sudo_async
from .parser import parse_fail2ban_events
from .source import first_server_key, get_server, server_timezone
from .views import (
    build_fail2ban_digest_text,
    build_fail2ban_menu_text,
    digest_keyboard,
    menu_keyboard,
    parse_server_key,
    parse_server_tail,
    tail_keyboard,
)


@require_admin
async def fail2ban_menu(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    query = update.callback_query
    message = update.effective_message
    if not message:
        return
    server_key = first_server_key()
    text = await build_fail2ban_menu_text(server_key)
    if query:
        await query.answer()
        result = await query.edit_message_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=menu_keyboard(server_key),
        )
        await record_navigation_result(update, result)
    else:
        result = await message.reply_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=menu_keyboard(server_key),
        )
        await record_navigation_result(update, result)


@require_admin
async def f2b_menu_cb(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    query = update.callback_query
    if not query:
        return
    await query.answer()
    server_key = parse_server_key(query.data or "", "menu") or first_server_key()
    await query.edit_message_text(
        await build_fail2ban_menu_text(server_key),
        parse_mode=ParseMode.HTML,
        reply_markup=menu_keyboard(server_key),
    )


@require_admin
async def f2b_tail_cb(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    query = update.callback_query
    if not query:
        return
    await query.answer("Загружаю логи…")
    parsed = parse_server_tail(query.data or "")
    if not parsed:
        return
    server_key, line_count = parsed
    line_count = 200 if line_count < 50 else min(5000, line_count)
    try:
        server = get_server(server_key)
    except KeyError:
        await query.edit_message_text(ui_error_text("сервер не найден."))
        return
    if not server.fail2ban_enabled:
        await query.edit_message_text(
            "Fail2ban отключён для этого сервера.",
            reply_markup=menu_keyboard(server_key),
        )
        return

    try:
        tail = await tail_text_file_with_sudo_async(
            server.fail2ban_log_path,
            n_lines=line_count,
        )
        if not tail.strip():
            payload = f"🛡 <b>Fail2ban — {html_escape(server.label)} · tail</b>\n\nЛог пуст или отсутствуют строки."
        else:
            payload = f"🛡 <b>Fail2ban — {html_escape(server.label)} · tail</b>\n{SEP}\n{wrap_as_codeblock_html(tail)}"
    except FileNotFoundError:
        payload = ui_error_text(f"лог-файл не найден: {html_escape(str(server.fail2ban_log_path))}")
    except PermissionError:
        payload = ui_error_text(f"нет прав на чтение: {html_escape(str(server.fail2ban_log_path))}")
    except Exception as exc:
        payload = ui_error_text(html_escape(str(exc)))
    await query.edit_message_text(
        payload,
        parse_mode=ParseMode.HTML,
        reply_markup=tail_keyboard(server_key, current=line_count),
    )


@require_admin
async def f2b_digest_cb(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    query = update.callback_query
    if not query:
        return
    await query.answer("Готовлю выжимку…")
    server_key = parse_server_key(query.data or "", "digest") or first_server_key()
    try:
        server = get_server(server_key)
    except KeyError:
        await query.edit_message_text(ui_error_text("сервер не найден."))
        return
    if not server.fail2ban_enabled:
        await query.edit_message_text(
            "Fail2ban отключён для этого сервера.",
            reply_markup=menu_keyboard(server_key),
        )
        return

    until = datetime.now(tz=TZ)
    since = until - timedelta(days=1)
    try:
        raw_tail = await tail_text_file_with_sudo_async(
            server.fail2ban_log_path,
            n_lines=FAIL2BAN_DIGEST_TAIL_LINES,
            max_bytes=FAIL2BAN_DIGEST_MAX_BYTES,
        )
        events = parse_fail2ban_events(
            raw_tail.splitlines(),
            timezone=server_timezone(server_key),
        )
        events = [event for event in events if since <= event.ts <= until]
        payload = f"🌍 <b>Сервер:</b> {html_escape(server.label)}\n" + build_fail2ban_digest_text(
            events, since=since, until=until
        )
    except FileNotFoundError:
        payload = (
            f"🛡 <b>Fail2ban — выжимка ({html_escape(server.label)})</b>\n\n"
            f"Лог-файл не найден: <code>{html_escape(server.fail2ban_log_path)}</code>"
        )
    except PermissionError:
        payload = (
            f"🛡 <b>Fail2ban — выжимка ({html_escape(server.label)})</b>\n\n"
            f"Нет прав на чтение: <code>{html_escape(server.fail2ban_log_path)}</code>"
        )
    except Exception as exc:
        payload = (
            f"🛡 <b>Fail2ban — выжимка ({html_escape(server.label)})</b>\n\nОшибка: <code>{html_escape(str(exc))}</code>"
        )
    await query.edit_message_text(
        payload,
        parse_mode=ParseMode.HTML,
        reply_markup=digest_keyboard(server_key),
    )


@require_admin
async def f2b_back_cb(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    query = update.callback_query
    if not query:
        return
    await query.answer()
    server_key = parse_server_key(query.data or "", "back") or first_server_key()
    text, markup = await build_status_message(update, server_key=server_key)
    await query.edit_message_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=markup,
    )


__all__ = [
    "f2b_back_cb",
    "f2b_digest_cb",
    "f2b_menu_cb",
    "f2b_tail_cb",
    "fail2ban_menu",
]
