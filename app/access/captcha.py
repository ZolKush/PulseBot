"""Admission gate for Telegram updates while the registration CAPTCHA is enabled."""

from __future__ import annotations

from urllib.parse import urlencode

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Update, WebAppInfo
from telegram.error import TelegramError
from telegram.ext import ApplicationHandlerStop, ContextTypes

from ..bot.conversations import navigation_command
from ..bot.guards import is_private, require_private
from ..config import CAPTCHA_URL, logger
from ..storage import captcha_allowed, captcha_session
from .commands import delete_sensitive_auth_message
from .views import request_access_markup

_PRIVACY_URL = "https://telegram.org/privacy-tpa"


@require_private
async def cmd_privacy(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.callback_query:
        await update.callback_query.answer()
    message = update.effective_message
    if message:
        await message.reply_text(
            "При включённой защите регистрации ZeroNet использует Cloudflare Turnstile. "
            "Мини-приложение передаёт боту данные, подписанные Telegram, чтобы связать проверку с вашей учётной записью. "
            "Cloudflare обрабатывает данные браузера для защиты от ботов. После успешной проверки бот "
            "сохраняет ваш Telegram ID и время проверки.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("Стандартная политика Telegram", url=_PRIVACY_URL)]]
            ),
        )


async def continue_registration(bot: Bot, user_id: int) -> None:
    """Resume the ordinary access-request flow after the durable verification."""
    await bot.send_message(
        chat_id=user_id,
        text="Проверка пройдена ✅\n\nДоступ предоставляется после одобрения администратором.",
        reply_markup=request_access_markup(),
    )


async def _reply_before_registration(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int) -> None:
    query = update.callback_query
    command = navigation_command(update) if query is None else None
    if command in {"auth", "login", "owner"}:
        await delete_sensitive_auth_message(update)
    message = update.effective_message
    if not is_private(update) or not message:
        return
    if query and query.data == "menu:privacy":
        await cmd_privacy(update, context)
        return
    if query:
        await query.answer()
    if command != "start":
        await message.reply_text("Для регистрации отправьте /start.")
        return

    session = await captcha_session(user_id)
    url = CAPTCHA_URL.rstrip("/") + "/?" + urlencode({"session": session})
    await message.reply_text(
        "Чтобы защитить ZeroNet от автоматических регистраций, пройдите короткую проверку. "
        "После неё вы сможете запросить доступ к боту.",
        reply_markup=InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("Пройти проверку", web_app=WebAppInfo(url=url))],
                [InlineKeyboardButton("Политика конфиденциальности", callback_data="menu:privacy")],
            ]
        ),
    )


async def captcha_guard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if user is None:
        raise ApplicationHandlerStop
    try:
        if await captcha_allowed(user.id):
            return
        await _reply_before_registration(update, context, user.id)
    except TelegramError as exc:
        logger.warning("Registration gate reply failed (%s)", type(exc).__name__)
    except Exception as exc:
        # PTB continues with later groups after an ordinary handler exception.
        # Only a successful eligibility check may let this update through.
        logger.warning("Registration gate unavailable (%s)", type(exc).__name__)
        try:
            if not update.callback_query and navigation_command(update) in {"auth", "login", "owner"}:
                await delete_sensitive_auth_message(update)
            if is_private(update) and update.effective_message:
                await update.effective_message.reply_text("Проверка временно недоступна. Попробуйте /start позже.")
        except Exception as reply_error:
            logger.warning("Registration gate reply failed (%s)", type(reply_error).__name__)
    raise ApplicationHandlerStop
