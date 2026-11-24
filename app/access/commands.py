from __future__ import annotations

import contextlib
import hmac
from datetime import datetime

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from ..bot.guards import (
    get_user_id,
    get_user_meta,
    is_authorized,
    is_enabled,
    is_private,
    reply_disabled,
    reply_need_auth,
    require_auth,
    require_private,
)
from ..bot.help import render_help_message
from ..bot.menu import main_menu_inline_kb, show_main_menu
from ..config import ADMIN_PASSWORD, OWNER_PASSWORD, TZ, logger
from ..messaging.message_cleanup import record_navigation_result
from ..messaging.review_sync import sync_service_review_messages_for_user
from ..storage import get_user_meta_copy, product_settings_snapshot
from .operations import (
    authorize_admin,
    can_restore_paid_access,
    claim_service_owner,
    logout_user,
    restore_paid_access,
)
from .security import (
    auth_actor_key,
    auth_lock_remaining_sec,
    register_auth_failure,
    reset_actor_auth_limits,
)
from .views import request_access_markup


async def delete_sensitive_auth_message(update: Update) -> None:
    message = update.effective_message
    if message:
        with contextlib.suppress(Exception):
            await message.delete()


@require_private
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update):
        meta = get_user_meta(get_user_id(update) or 0)
        if meta and meta.get("access_state") == "blocked":
            await reply_disabled(update)
        else:
            user = update.effective_user
            restored = (
                await restore_paid_access(
                    user_id=user.id,
                    username=user.username,
                    first_name=user.first_name,
                    last_name=user.last_name,
                    restored_at=datetime.now(TZ),
                )
                if user
                else None
            )
            if restored:
                await show_main_menu(
                    update,
                    text="Оплаченный доступ восстановлен автоматически ✅\n\nМеню:",
                )
            else:
                await reply_need_auth(update)
        return
    if not is_enabled(update):
        await reply_disabled(update)
        return
    await show_main_menu(update)


@require_private
@require_auth
async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = render_help_message(product_settings_snapshot())
    query = update.callback_query
    message = update.effective_message
    if query and message:
        await query.answer()
        result = await query.edit_message_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=main_menu_inline_kb(update),
        )
        await record_navigation_result(update, result)
    elif message:
        result = await message.reply_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=main_menu_inline_kb(update),
        )
        await record_navigation_result(update, result)


async def cmd_auth(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_private(update):
        await delete_sensitive_auth_message(update)
        return
    message = update.effective_message
    try:
        remaining = auth_lock_remaining_sec(update)
        if remaining > 0:
            if message:
                await message.reply_text(f"Слишком много попыток. Повторите через {remaining} сек.")
            return

        text = (message.text if message else "") or ""
        parts = text.split(maxsplit=1)
        if len(parts) < 2 or not parts[1]:
            if message:
                await message.reply_text(
                    "Эта команда предназначена только для администраторов. Формат: <b>/auth пароль</b>",
                    parse_mode=ParseMode.HTML,
                )
            return

        user = update.effective_user
        if not user:
            return
        existing = get_user_meta_copy(user.id) or {}
        if existing.get("access_state") == "blocked" and existing.get("role") != "admin":
            await reply_disabled(update)
            return

        if not hmac.compare_digest(parts[1].encode("utf-8"), ADMIN_PASSWORD.encode("utf-8")):
            register_auth_failure(update)
            logger.warning(
                "Admin auth failed for %s",
                auth_actor_key(update),
                extra={"action": "auth_failed"},
            )
            if message:
                await message.reply_text("Пароль неверный.")
            return

        pending_count = await authorize_admin(
            user_id=user.id,
            username=user.username,
            first_name=user.first_name,
            last_name=user.last_name,
            authenticated_at=datetime.now(TZ),
        )
        reset_actor_auth_limits(update)
        logger.info(
            "Administrator authenticated user_id=%s",
            user.id,
            extra={"user_id": user.id, "action": "auth_ok"},
        )
        pending_note = (
            f"\nОжидают решения заявок: <b>{pending_count}</b>. Откройте раздел «Пользователи»."
            if pending_count
            else ""
        )
        await show_main_menu(
            update,
            text=f"Авторизация администратора успешна ✅{pending_note}\n\nМеню:",
        )
    finally:
        # Sensitive command messages are deleted on malformed input and
        # lockout paths as well.
        await delete_sensitive_auth_message(update)


async def cmd_owner(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Claim the one service-owner role with its separate secret."""

    if not is_private(update):
        await delete_sensitive_auth_message(update)
        return
    message = update.effective_message
    try:
        remaining = auth_lock_remaining_sec(update)
        if remaining > 0:
            if message:
                await message.reply_text(f"Слишком много попыток. Повторите через {remaining} сек.")
            return
        user_id = get_user_id(update)
        current = get_user_meta(user_id or 0) if user_id is not None else None
        if (
            user_id is None
            or not current
            or current.get("role") != "admin"
            or current.get("access_state") != "approved"
        ):
            if message:
                await message.reply_text("Сначала авторизуйтесь как администратор.")
            return
        text = (message.text if message else "") or ""
        parts = text.split(maxsplit=1)
        if len(parts) != 2 or not parts[1]:
            if message:
                await message.reply_text(
                    "Формат: <b>/owner отдельный_пароль</b>",
                    parse_mode=ParseMode.HTML,
                )
            return
        if not hmac.compare_digest(parts[1].encode("utf-8"), OWNER_PASSWORD.encode("utf-8")):
            register_auth_failure(update)
            logger.warning(
                "Owner claim failed for %s",
                auth_actor_key(update),
                extra={"action": "owner_claim_failed"},
            )
            if message:
                await message.reply_text("Пароль неверный.")
            return

        outcome = await claim_service_owner(
            user_id=user_id,
            claimed_at=datetime.now(TZ),
        )
        if outcome == "claimed":
            reset_actor_auth_limits(update)
            logger.info(
                "Service owner claimed by user_id=%s",
                user_id,
                extra={"user_id": user_id, "action": "owner_claimed"},
            )
            await show_main_menu(
                update,
                text="Роль руководителя сервиса активирована ✅\n\nМеню:",
            )
            try:
                await sync_service_review_messages_for_user(context.bot, user_id)
            except Exception:
                logger.exception(
                    "Could not synchronize requests cancelled by owner claim user_id=%s",
                    user_id,
                )
        # If an owner already exists, state and interface remain unchanged.
    finally:
        await delete_sensitive_auth_message(update)


@require_private
async def cmd_logout(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = get_user_id(update)
    message = update.effective_message
    if user_id is None:
        return
    updated = await logout_user(
        user_id=user_id,
        logged_out_at=datetime.now(TZ),
    )
    if not message:
        return
    if not updated:
        await message.reply_text("Активной авторизации нет.")
        return
    if updated.get("role") == "admin":
        await message.reply_text("Вы вышли из администраторской учётной записи. Для возврата используйте /auth.")
        return
    if can_restore_paid_access(updated, now=datetime.now(TZ)):
        await message.reply_text(
            "Вы вышли из бота. Оплаченная подписка сохранена; для возврата без повторного одобрения отправьте /start."
        )
        return
    await message.reply_text(
        "Вы вышли из бота. Запись и ограничения доступа сохранены; для возврата отправьте новую заявку.",
        reply_markup=request_access_markup(),
    )


__all__ = [
    "cmd_auth",
    "cmd_help",
    "cmd_logout",
    "cmd_owner",
    "cmd_start",
    "delete_sensitive_auth_message",
]
