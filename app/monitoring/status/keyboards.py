"""Status navigation keyboards and callback parsers."""

from __future__ import annotations

import re

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from ...config import SERVER_KEY_PATTERN, SERVERS
from .common import server_flag, server_keys


def status_pick_keyboard() -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for key in server_keys():
        server = SERVERS[key]
        rows.append(
            [
                InlineKeyboardButton(
                    f"{server_flag(server)} {server.label}",
                    callback_data=f"status:show:{server.key}",
                )
            ]
        )
    rows.append([InlineKeyboardButton("🏠 Меню", callback_data="menu:home")])
    return InlineKeyboardMarkup(rows)


def status_pick_text() -> str:
    return "<b>Выберите сервер</b>\nКакой статус показать?\n\nℹ️ Нажмите кнопку сервера один раз и подождите загрузку."


def status_actions_keyboard(
    admin_mode: bool,
    server_key: str,
) -> InlineKeyboardMarkup:
    host_checks = server_key not in SERVERS or SERVERS[server_key].mode != "panel"
    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton("🔄 Обновить", callback_data=f"status:refresh:{server_key}")],
    ]
    if admin_mode and host_checks:
        rows.extend(
            [
                [InlineKeyboardButton("🛡️ UFW", callback_data=f"status:ufw:{server_key}")],
                [
                    InlineKeyboardButton("🐳 Контейнеры", callback_data=f"docker:list:{server_key}"),
                    InlineKeyboardButton("🔐 TLS", callback_data=f"tls:list:{server_key}"),
                ],
            ]
        )
    if admin_mode and not host_checks:
        rows.append([InlineKeyboardButton("🔐 TLS", callback_data=f"tls:list:{server_key}")])
    if admin_mode and host_checks:
        rows.append(
            [
                InlineKeyboardButton(
                    "🛡️ Fail2ban: logs",
                    callback_data=f"f2b:menu:{server_key}",
                )
            ]
        )
    if len(SERVERS) > 1:
        rows.append(
            [
                InlineKeyboardButton(
                    "⬅️ К выбору сервера",
                    callback_data="status:pick",
                )
            ]
        )
    rows.append([InlineKeyboardButton("🏠 Меню", callback_data="menu:home")])
    return InlineKeyboardMarkup(rows)


def ufw_actions_keyboard(server_key: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "🔄 Обновить UFW",
                    callback_data=f"status:ufw:{server_key}",
                )
            ],
            [
                InlineKeyboardButton(
                    "⬅️ Назад к статусу",
                    callback_data=f"status:show:{server_key}",
                )
            ],
            [InlineKeyboardButton("🏠 Меню", callback_data="menu:home")],
        ]
    )


def resolve_server_key(data: str, prefix: str) -> str | None:
    match = re.fullmatch(prefix + rf":({SERVER_KEY_PATTERN})", data or "")
    return match.group(1) if match else None


def parse_ufw_callback(data: str) -> str | None:
    return resolve_server_key(data, r"status:ufw")


def parse_dns_refresh_callback(data: str) -> str | None:
    return resolve_server_key(data, r"status:dnsrefresh")


def parse_tls_refresh_callback(data: str) -> str | None:
    return resolve_server_key(data, r"status:tlsrefresh")


def parse_refresh_callback(data: str) -> str | None:
    return resolve_server_key(data, r"status:refresh")
