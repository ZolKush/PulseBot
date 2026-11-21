"""Display, freshness and role boundaries for the Telegram status button."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app import storage
from app.monitoring.status import handlers, views
from app.monitoring.zabbix import Host, Metric, Problem, ZabbixError
from tests.product_support import _admin, _callback_names, _callback_update, _user

HOST = Host("42", "Узел 1")


async def _seed() -> None:
    def seed(config: storage.UserData) -> None:
        config.authorized_users = {"1": _admin(1), "2": _user(2, service_tier="subscriber")}

    await storage.update_user_data(seed)


@pytest.fixture
def api(monkeypatch):
    client = SimpleNamespace(hosts=AsyncMock(return_value=(HOST,)), problems=AsyncMock(), metrics=AsyncMock())
    monkeypatch.setattr(handlers, "ZabbixClient", Mock(return_value=client))
    return client


@pytest.mark.asyncio
async def test_subscriber_refreshes_public_status_through_resolution(isolated_storage, api):
    await _seed()
    update, context = _callback_update(2, "menu:status")
    issue = Problem("42", "12345", 4, True, cause_eventid="12300", name="Private trigger", comment="Private comment")
    api.problems.side_effect = [(), (issue,), ()]
    for expected in (views.HEALTHY, "Высокая важность", views.HEALTHY):
        await handlers.cmd_health(update, context)
        call = update.callback_query.edit_message_text.call_args
        text = call.args[0]
        if expected == views.HEALTHY:
            assert text == expected
        else:
            assert expected in text and HOST.name in text and "подтверждена" in text
            assert "Private" not in text and issue.event_id not in text
            assert "Связана с другой проблемой." in text and issue.cause_eventid not in text
        assert _callback_names(call.kwargs["reply_markup"]) == {"status:issues:0", "menu:home"}
    api.problems.assert_awaited_with((HOST,), staff=False)
    api.metrics.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", [(), ZabbixError("private diagnostic")])
async def test_unknown_inventory_never_reports_healthy(isolated_storage, api, outcome):
    await _seed()
    update, context = _callback_update(2, "menu:status")
    api.hosts.side_effect = [outcome]
    await handlers.cmd_health(update, context)
    assert update.callback_query.edit_message_text.call_args.args == (views.UNAVAILABLE,)
    api.problems.assert_not_awaited()


@pytest.mark.asyncio
async def test_subscriber_cannot_query_staff_metrics_or_inventory(isolated_storage, api):
    await _seed()
    for callback, data in (
        (handlers.status_hosts_cb, "status:hosts:0"),
        (handlers.status_metrics_cb, "status:metrics:42:0"),
    ):
        update, context = _callback_update(2, data)
        await callback(update, context)
    api.hosts.assert_not_awaited()
    api.metrics.assert_not_awaited()


@pytest.mark.asyncio
async def test_staff_views_use_live_inventory_and_selected_host(isolated_storage, api):
    await _seed()
    update, context = _callback_update(1, "menu:status")
    await handlers.cmd_health(update, context)
    call = update.callback_query.edit_message_text.call_args
    assert "Узлы Zabbix · 1" in call.args[0]
    assert {"status:issues:0", "status:metrics:42:0"} <= _callback_names(call.kwargs["reply_markup"])
    api.metrics.assert_not_awaited()

    update.callback_query.data = "status:metrics:42:0"
    api.metrics.return_value = (Metric("system.cpu.util", "CPU", "%", "17", 1700000000),)
    await handlers.status_metrics_cb(update, context)
    text = update.callback_query.edit_message_text.call_args.args[0]
    assert "system.cpu.util" in text and "17 %" in text and "2023" in text
    api.metrics.assert_awaited_once_with("42")

    update.callback_query.data = "status:issues:0"
    api.problems.return_value = (Problem("42", "11", 0, False, True, "10", "Триггер", "Принято"),)
    await handlers.status_issues_cb(update, context)
    text = update.callback_query.edit_message_text.call_args.args[0]
    assert all(value in text for value in ("Триггер", "Принято", "Подавлена", "#10"))
    api.problems.assert_awaited_once_with((HOST,), staff=True)

    update.callback_query.data = "status:metrics:43:0"
    await handlers.status_metrics_cb(update, context)
    assert "больше недоступен" in update.callback_query.edit_message_text.call_args.args[0]
    api.metrics.assert_awaited_once()


def test_metric_pages_keep_missing_and_zero_distinct_and_clamp_stale_pages():
    metrics = tuple(Metric(f"net.if.in[eth{i}]", "Сеть", "bps", "0", 1700000000) for i in range(5))
    metrics += (Metric("docker.ping", "Docker", "", None, 0),)
    first, markup = views.metric_list(HOST, metrics, 0)
    assert "0 bps" in first
    assert "status:metrics:42:1" in _callback_names(markup)
    last, _ = views.metric_list(HOST, metrics, 999)
    assert "docker.ping" in last and "нет данных" in last


def test_invalid_metric_timestamp_preserves_the_returned_value():
    text, _ = views.metric_list(HOST, (Metric("docker.ping", "Docker", "", "1", 10**19),), 0)
    assert "1 · время измерения неизвестно" in text
