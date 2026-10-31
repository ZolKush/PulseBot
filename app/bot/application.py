"""Telegram application assembly and process-level execution."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from telegram import Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    MessageHandler,
    TypeHandler,
)

from ..config import (
    BOT_MODE,
    BOT_TOKEN,
    DATA_DIR,
    INSTANCE_LOCK_PATH,
    NAVIGATION_CLEANUP_ENABLED,
    NAVIGATION_RETENTION_HOURS,
    SERVER_KEY_PATTERN,
    SERVERS,
    logger,
)
from ..messaging.message_cleanup import MessageCleanupStats, MessageTracker, TrackingExtBot
from ..messaging.review_navigation import retire_review_card_for_navigation
from ..monitoring.remnawave.client import close_metrics_client
from ..monitoring.status.reconciliation import reconcile_configured_servers
from ..persistence.sqlite_schema import DATABASE_FILENAME
from ..runtime.lock import ALREADY_RUNNING_EXIT_CODE, InstanceAlreadyRunning, SingleInstanceLock
from ..subscriptions.requests.flow_cleanup import abandon_product_flow
from .conversations import reset_navigation_state
from .easter_eggs import RANEPA_TEXT, ranepa_easter_egg
from .errors import blocked_user_guard, fallback_text, on_error, unhandled_callback
from .flow_routes import PRIVATE_TEXT
from .job_queue import ManagedJobQueue
from .jobs import register_jobs
from .persistence_sqlite import build_sqlite_persistence
from .routes import register_routes


def _log_message_cleanup(stats: MessageCleanupStats, *, reason: str) -> None:
    if reason == "startup" or stats.deleted or stats.expired or stats.failed:
        logger.info(
            "Navigation cleanup (%s): chats=%s candidates=%s deleted=%s expired=%s failed=%s",
            reason,
            stats.tracked_chats,
            stats.candidates,
            stats.deleted,
            stats.expired,
            stats.failed,
            extra={"action": "message_cleanup"},
        )
    if stats.expired:
        logger.warning(
            "Navigation cleanup skipped %s panels older than Telegram's 48-hour deletion limit",
            stats.expired,
            extra={"action": "message_cleanup_expired"},
        )


async def _post_shutdown(_application: Application) -> None:
    await close_metrics_client()


def build_application(*, bot_mode: str = BOT_MODE) -> Application:
    """Build the complete PTB application without starting network polling."""
    if not BOT_TOKEN:
        raise RuntimeError("Не задан BOT_TOKEN в app/env.secrets или переменных окружения")

    message_tracker = MessageTracker(
        enabled=NAVIGATION_CLEANUP_ENABLED,
        retention=timedelta(hours=NAVIGATION_RETENTION_HOURS),
    )
    bot = TrackingExtBot(BOT_TOKEN, message_tracker=message_tracker)

    async def post_init(application: Application) -> None:
        message_tracker.bind(application.bot_data)
        try:
            removed = await reconcile_configured_servers(SERVERS)
        except Exception:
            logger.exception(
                "Could not reconcile monitoring cache with configured servers",
                extra={"action": "monitoring_cache_reconcile_failed"},
            )
            raise
        else:
            total_removed = sum(removed.values())
            if total_removed:
                logger.info(
                    "Removed %s stale monitoring cache records: %s",
                    total_removed,
                    removed,
                    extra={"action": "monitoring_cache_reconciled", "total": total_removed},
                )
        if not NAVIGATION_CLEANUP_ENABLED:
            return
        try:
            stats = await message_tracker.cleanup(application.bot, startup=True)
        except Exception:
            logger.exception(
                "Navigation cleanup startup check failed",
                extra={"action": "message_cleanup_failed"},
            )
        else:
            _log_message_cleanup(stats, reason="startup")

    async def message_cleanup_job(context) -> None:
        try:
            stats = await message_tracker.cleanup(context.bot)
        except Exception:
            logger.exception(
                "Scheduled navigation cleanup failed",
                extra={"action": "message_cleanup_failed"},
            )
        else:
            _log_message_cleanup(stats, reason="scheduled")

    application: Application = (
        ApplicationBuilder()
        .bot(bot)
        .job_queue(ManagedJobQueue())
        .persistence(build_sqlite_persistence(Path(DATA_DIR) / DATABASE_FILENAME))
        .post_init(post_init)
        .post_shutdown(_post_shutdown)
        .build()
    )

    # Exact easter eggs precede conversations so an active text-input state
    # cannot reinterpret the trigger as business data.
    application.add_handler(MessageHandler(RANEPA_TEXT, ranepa_easter_egg))
    conversations = register_routes(
        application,
        server_key_pattern=SERVER_KEY_PATTERN,
    )
    register_jobs(
        application,
        bot_mode=bot_mode,
        message_cleanup_job=message_cleanup_job,
    )

    navigation_callbacks = [handler for handler in application.handlers[0] if isinstance(handler, CallbackQueryHandler)]

    async def navigation_preprocessor(update: Update, context) -> None:
        try:
            await retire_review_card_for_navigation(update)
        except Exception:
            # A failed bookkeeping write must not make Telegram navigation unusable.
            logger.exception(
                "Could not retire review-card reference before navigation",
                extra={"action": "review_card_retire_failed"},
            )
        await reset_navigation_state(
            update,
            context,
            conversations,
            navigation_callbacks,
            abandon_flow=abandon_product_flow,
        )

    # Blocked accounts are stopped before tracking, navigation and every command.
    application.add_handler(TypeHandler(Update, blocked_user_guard), group=-100)

    # Stale-flow cleanup runs before the regular group-zero handlers.
    application.add_handler(TypeHandler(Update, navigation_preprocessor), group=-1)

    # A catch-all callback acknowledges obsolete buttons; the text fallback stays
    # last in group zero so active conversations retain exclusive message handling.
    application.add_handler(CallbackQueryHandler(unhandled_callback))
    application.add_handler(MessageHandler(PRIVATE_TEXT, fallback_text))
    application.add_error_handler(on_error)
    return application


def run_application(*, instance_lock: SingleInstanceLock | None = None, bot_mode: str = BOT_MODE) -> None:
    """Build and poll while respecting the process-wide single-instance lock."""
    lock = instance_lock or SingleInstanceLock(INSTANCE_LOCK_PATH)
    owns_lock = instance_lock is None
    if owns_lock:
        try:
            lock.acquire()
        except InstanceAlreadyRunning as exc:
            logger.warning("MaintBot не запущен: %s", exc)
            raise SystemExit(ALREADY_RUNNING_EXIT_CODE) from exc
    try:
        application = build_application(bot_mode=bot_mode)
        logger.info("Bot started", extra={"action": "startup"})
        application.run_polling(drop_pending_updates=False)
    finally:
        if owns_lock:
            lock.release()
