"""Registration of the bot's persistent multi-step conversations."""

from __future__ import annotations

from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
)

from ..administration.flow_handlers import (
    administration_cancel,
    administration_confirm_cb,
    administration_input_start_cb,
    administration_text_input,
)
from ..administration.state import ADMINISTRATION_CONFIRM, ADMINISTRATION_INPUT
from ..maintenance.flow import maint_duration, maint_mode, maint_scope, maint_scope_page, maint_start, maint_urgency
from ..maintenance.lifecycle import maint_extend_cb, maint_extend_duration
from ..maintenance.policy import SERVER_KEY_PATTERN
from ..maintenance.scheduling import maint_cal_day, maint_cal_nav, maint_cal_noop, maint_schedule_range
from ..maintenance.state import (
    STATE_MAINT_DURATION,
    STATE_MAINT_EXTEND,
    STATE_MAINT_MODE,
    STATE_MAINT_SCHEDULE_DATE,
    STATE_MAINT_SCHEDULE_RANGE,
    STATE_MAINT_SCOPE,
    STATE_MAINT_URGENCY,
)
from ..subscriptions.requests.confirmation import product_confirm_cb
from ..subscriptions.requests.customer import trial_request_start_cb
from ..subscriptions.requests.flow_cleanup import product_cancel
from ..subscriptions.requests.input_processing import product_text_input
from ..subscriptions.requests.input_start import product_input_start_cb
from ..subscriptions.requests.review_handlers import product_request_action_cb
from ..subscriptions.requests.state import PRODUCT_CONFIRM, PRODUCT_INPUT
from ..subscriptions.requests.terms_flow import terms_input_start_cb
from ..tickets.dashboard_handlers import (
    ticket_archive_cb,
    ticket_archive_page_cb,
    ticket_list_cb,
    ticket_open_cb,
)
from ..tickets.reply_handlers import (
    ticket_admin_reply_start,
    ticket_admin_reply_text,
    ticket_user_reply_start,
    ticket_user_reply_text,
)
from ..tickets.routes import (
    TICKET_ADMIN_REPLY_TEXT,
    TICKET_CONFIRM,
    TICKET_SUBJECT,
    TICKET_TEXT,
    TICKET_URGENCY,
    TICKET_USER_REPLY_TEXT,
)
from ..tickets.transfer_handlers import ticket_transfer_init_cb, ticket_transfer_to_cb
from ..tickets.user_handlers import (
    ticket_confirm,
    ticket_start,
    ticket_subject,
    ticket_text,
    ticket_urgency,
)
from ..users.admin import (
    users_all_menu,
    users_all_msg_confirm,
    users_all_msg_text,
    users_entry,
    users_pick,
    users_user_cfg_text,
    users_user_menu,
    users_user_msg_text,
    users_user_nick_text,
)
from ..users.profile_handlers import (
    PROFILE_EMAIL_INPUT,
    profile_cancel,
    profile_email_start_cb,
    profile_email_text,
)
from ..users.states import (
    ADMIN_ALL_MENU,
    ADMIN_ALL_MSG_CONFIRM,
    ADMIN_ALL_MSG_TEXT,
    ADMIN_PICK,
    ADMIN_USER_CFG_TEXT,
    ADMIN_USER_MENU,
    ADMIN_USER_MSG_TEXT,
    ADMIN_USER_NICK_TEXT,
)
from .conversations import NavigableConversationHandler, conversation_handler
from .navigation import cancel, cancel_to_menu_cb

PRIVATE_TEXT = filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND
PRIVATE_TICKET_INPUT = (
    filters.ChatType.PRIVATE & ~filters.COMMAND & (filters.TEXT | filters.PHOTO | filters.Document.ALL)
)


def build_administration_flow() -> NavigableConversationHandler:
    return conversation_handler(
        entry_points=[
            CallbackQueryHandler(
                administration_input_start_cb,
                pattern=(
                    r"^(administration:input:(alias|help|support_email|payment_message|standard_price|"
                    r"period_current|period_next)|staff:alias|"
                    r"product:input:setting_(payment|current|next))$"
                ),
            ),
        ],
        states={
            ADMINISTRATION_INPUT: [MessageHandler(PRIVATE_TEXT, administration_text_input)],
            ADMINISTRATION_CONFIRM: [
                CallbackQueryHandler(administration_confirm_cb, pattern=r"^administration:confirm$")
            ],
        },
        fallbacks=[
            CommandHandler("cancel", administration_cancel),
            CallbackQueryHandler(administration_cancel, pattern=r"^administration:cancel$"),
            CallbackQueryHandler(cancel_to_menu_cb, pattern=r"^menu:home$"),
        ],
        name="administration_flow",
        persistent=True,
    )


def build_profile_flow() -> NavigableConversationHandler:
    return conversation_handler(
        entry_points=[CallbackQueryHandler(profile_email_start_cb, pattern=r"^profile:email:edit$")],
        states={PROFILE_EMAIL_INPUT: [MessageHandler(PRIVATE_TEXT, profile_email_text)]},
        fallbacks=[
            CommandHandler("cancel", profile_cancel),
            CallbackQueryHandler(profile_cancel, pattern=r"^profile:show$"),
            CallbackQueryHandler(cancel_to_menu_cb, pattern=r"^menu:home$"),
        ],
        name="profile_flow",
        persistent=True,
    )


def build_product_flow() -> NavigableConversationHandler:
    return conversation_handler(
        entry_points=[
            CallbackQueryHandler(trial_request_start_cb, pattern=r"^subscription:trial$"),
            CallbackQueryHandler(terms_input_start_cb, pattern=r"^product:input:terms:\d+$"),
            CallbackQueryHandler(
                product_request_action_cb,
                pattern=r"^product:req:(approve|approve24|custom|reject|requisites|confirm|notfound):\d+$",
            ),
            CallbackQueryHandler(
                product_input_start_cb,
                pattern=r"^product:input:(massdate|massremind|user_end:\d+|manualpay:\d+)$",
            ),
        ],
        states={
            PRODUCT_INPUT: [MessageHandler(PRIVATE_TEXT, product_text_input)],
            PRODUCT_CONFIRM: [CallbackQueryHandler(product_confirm_cb, pattern=r"^product:confirm:apply$")],
        },
        fallbacks=[
            CommandHandler("cancel", product_cancel),
            CallbackQueryHandler(product_cancel, pattern=r"^(product:cancel|menu:home)$"),
        ],
        name="product_flow",
        persistent=True,
    )


def build_maintenance_flow() -> NavigableConversationHandler:
    return conversation_handler(
        entry_points=[
            CommandHandler("maint", maint_start),
            CallbackQueryHandler(maint_start, pattern=r"^menu:maint$"),
            CallbackQueryHandler(maint_extend_cb, pattern=r"^maint:extend:[0-9a-f]+$"),
        ],
        states={
            STATE_MAINT_MODE: [CallbackQueryHandler(maint_mode, pattern=r"^maint:mode:(announce|schedule)$")],
            STATE_MAINT_SCOPE: [
                CallbackQueryHandler(maint_scope, pattern=rf"^maint:scope:(?:all|{SERVER_KEY_PATTERN})$"),
                CallbackQueryHandler(maint_scope_page, pattern=r"^maint:scope:page:[0-9]{1,9}$"),
            ],
            STATE_MAINT_URGENCY: [CallbackQueryHandler(maint_urgency, pattern=r"^maint:urgency:(urgent|planned)$")],
            STATE_MAINT_DURATION: [MessageHandler(PRIVATE_TEXT, maint_duration)],
            STATE_MAINT_EXTEND: [MessageHandler(PRIVATE_TEXT, maint_extend_duration)],
            STATE_MAINT_SCHEDULE_RANGE: [MessageHandler(PRIVATE_TEXT, maint_schedule_range)],
            STATE_MAINT_SCHEDULE_DATE: [
                CallbackQueryHandler(maint_cal_nav, pattern=r"^maint:cal:nav:\d{4}-\d{2}$"),
                CallbackQueryHandler(maint_cal_day, pattern=r"^maint:cal:day:\d{4}-\d{2}-\d{2}$"),
                CallbackQueryHandler(maint_cal_noop, pattern=r"^maint:cal:noop$"),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            CallbackQueryHandler(cancel_to_menu_cb, pattern=r"^menu:home$"),
        ],
        name="maint_flow",
        persistent=True,
    )


def _ticket_persistent_entry_points() -> list[CallbackQueryHandler]:
    return [
        CallbackQueryHandler(ticket_list_cb, pattern=r"^ticket:list(?::\d+)?$"),
        CallbackQueryHandler(ticket_open_cb, pattern=r"^ticket:open:\d+$"),
        CallbackQueryHandler(ticket_archive_cb, pattern=r"^ticket:archive$"),
        CallbackQueryHandler(ticket_archive_page_cb, pattern=r"^ticket:archive_page:\d+$"),
        CallbackQueryHandler(ticket_transfer_init_cb, pattern=r"^ticket:transfer_init:\d+$"),
        CallbackQueryHandler(ticket_transfer_to_cb, pattern=r"^ticket:transfer_to:\d+:\d+$"),
    ]


def build_ticket_flow() -> NavigableConversationHandler:
    persistent_entry_points = _ticket_persistent_entry_points()
    return conversation_handler(
        entry_points=[
            CommandHandler("ticket", ticket_start),
            CallbackQueryHandler(ticket_start, pattern=r"^menu:ticket$"),
            CallbackQueryHandler(ticket_admin_reply_start, pattern=r"^ticket:adminreply:\d+$"),
            CallbackQueryHandler(ticket_user_reply_start, pattern=r"^ticket:userreply:\d+$"),
            *persistent_entry_points,
        ],
        states={
            TICKET_SUBJECT: [MessageHandler(PRIVATE_TEXT, ticket_subject)],
            TICKET_URGENCY: [CallbackQueryHandler(ticket_urgency, pattern=r"^ticket:(p1|p2|p3)$")],
            TICKET_TEXT: [MessageHandler(PRIVATE_TICKET_INPUT, ticket_text)],
            TICKET_CONFIRM: [
                CallbackQueryHandler(ticket_confirm, pattern=r"^ticket:(send|edit_subj|edit_text|cancel)$")
            ],
            TICKET_USER_REPLY_TEXT: [MessageHandler(PRIVATE_TICKET_INPUT, ticket_user_reply_text)],
            TICKET_ADMIN_REPLY_TEXT: [MessageHandler(PRIVATE_TICKET_INPUT, ticket_admin_reply_text)],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            CallbackQueryHandler(cancel_to_menu_cb, pattern=r"^menu:home$"),
            *persistent_entry_points,
        ],
        name="ticket_flow",
        persistent=True,
    )


def build_users_flow() -> NavigableConversationHandler:
    return conversation_handler(
        entry_points=[
            CommandHandler("users", users_entry),
            CallbackQueryHandler(users_entry, pattern=r"^menu:users$"),
            CallbackQueryHandler(users_pick, pattern=r"^users:user:\d+$"),
        ],
        states={
            ADMIN_PICK: [
                CallbackQueryHandler(
                    users_pick,
                    pattern=r"^users:(all|main|back|filter:(all|active|disabled|unpaid|admins|blocked)|"
                    r"user:\d+|page:\d+)$",
                ),
            ],
            ADMIN_ALL_MENU: [CallbackQueryHandler(users_all_menu, pattern=r"^users:(allmsg:(all|admins)|back)$")],
            ADMIN_ALL_MSG_TEXT: [MessageHandler(PRIVATE_TEXT, users_all_msg_text)],
            ADMIN_ALL_MSG_CONFIRM: [CallbackQueryHandler(users_all_msg_confirm, pattern=r"^users:(allsend|all|back)$")],
            ADMIN_USER_MENU: [
                CallbackQueryHandler(
                    users_user_menu,
                    pattern=(
                        r"^users:(msg:\d+|nick:\d+|cfg:\d+|subassign:\d+|subsend:\d+|subview:\d+|"
                        r"toggle:\d+|toggleapply:\d+|access:(approve|block):\d+|"
                        r"accessapply:(approve|block):\d+|back)$"
                    ),
                ),
                CallbackQueryHandler(users_pick, pattern=r"^users:user:\d+$"),
            ],
            ADMIN_USER_MSG_TEXT: [MessageHandler(PRIVATE_TEXT, users_user_msg_text)],
            ADMIN_USER_NICK_TEXT: [MessageHandler(PRIVATE_TEXT, users_user_nick_text)],
            ADMIN_USER_CFG_TEXT: [
                MessageHandler(PRIVATE_TEXT, users_user_cfg_text),
                CallbackQueryHandler(users_pick, pattern=r"^users:user:\d+$"),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            CallbackQueryHandler(cancel_to_menu_cb, pattern=r"^menu:home$"),
        ],
        name="users_flow",
        persistent=True,
    )
