"""Build the complete status message and navigation markup."""

from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update

from ...bot.ui import ui_error_text
from ...config import SERVERS
from .collectors import build_status_snapshot
from .common import default_server_target, get_server_target
from .keyboards import status_actions_keyboard, status_pick_keyboard
from .views import format_status_message


async def build_status_message(
    update: Update,
    server_key: str | None = None,
) -> tuple[str, InlineKeyboardMarkup | None]:
    server = get_server_target(server_key) if server_key else default_server_target()
    if not server:
        if not SERVERS:
            keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Меню", callback_data="menu:home")]])
            return ui_error_text("Сервер не настроен."), keyboard
        if len(SERVERS) > 1:
            return ui_error_text("Сервер не найден."), status_pick_keyboard()
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Меню", callback_data="menu:home")]])
        return ui_error_text("Сервер не найден."), keyboard

    snapshot = await build_status_snapshot(update, server)
    keyboard = status_actions_keyboard(
        admin_mode=snapshot.admin_mode,
        server_key=server.key,
    )
    return format_status_message(snapshot), keyboard


__all__ = ["build_status_message"]
