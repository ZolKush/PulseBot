"""PTB route composition in the externally visible registration order."""

from __future__ import annotations

from telegram.ext import Application, CallbackQueryHandler, CommandHandler

from ..access.commands import cmd_auth, cmd_help, cmd_logout, cmd_owner, cmd_start
from ..access.request_handlers import access_request_cb, access_review_cb
from ..administration.profile_handlers import (
    administration_show_cb,
    administration_signature_mode_cb,
    administration_staff_title_apply_cb,
    administration_staff_title_menu_cb,
)
from ..administration.settings_handlers import administration_service_settings_cb
from ..maintenance.lifecycle import (
    maint_cancel_end_cb,
    maint_end_cb,
    maint_end_confirm_cb,
)
from ..maintenance.scheduling import (
    maint_sched_cancel_back_cb,
    maint_sched_cancel_cb,
    maint_sched_cancel_confirm_cb,
)
from ..monitoring.status.handlers import cmd_health
from ..subscriptions.connections import connection_show_cb, subscription_show
from ..subscriptions.requests.admin_listing import (
    product_request_view_cb,
    product_requests_cb,
)
from ..subscriptions.requests.customer import purchase_create_cb, purchase_show_cb
from ..subscriptions.requests.management import (
    product_manage_user_cb,
    product_tier_cb,
)
from ..subscriptions.requests.payment_reports import (
    payment_reported_cb,
    renewal_reported_cb,
)
from ..subscriptions.requests.reminders import product_manual_reminder_cb
from ..tickets.admin_handlers import ticket_close_cb, ticket_take_cb
from ..users.admin import export_clients_xlsx_cb
from ..users.profile_handlers import personal_profile_cb, profile_email_clear_cb
from .conversations import NavigableConversationHandler
from .flow_routes import (
    build_administration_flow,
    build_maintenance_flow,
    build_product_flow,
    build_profile_flow,
    build_ticket_flow,
    build_users_flow,
)
from .menu import menu_home_cb
from .monitoring_routes import register_monitoring_routes
from .navigation import cancel


def _register_access_routes(application: Application) -> None:
    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("menu", cmd_start))
    application.add_handler(CommandHandler("help", cmd_help))
    application.add_handler(CommandHandler("auth", cmd_auth))
    application.add_handler(CommandHandler("login", cmd_auth))
    application.add_handler(CommandHandler("logout", cmd_logout))
    application.add_handler(CommandHandler("owner", cmd_owner))
    application.add_handler(CallbackQueryHandler(access_request_cb, pattern=r"^access:request$"))
    application.add_handler(CallbackQueryHandler(access_review_cb, pattern=r"^access:(approve|reject|block):\d+$"))
    application.add_handler(CommandHandler("health", cmd_health, block=False))
    application.add_handler(CommandHandler("subscription", subscription_show))


def _register_administration_routes(
    application: Application,
    conversations: list[NavigableConversationHandler],
) -> None:
    flow = build_administration_flow()
    conversations.append(flow)
    application.add_handler(flow)
    application.add_handler(
        CallbackQueryHandler(administration_show_cb, pattern=r"^(administration:show|staff:profile)$")
    )
    application.add_handler(
        CallbackQueryHandler(
            administration_signature_mode_cb,
            pattern=r"^(administration:signature|staff:mode):(title|title_alias)$",
        )
    )
    application.add_handler(
        CallbackQueryHandler(
            administration_service_settings_cb,
            pattern=r"^(administration:settings|product:owner)$",
        )
    )
    application.add_handler(
        CallbackQueryHandler(
            administration_staff_title_menu_cb,
            pattern=r"^(administration:title|product:titlemenu):\d+$",
        )
    )
    application.add_handler(
        CallbackQueryHandler(
            administration_staff_title_apply_cb,
            pattern=r"^(administration:title|product:title):\d+:[a-z_]+$",
        )
    )


def _register_profile_routes(
    application: Application,
    conversations: list[NavigableConversationHandler],
) -> None:
    flow = build_profile_flow()
    conversations.append(flow)
    application.add_handler(flow)
    application.add_handler(CallbackQueryHandler(personal_profile_cb, pattern=r"^(profile:show|product:profile)$"))
    application.add_handler(CallbackQueryHandler(profile_email_clear_cb, pattern=r"^profile:email:clear$"))


def _register_product_routes(
    application: Application,
    conversations: list[NavigableConversationHandler],
) -> None:
    flow = build_product_flow()
    conversations.append(flow)
    application.add_handler(flow)
    application.add_handler(CallbackQueryHandler(purchase_show_cb, pattern=r"^subscription:buy$"))
    application.add_handler(CallbackQueryHandler(purchase_create_cb, pattern=r"^subscription:buyconfirm$"))
    application.add_handler(CallbackQueryHandler(payment_reported_cb, pattern=r"^subscription:paid:\d+$"))
    application.add_handler(CallbackQueryHandler(renewal_reported_cb, pattern=r"^subscription:renew$"))
    application.add_handler(CallbackQueryHandler(connection_show_cb, pattern=r"^subscription:connection$"))
    application.add_handler(
        CallbackQueryHandler(product_requests_cb, pattern=r"^product:requests(?::(active|archive):\d+)?$")
    )
    application.add_handler(
        CallbackQueryHandler(product_request_view_cb, pattern=r"^product:req:view:\d+(?::archive:\d+)?$")
    )
    application.add_handler(CallbackQueryHandler(product_manage_user_cb, pattern=r"^product:manage:\d+$"))
    application.add_handler(
        CallbackQueryHandler(product_tier_cb, pattern=r"^product:tier:\d+:(basic|unlimited_trial)$")
    )
    application.add_handler(CallbackQueryHandler(product_manual_reminder_cb, pattern=r"^product:remind:\d+$"))


def _register_maintenance_routes(
    application: Application,
    conversations: list[NavigableConversationHandler],
    *,
    server_key_pattern: str,
) -> None:
    flow = build_maintenance_flow(server_key_pattern)
    conversations.append(flow)
    application.add_handler(flow)
    application.add_handler(CallbackQueryHandler(maint_end_confirm_cb, pattern=r"^maint:endconfirm:[0-9a-f]+$"))
    application.add_handler(CallbackQueryHandler(maint_cancel_end_cb, pattern=r"^maint:cancelend:[0-9a-f]+$"))
    application.add_handler(CallbackQueryHandler(maint_end_cb, pattern=r"^maint:end:[0-9a-f]+$"))
    application.add_handler(
        CallbackQueryHandler(
            maint_sched_cancel_confirm_cb,
            pattern=r"^maint:schedcancelconfirm:[0-9a-f]+$",
        )
    )
    application.add_handler(
        CallbackQueryHandler(maint_sched_cancel_back_cb, pattern=r"^maint:schedcancelback:[0-9a-f]+$")
    )
    application.add_handler(CallbackQueryHandler(maint_sched_cancel_cb, pattern=r"^maint:schedcancel:[0-9a-f]+$"))


def _register_ticket_routes(
    application: Application,
    conversations: list[NavigableConversationHandler],
) -> None:
    flow = build_ticket_flow()
    conversations.append(flow)
    application.add_handler(flow)
    application.add_handler(CallbackQueryHandler(ticket_take_cb, pattern=r"^ticket:take:\d+$"))
    application.add_handler(CallbackQueryHandler(ticket_close_cb, pattern=r"^ticket:close:\d+$"))


def _register_users_routes(
    application: Application,
    conversations: list[NavigableConversationHandler],
) -> None:
    flow = build_users_flow()
    conversations.append(flow)
    application.add_handler(flow)
    application.add_handler(CallbackQueryHandler(export_clients_xlsx_cb, pattern=r"^users:export:xlsx$"))


def _register_menu_routes(application: Application) -> None:
    application.add_handler(CommandHandler("cancel", cancel))
    application.add_handler(CallbackQueryHandler(menu_home_cb, pattern=r"^menu:home$"))
    application.add_handler(CallbackQueryHandler(cmd_help, pattern=r"^menu:help$"))
    application.add_handler(CallbackQueryHandler(cmd_health, pattern=r"^menu:status$", block=False))
    application.add_handler(CallbackQueryHandler(subscription_show, pattern=r"^menu:subscription$"))


def register_routes(
    application: Application,
    *,
    server_key_pattern: str,
) -> list[NavigableConversationHandler]:
    """Register group-zero feature routes and return persistent conversations."""
    conversations: list[NavigableConversationHandler] = []
    _register_access_routes(application)
    _register_administration_routes(application, conversations)
    _register_profile_routes(application, conversations)
    _register_product_routes(application, conversations)
    _register_maintenance_routes(
        application,
        conversations,
        server_key_pattern=server_key_pattern,
    )
    _register_ticket_routes(application, conversations)
    _register_users_routes(application, conversations)
    _register_menu_routes(application)
    register_monitoring_routes(
        application,
        server_key_pattern=server_key_pattern,
    )
    return conversations
