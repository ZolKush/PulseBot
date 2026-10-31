"""Characterization tests for MaintBot's externally visible PTB routing contract."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger
from telegram.ext import CallbackQueryHandler, CommandHandler, ConversationHandler, MessageHandler, TypeHandler

import app.main as main_module
from app.messaging.message_cleanup import TrackingExtBot
from tests.routing_contract_data import CONVERSATION_ROUTES

DEFAULT_ROUTES = (
    "message",
    "command:start",
    "command:menu",
    "command:help",
    "command:auth",
    "command:login",
    "command:logout",
    "command:owner",
    r"callback:^access:request$",
    r"callback:^access:(approve|reject|block):\d+$",
    "command:health",
    "command:subscription",
    "conversation:administration_flow",
    r"callback:^(administration:show|staff:profile)$",
    r"callback:^(administration:signature|staff:mode):(title|title_alias)$",
    r"callback:^(administration:settings|product:owner)$",
    r"callback:^(administration:title|product:titlemenu):\d+$",
    r"callback:^(administration:title|product:title):\d+:[a-z_]+$",
    "conversation:profile_flow",
    r"callback:^(profile:show|product:profile)$",
    r"callback:^profile:email:clear$",
    "conversation:product_flow",
    r"callback:^subscription:buy$",
    r"callback:^subscription:buyconfirm$",
    r"callback:^subscription:paid:\d+$",
    r"callback:^subscription:renew$",
    r"callback:^subscription:connection$",
    r"callback:^product:requests(?::(active|archive):\d+)?$",
    r"callback:^product:req:view:\d+(?::archive:\d+)?$",
    r"callback:^product:manage:\d+$",
    r"callback:^product:tier:\d+:(basic|unlimited_trial)$",
    r"callback:^product:remind:\d+$",
    "conversation:maint_flow",
    r"callback:^maint:endconfirm:[0-9a-f]+$",
    r"callback:^maint:cancelend:[0-9a-f]+$",
    r"callback:^maint:end:[0-9a-f]+$",
    r"callback:^maint:schedcancelconfirm:[0-9a-f]+$",
    r"callback:^maint:schedcancelback:[0-9a-f]+$",
    r"callback:^maint:schedcancel:[0-9a-f]+$",
    "conversation:ticket_flow",
    r"callback:^ticket:take:\d+$",
    r"callback:^ticket:close:\d+$",
    "conversation:users_flow",
    r"callback:^users:export:xlsx$",
    "command:cancel",
    r"callback:^menu:home$",
    r"callback:^menu:help$",
    r"callback:^menu:status$",
    r"callback:^menu:subscription$",
    r"callback:^status:pick$",
    r"callback:^status:show:[a-z0-9_-]{1,12}$",
    r"callback:^status:refresh:[a-z0-9_-]{1,12}$",
    r"callback:^status:ufw:[a-z0-9_-]{1,12}$",
    r"callback:^status:dnsrefresh:[a-z0-9_-]{1,12}$",
    r"callback:^status:tlsrefresh:[a-z0-9_-]{1,12}$",
    r"callback:^tls:list:[a-z0-9_-]{1,12}$",
    r"callback:^dns:back:[a-z0-9_-]{1,12}$",
    r"callback:^docker:list:[a-z0-9_-]{1,12}$",
    r"callback:^docker:back:[a-z0-9_-]{1,12}$",
    r"callback:^docker:show:[a-z0-9_-]{1,12}:[a-zA-Z0-9_.\-]{1,64}$",
    r"callback:^docker:inspect:[a-z0-9_-]{1,12}:[a-zA-Z0-9_.\-]{1,64}$",
    r"callback:^docker:logs:[a-z0-9_-]{1,12}:[a-zA-Z0-9_.\-]{1,64}:\d{1,4}$",
    "command:fail2ban",
    r"callback:^f2b:menu:[a-z0-9_-]{1,12}$",
    r"callback:^f2b:tail:[a-z0-9_-]{1,12}:\d{1,5}$",
    r"callback:^f2b:digest:[a-z0-9_-]{1,12}$",
    r"callback:^f2b:back:[a-z0-9_-]{1,12}$",
    "callback:*",
    "message",
)

NON_BLOCKING_ROUTES = (
    "command:health",
    r"callback:^menu:status$",
    r"callback:^status:pick$",
    r"callback:^status:show:[a-z0-9_-]{1,12}$",
    r"callback:^status:refresh:[a-z0-9_-]{1,12}$",
    r"callback:^status:ufw:[a-z0-9_-]{1,12}$",
    r"callback:^status:dnsrefresh:[a-z0-9_-]{1,12}$",
    r"callback:^status:tlsrefresh:[a-z0-9_-]{1,12}$",
    r"callback:^tls:list:[a-z0-9_-]{1,12}$",
    r"callback:^dns:back:[a-z0-9_-]{1,12}$",
    r"callback:^docker:list:[a-z0-9_-]{1,12}$",
    r"callback:^docker:back:[a-z0-9_-]{1,12}$",
    r"callback:^docker:show:[a-z0-9_-]{1,12}:[a-zA-Z0-9_.\-]{1,64}$",
    r"callback:^docker:inspect:[a-z0-9_-]{1,12}:[a-zA-Z0-9_.\-]{1,64}$",
    r"callback:^docker:logs:[a-z0-9_-]{1,12}:[a-zA-Z0-9_.\-]{1,64}:\d{1,4}$",
    "command:fail2ban",
    r"callback:^f2b:menu:[a-z0-9_-]{1,12}$",
    r"callback:^f2b:tail:[a-z0-9_-]{1,12}:\d{1,5}$",
    r"callback:^f2b:digest:[a-z0-9_-]{1,12}$",
    r"callback:^f2b:back:[a-z0-9_-]{1,12}$",
)

DEFAULT_JOB_NAMES = (
    "fail2ban_digest",
    "dns_daily_refresh",
    "dns_refresh_startup",
    "maint_active_reminder",
    "maint_schedule_tick",
    "auth_prune",
    "outbox_delivery",
    "ticket_orphan_release",
    "subscription_lifecycle",
    "docker_status_refresh",
    "tls_certificate_check_startup",
    "tls_certificate_check",
    "tls_deadline_evaluation",
    "message_cleanup",
)


def _pattern(handler: CallbackQueryHandler) -> str:
    if handler.pattern is None:
        return "*"
    return str(getattr(handler.pattern, "pattern", handler.pattern))


def _route_key(handler: object) -> str:
    if isinstance(handler, CommandHandler):
        return "command:" + ",".join(sorted(handler.commands))
    if isinstance(handler, CallbackQueryHandler):
        return "callback:" + _pattern(handler)
    if isinstance(handler, ConversationHandler):
        return f"conversation:{handler.name}"
    if isinstance(handler, MessageHandler):
        return "message"
    if isinstance(handler, TypeHandler):
        return "type"
    return type(handler).__name__


def _effective_block(handler: Any) -> bool:
    value = handler.block
    return bool(value if isinstance(value, bool) else value.value)


def _cron_fields(trigger: CronTrigger) -> dict[str, str]:
    return {field.name: str(field) for field in trigger.fields}


def test_default_handler_groups_and_route_order_are_stable() -> None:
    application = main_module.build_app()

    assert isinstance(application.bot, TrackingExtBot)
    assert application.post_init is not None
    assert tuple(sorted(application.handlers)) == (-100, -1, 0)
    assert all(len(application.handlers[group]) == 1 for group in (-100, -1))
    assert all(isinstance(application.handlers[group][0], TypeHandler) for group in (-100, -1))

    routes = tuple(_route_key(handler) for handler in application.handlers[0])
    assert routes == DEFAULT_ROUTES
    assert (
        tuple(_route_key(handler) for handler in application.handlers[0] if not _effective_block(handler))
        == NON_BLOCKING_ROUTES
    )


def test_persistent_conversation_contract_is_stable() -> None:
    application = main_module.build_app()
    conversations = {
        handler.name: handler for handler in application.handlers[0] if isinstance(handler, ConversationHandler)
    }

    assert tuple(conversations) == tuple(CONVERSATION_ROUTES)
    for name, expected in CONVERSATION_ROUTES.items():
        conversation = conversations[name]
        assert conversation.persistent is True
        assert (conversation.per_chat, conversation.per_user, conversation.per_message) == (True, True, False)
        assert tuple(conversation.states) == expected["states"]
        assert tuple(_route_key(handler) for handler in conversation.entry_points) == expected["entry_points"]
        assert {
            state: tuple(_route_key(handler) for handler in handlers) for state, handlers in conversation.states.items()
        } == expected["state_routes"]
        assert tuple(_route_key(handler) for handler in conversation.fallbacks) == expected["fallbacks"]


def test_default_job_names_and_schedules_are_stable() -> None:
    before = datetime.now(timezone.utc)
    application = main_module.build_app()
    after = datetime.now(timezone.utc)
    assert application.job_queue is not None
    jobs = {job.name: job for job in application.job_queue.jobs()}

    assert tuple(jobs) == DEFAULT_JOB_NAMES

    intervals = {
        "maint_active_reminder": 1800,
        "maint_schedule_tick": 60,
        "auth_prune": 300,
        "outbox_delivery": 10,
        "ticket_orphan_release": 60,
        "subscription_lifecycle": 60,
        "docker_status_refresh": 21600,
        "tls_certificate_check": 604800,
        "tls_deadline_evaluation": 86400,
        "message_cleanup": 1800,
    }
    for name, seconds in intervals.items():
        trigger = jobs[name].job.trigger
        assert isinstance(trigger, IntervalTrigger)
        assert trigger.interval.total_seconds() == seconds

    daily = {
        "fail2ban_digest": ("12", "0"),
        "dns_daily_refresh": ("3", "5"),
    }
    for name, (hour, minute) in daily.items():
        trigger = jobs[name].job.trigger
        assert isinstance(trigger, CronTrigger)
        fields = _cron_fields(trigger)
        assert (fields["hour"], fields["minute"], fields["second"]) == (hour, minute, "0")
        assert fields["day_of_week"] == "sun,mon,tue,wed,thu,fri,sat"
        assert str(trigger.timezone) == "Europe/Moscow"

    startup = jobs["dns_refresh_startup"].job.trigger
    assert isinstance(startup, DateTrigger)
    assert before.timestamp() + 4 <= startup.run_date.timestamp() <= after.timestamp() + 6

    tls_startup = jobs["tls_certificate_check_startup"].job.trigger
    assert isinstance(tls_startup, DateTrigger)
    assert before.timestamp() + 9 <= tls_startup.run_date.timestamp() <= after.timestamp() + 11


def test_mixed_mode_keeps_routes_and_adds_node_status_jobs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(main_module, "BOT_MODE", "mixed")
    before = datetime.now(timezone.utc)
    application = main_module.build_app()
    after = datetime.now(timezone.utc)

    routes = tuple(_route_key(handler) for handler in application.handlers[0])
    assert routes == DEFAULT_ROUTES
    assert not any("status:ssh" in route for route in routes)

    assert application.job_queue is not None
    jobs = {job.name: job for job in application.job_queue.jobs()}
    assert tuple(jobs) == (
        "fail2ban_digest",
        "dns_daily_refresh",
        "dns_refresh_startup",
        "daily_node_status_refresh",
        "daily_node_status_startup",
        *DEFAULT_JOB_NAMES[3:],
    )

    daily = jobs["daily_node_status_refresh"].job.trigger
    assert isinstance(daily, CronTrigger)
    fields = _cron_fields(daily)
    assert (fields["hour"], fields["minute"], fields["second"]) == ("12", "0", "0")
    assert str(daily.timezone) == "Europe/Moscow"

    startup = jobs["daily_node_status_startup"].job.trigger
    assert isinstance(startup, DateTrigger)
    assert before.timestamp() + 9 <= startup.run_date.timestamp() <= after.timestamp() + 11
