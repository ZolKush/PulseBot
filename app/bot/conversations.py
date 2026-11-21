"""Conversation lifecycle helpers shared by the bot composition layer."""

from __future__ import annotations

import contextlib
import warnings
from collections.abc import Awaitable, Callable
from typing import Any

from telegram import Update
from telegram.ext import CallbackQueryHandler, ConversationHandler
from telegram.warnings import PTBUserWarning

from ..config import logger
from .navigation import clear_transient_user_context

_NAVIGATION_COMMANDS = frozenset(
    {
        "start",
        "menu",
        "help",
        "auth",
        "login",
        "logout",
        "owner",
        "health",
        "subscription",
        "users",
        "ticket",
        "maint",
        "cancel",
    }
)
_ROOT_NAVIGATION_CALLBACKS = frozenset(
    {"product:requests", "administration:show", "staff:profile", "profile:show", "product:profile"}
)

AbandonFlow = Callable[[Update, Any], Awaitable[Any]]


class NavigableConversationHandler(ConversationHandler):
    """ConversationHandler that can be ended by the global navigation preprocessor."""

    def matches_entry_point(self, update: Update) -> bool:
        return any(handler.check_update(update) not in (None, False) for handler in self.entry_points)

    def end_for_update(self, update: Update) -> bool:
        # PTB has no public API for ending a conversation from another handler.
        # _update_state is intentionally used instead of mutating the mapping directly:
        # with persistent handlers its TrackingDict records the deletion for the next flush.
        try:
            key = self._get_key(update)
        except RuntimeError:
            return False
        if key not in self._conversations:
            return False
        timeout_job = self.timeout_jobs.pop(key, None)
        if timeout_job is not None:
            with contextlib.suppress(Exception):
                timeout_job.schedule_removal()
        self._update_state(self.END, key)
        return True


def _navigation_command(update: Update) -> str | None:
    message = update.effective_message
    text = str(message.text or "").strip() if message else ""
    if not text.startswith("/"):
        return None
    return text.split(maxsplit=1)[0][1:].split("@", maxsplit=1)[0].casefold() or None


def _is_navigation_update(
    update: Update,
    conversations: list[NavigableConversationHandler],
    callback_handlers: list[CallbackQueryHandler],
) -> bool:
    command = _navigation_command(update)
    if command in _NAVIGATION_COMMANDS:
        return True
    data = update.callback_query.data if update.callback_query else None
    if isinstance(data, str) and (data.startswith("menu:") or data in _ROOT_NAVIGATION_CALLBACKS):
        return True
    if any(conversation.matches_entry_point(update) for conversation in conversations):
        return True
    return any(handler.check_update(update) not in (None, False) for handler in callback_handlers)


def _navigation_trigger(update: Update) -> str:
    command = _navigation_command(update)
    if command:
        return f"command:{command}"
    data = update.callback_query.data if update.callback_query else None
    return f"callback:{str(data)[:100]}"


async def reset_navigation_state(
    update: Update,
    context: Any,
    conversations: list[NavigableConversationHandler],
    callback_handlers: list[CallbackQueryHandler],
    *,
    abandon_flow: AbandonFlow,
) -> None:
    """End stale conversations before a global navigation update is handled."""
    if not _is_navigation_update(update, conversations, callback_handlers):
        return

    # A product request can be temporarily claimed while an administrator enters
    # a connection link. Release that claim before dropping the conversation.
    try:
        await abandon_flow(update, context)
    except Exception:
        # A temporary storage error must not make every menu button unusable.
        logger.exception(
            "Failed to release product flow during navigation reset",
            extra={
                "user_id": update.effective_user.id if update.effective_user else None,
                "action": "navigation_product_release_failed",
            },
        )
    ended = [conversation.name for conversation in conversations if conversation.end_for_update(update)]
    clear_transient_user_context(context)
    if ended:
        user_id = update.effective_user.id if update.effective_user else None
        logger.info(
            "Navigation state reset user_id=%s flows=%s trigger=%s",
            user_id,
            ",".join(str(name) for name in ended),
            _navigation_trigger(update),
            extra={"user_id": user_id, "action": "navigation_reset"},
        )


def conversation_handler(**kwargs: Any) -> NavigableConversationHandler:
    """Build a persistent mixed callback/text conversation without PTB noise."""
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore",
            message=r"If 'per_message=False', 'CallbackQueryHandler' will not be tracked for every message\..*",
            category=PTBUserWarning,
        )
        return NavigableConversationHandler(**kwargs)
