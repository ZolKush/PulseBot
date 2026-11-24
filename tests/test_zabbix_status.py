"""Display, freshness and role boundaries for the Telegram status button."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app import storage
from app.monitoring.status import handlers, views
from app.monitoring.zabbix import Host, Metric, Problem, ZabbixError
from tests.product_support import _admin, _callback_names, _user
from tests.product_support import _callback_update as _base_callback_update

HOST = Host("42", "Узел 1")


def _callback_update(uid, data, message_id=10):
    update, context = _base_callback_update(uid, data)
    update.effective_message.chat_id = uid
    update.effective_message.message_id = message_id
    return update, context


async def _seed() -> None:
    def seed(config: storage.UserData) -> None:
        config.authorized_users = {"1": _admin(1), "2": _user(2, service_tier="subscriber")}

    await storage.update_user_data(seed)


@pytest.fixture
def api(monkeypatch):
    client = SimpleNamespace(
        hosts=AsyncMock(return_value=(HOST,)), problems=AsyncMock(return_value=()), metrics=AsyncMock(return_value=())
    )
    monkeypatch.setattr(handlers, "ZabbixClient", Mock(return_value=client))
    return client


def rendered(update):
    call = update.callback_query.edit_message_text.call_args
    return call.args[0], call.kwargs["reply_markup"]


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
async def test_subscriber_cannot_query_staff_screens(isolated_storage, api):
    await _seed()
    for callback, data in (
        (handlers.status_hosts_cb, "status:hosts:0"),
        (handlers.status_host_cb, "status:host:42"),
        (handlers.status_host_page_cb, "status:metrics:42:0"),
        (handlers.status_host_page_cb, "status:hostissues:42:0"),
    ):
        update, context = _callback_update(2, data)
        await callback(update, context)
    api.hosts.assert_not_awaited()
    api.metrics.assert_not_awaited()
    api.problems.assert_not_awaited()


@pytest.mark.asyncio
async def test_staff_overview_counts_unique_active_events_across_all_pages(isolated_storage, api):
    await _seed()
    hosts = tuple(Host(str(i), f"Russia QA-{i}") for i in range(1, views.HOSTS_PER_PAGE + 2))
    api.hosts.return_value = hosts
    api.problems.return_value = (
        Problem("2", "300", 4, True),
        Problem(hosts[-1].id, "300", 4, True),
        Problem(hosts[-1].id, "301", 3, False),
        Problem("3", "302", 4, True, suppressed=True),
    )
    update, context = _callback_update(1, "menu:status")
    await handlers.cmd_health(update, context)
    text, markup = rendered(update)
    assert "Состояние инфраструктуры" in text and f"Узлов под мониторингом: {len(hosts)}" in text
    assert "Активных проблем: 2" in text
    assert all(line in text for line in ("🇷🇺 Russia QA-1  ✅", "Russia QA-2  ⚠️", "Russia QA-3  🔕"))
    assert [button.callback_data for button in markup.inline_keyboard[0]] == ["status:host:1", "status:host:2"]
    assert {"status:hosts:0", "status:hosts:1", "status:issues:0"} <= _callback_names(markup)
    assert f"status:host:{hosts[-1].id}" not in _callback_names(markup)
    update.callback_query.data = "status:hosts:1"
    await handlers.status_hosts_cb(update, context)
    text, markup = rendered(update)
    assert f"{hosts[-1].name}  ⚠️" in text and "Активных проблем: 2" in text
    assert {"status:hosts:0", "status:hosts:1", f"status:host:{hosts[-1].id}"} <= _callback_names(markup)
    api.problems.assert_awaited_with(hosts, staff=True)
    api.metrics.assert_not_awaited()


@pytest.mark.asyncio
async def test_staff_host_card_links_to_scoped_details_and_original_overview_page(isolated_storage, api):
    await _seed()
    api.hosts.return_value = (*tuple(Host(str(i), f"Сервер {i}") for i in range(views.HOSTS_PER_PAGE)), HOST)
    api.problems.return_value = (
        Problem(HOST.id, "11", 2, True, name="Мало памяти"),
        Problem(HOST.id, "12", 4, True, name="Задержка диска", comment="В работе"),
        Problem(HOST.id, "14", 5, True, suppressed=True, cause_eventid="12", name="Пауза"),
    )
    api.metrics.return_value = (Metric("system.cpu.util", "CPU", "%", "17", 1700000000),)
    update, context = _callback_update(1, "status:host:42")
    await handlers.status_host_cb(update, context)
    text, markup = rendered(update)
    assert all(label in text for label in (HOST.name, "Процессор", "17 %", "Задержка диска", "подтверждена"))
    assert "system.cpu.util" not in text and "Пауза" not in text
    assert "Других активных проблем: 1" in text and "Подавленных проблем: 1" in text
    assert {"status:metrics:42:0", "status:hostissues:42:0", "status:host:42", "status:hosts:1"} == _callback_names(
        markup
    )
    api.metrics.assert_awaited_once_with("42")
    api.problems.assert_awaited_once_with((HOST,), staff=True)

    update.callback_query.data = "status:metrics:42:0"
    await handlers.status_host_page_cb(update, context)
    text, markup = rendered(update)
    assert "system.cpu.util" in text and "17 %" in text and "2023" in text
    assert {"status:metrics:42:0", "status:host:42"} <= _callback_names(markup)
    api.problems.assert_awaited_once()

    update.callback_query.data = "status:hostissues:42:0"
    await handlers.status_host_page_cb(update, context)
    text, markup = rendered(update)
    assert all(value in text for value in ("Задержка диска", "В работе", "Подавлена", "#12"))
    assert {"status:hostissues:42:0", "status:host:42"} <= _callback_names(markup)
    api.problems.assert_awaited_with((HOST,), staff=True)

    update.callback_query.data = "status:metrics:43:0"
    await handlers.status_host_page_cb(update, context)
    text, markup = rendered(update)
    assert "больше недоступен" in text and "status:hosts:0" in _callback_names(markup)
    assert api.metrics.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("uid", "callback", "data"),
    [
        (1, handlers.status_hosts_cb, "status:hosts:0"),
        (1, handlers.status_host_cb, "status:host:42"),
        (2, handlers.status_issues_cb, "status:issues:0"),
    ],
)
async def test_failed_problem_query_displays_unavailability_and_preserves_retry(
    isolated_storage, api, uid, callback, data
):
    await _seed()
    api.problems.side_effect = ZabbixError("private diagnostic")
    update, context = _callback_update(uid, data)
    await callback(update, context)
    text, markup = rendered(update)
    assert text == views.UNAVAILABLE and data in _callback_names(markup)


@pytest.mark.asyncio
@pytest.mark.parametrize("same_panel", [True, False])
async def test_slow_host_selection_does_not_overwrite_a_newer_selection(isolated_storage, api, same_panel):
    await _seed()
    other = Host("43", "Другой узел")
    api.hosts.return_value = (HOST, other)
    first_started, release_first = asyncio.Event(), asyncio.Event()

    async def metrics(host_id):
        if host_id == HOST.id:
            first_started.set()
            await release_first.wait()
        return ()

    api.metrics.side_effect = metrics
    first, first_context = _callback_update(1, "status:host:42", message_id=10)
    second, second_context = _callback_update(1, "status:host:43", message_id=10 if same_panel else 11)
    pending = asyncio.create_task(handlers.status_host_cb(first, first_context))
    try:
        await asyncio.wait_for(first_started.wait(), 5)
        await asyncio.wait_for(handlers.status_host_cb(second, second_context), 5)
        assert other.name in rendered(second)[0]
    finally:
        release_first.set()
        await asyncio.wait_for(pending, 5)
    if same_panel:
        first.callback_query.edit_message_text.assert_not_awaited()
    else:
        assert HOST.name in rendered(first)[0]


@pytest.mark.asyncio
async def test_host_card_withheld_when_admin_access_revoked_during_request(isolated_storage, api):
    await _seed()

    async def revoke(host_id):
        await storage.update_user_data(lambda cfg: cfg.authorized_users.update({"1": _user(1)}))
        return (Metric("system.cpu.util", "Private staff metric", "%", "17", 1700000000),)

    api.metrics.side_effect = revoke
    update, context = _callback_update(1, "status:host:42")
    await handlers.status_host_cb(update, context)
    api.metrics.assert_awaited_once_with("42")
    update.callback_query.edit_message_text.assert_not_awaited()
    update.effective_message.reply_text.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(("access_state", "tier"), [("blocked", "subscriber"), ("approved", "basic")])
async def test_public_status_withheld_after_subscriber_access_changes(isolated_storage, api, access_state, tier):
    await _seed()

    async def revoke(hosts, *, staff):
        changed_user = _user(2, access_state=access_state, service_tier=tier)
        await storage.update_user_data(lambda cfg: cfg.authorized_users.update({"2": changed_user}))
        return (Problem(HOST.id, "12", 4, False),)

    api.problems.side_effect = revoke
    update, context = _callback_update(2, "status:issues:0")
    await handlers.status_issues_cb(update, context)
    api.problems.assert_awaited_once_with((HOST,), staff=False)
    update.callback_query.edit_message_text.assert_not_awaited()
    update.effective_message.reply_text.assert_not_awaited()


def test_metric_pages_keep_missing_and_zero_distinct_and_clamp_stale_pages():
    metrics = tuple(Metric(f"net.if.in[eth{i}]", "Сеть", "bps", "0", 1700000000) for i in range(5))
    metrics += (Metric("docker.ping", "Docker", "", None, 0),)
    first, markup = views.metric_list(HOST, metrics, 0)
    assert "0 бит/с" in first
    assert "status:metrics:42:1" in _callback_names(markup)
    last, _ = views.metric_list(HOST, metrics, 999)
    assert "docker.ping" in last and "нет данных" in last


def test_invalid_metric_timestamp_preserves_the_returned_value():
    text, _ = views.metric_list(HOST, (Metric("docker.ping", "Docker", "", "1", 10**19),), 0)
    assert "1 · время измерения неизвестно" in text
