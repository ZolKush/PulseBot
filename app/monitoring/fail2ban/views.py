"""Fail2Ban keyboards and HTML presentation."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from ...bot.ui import SEP, html_escape, plural_ru
from ...config import SERVER_KEY_PATTERN, TZ
from .local import fail2ban_stat_with_sudo_async
from .models import Fail2banEvent
from .source import get_server


def menu_keyboard(server_key: str) -> InlineKeyboardMarkup:
    try:
        server = get_server(server_key)
    except KeyError:
        server = None
    if server and not server.fail2ban_enabled:
        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "⬅️ Назад",
                        callback_data=f"f2b:back:{server_key}",
                    )
                ],
                [InlineKeyboardButton("🏠 Меню", callback_data="menu:home")],
            ]
        )
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "📜 Логи (tail)",
                    callback_data=f"f2b:tail:{server_key}:200",
                )
            ],
            [
                InlineKeyboardButton(
                    "🧾 Выжимка за сутки",
                    callback_data=f"f2b:digest:{server_key}",
                )
            ],
            [
                InlineKeyboardButton(
                    "⬅️ Назад",
                    callback_data=f"f2b:back:{server_key}",
                )
            ],
            [InlineKeyboardButton("🏠 Меню", callback_data="menu:home")],
        ]
    )


def tail_keyboard(server_key: str, current: int) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for line_count in (200, 600, 2000, 5000):
        label = f"{line_count} строк" + (" ✅" if line_count == current else "")
        row.append(
            InlineKeyboardButton(
                label,
                callback_data=f"f2b:tail:{server_key}:{line_count}",
            )
        )
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.extend(
        [
            [
                InlineKeyboardButton(
                    "⬅️ Назад",
                    callback_data=f"f2b:menu:{server_key}",
                )
            ],
            [InlineKeyboardButton("🏠 Меню", callback_data="menu:home")],
        ]
    )
    return InlineKeyboardMarkup(rows)


def digest_keyboard(server_key: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "⬅️ Назад",
                    callback_data=f"f2b:menu:{server_key}",
                )
            ],
            [InlineKeyboardButton("🏠 Меню", callback_data="menu:home")],
        ]
    )


def parse_server_key(data: str, action: str) -> str | None:
    match = re.fullmatch(
        rf"f2b:{action}:({SERVER_KEY_PATTERN})",
        data or "",
    )
    return match.group(1) if match else None


def parse_server_tail(data: str) -> tuple[str, int] | None:
    match = re.fullmatch(
        rf"f2b:tail:({SERVER_KEY_PATTERN}):(\d{{1,5}})",
        data or "",
    )
    return (match.group(1), int(match.group(2))) if match else None


def format_datetime(timestamp: datetime) -> str:
    return timestamp.astimezone(TZ).strftime("%d.%m.%Y %H:%M")


def format_event_datetime(timestamp: datetime) -> str:
    return timestamp.astimezone(TZ).strftime("%d.%m %H:%M")


async def build_fail2ban_menu_text(server_key: str) -> str:
    try:
        server = get_server(server_key)
    except KeyError:
        return "Сервер не найден."
    path = server.fail2ban_log_path
    title = f"🛡 <b>Fail2ban — {html_escape(server.label)}</b>\n{SEP}\n"
    if not server.fail2ban_enabled:
        return title + "Ежедневный сбор и просмотр отключены для этого сервера в конфигурации."
    try:
        stat = await fail2ban_stat_with_sudo_async(path)
        if stat is None:
            raise RuntimeError("stat unavailable")
        size_bytes, modified_at = stat
        return (
            title
            + f"Файл: <code>{html_escape(str(path))}</code>\n"
            + f"Размер: <code>{size_bytes / 1024.0:.1f} KiB</code>\n"
            + f"Изменён: <code>{html_escape(format_datetime(modified_at))}</code>\n\n"
            + "Выберите действие:"
        )
    except Exception:
        return title + f"Файл: <code>{html_escape(str(path))}</code>\n\nВыберите действие:"


def build_fail2ban_digest_text(
    events: list[Fail2banEvent],
    since: datetime,
    until: datetime,
) -> str:
    per_jail: dict[str, dict[str, Any]] = {}
    for event in events:
        counters = per_jail.setdefault(
            event.jail,
            {"ban": [], "unban": 0, "restore": 0, "started": 0, "stopped": 0},
        )
        if event.action == "Ban":
            counters["ban"].append(event)
        elif event.action == "Unban":
            counters["unban"] += 1
        elif event.action == "Restore Ban":
            counters["restore"] += 1
            counters["ban"].append(event)
        elif event.action == "Jail started":
            counters["started"] += 1
        elif event.action == "Jail stopped":
            counters["stopped"] += 1

    total_bans = sum(len(values["ban"]) for values in per_jail.values())
    header = (
        "🛡 <b>Fail2ban — выжимка за сутки</b>\n"
        f"Период: <code>{html_escape(format_datetime(since))}</code> — "
        f"<code>{html_escape(format_datetime(until))}</code>\n{SEP}\n"
    )
    has_other_events = any(values["unban"] or values["started"] or values["stopped"] for values in per_jail.values())
    if total_bans == 0 and not has_other_events:
        return header + "\nСобытий не найдено."

    lines: list[str] = [header]
    for jail in sorted(per_jail):
        values = per_jail[jail]
        bans: list[Fail2banEvent] = values["ban"]
        if not bans and not (values["unban"] or values["started"] or values["stopped"]):
            continue
        lines.append(f"\n<b>[{html_escape(jail)}]</b>")
        if values["started"]:
            lines.append(f"• Запусков jail: <code>{values['started']}</code>")
        if values["stopped"]:
            lines.append(f"• Остановок jail: <code>{values['stopped']}</code>")
        if bans:
            ban_line = f"• {plural_ru(len(bans), 'Бан', 'Бана', 'Банов')}: <code>{len(bans)}</code>"
            if values["restore"]:
                ban_line += f" (повторных: <code>{values['restore']}</code>)"
            lines.append(ban_line)
            recent = sorted(bans, key=lambda event: event.ts)[-20:]
            for event in recent:
                lines.append(
                    f"  • <code>{html_escape(format_event_datetime(event.ts))}</code>"
                    f" — <code>{html_escape(event.ip or '-')}</code> "
                    f"({html_escape(event.action)})"
                )
            if len(bans) > 20:
                hidden = len(bans) - 20
                lines.append(f"  … ещё <code>{hidden}</code> {plural_ru(hidden, 'событие', 'события', 'событий')}")
        if values["unban"]:
            lines.append(f"• Разбанов: <code>{values['unban']}</code>")

    output: list[str] = []
    current_length = 0
    for line in lines:
        extra = len(line) + (1 if output else 0)
        if current_length + extra > 3760:
            output.append("… (обрезано из-за лимита Telegram)")
            break
        output.append(line)
        current_length += extra
    return "\n".join(output)


__all__ = ["build_fail2ban_digest_text", "build_fail2ban_menu_text"]
