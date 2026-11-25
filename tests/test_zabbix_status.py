"""Display, freshness and role boundaries for the Telegram status button."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app import storage
from app.bot.text_limits import utf16_length
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
            assert text.startswith("✅") and "\n" not in text
        else:
            assert text.startswith("🖥 Статус серверов")
            assert "Серверов с проблемами: 1" in text
            assert f"🔴 {HOST.name} · {expected}" in text.splitlines()
            assert "👁 Подтверждена · 🔗 Связана с другой проблемой." in text
            assert "Private" not in text and issue.event_id not in text
            assert issue.cause_eventid not in text
        assert _callback_names(call.kwargs["reply_markup"]) == {"status:issues:0", "menu:home"}
    api.problems.assert_awaited_with((HOST,), staff=False)
    api.metrics.assert_not_awaited()


def test_subscriber_shows_only_newest_event_at_highest_severity_per_host():
    issues = (
        Problem(HOST.id, "99999", 5, False),
        Problem(HOST.id, "100001", 4, False),
        Problem(HOST.id, "100000", 5, True, cause_eventid="119000", name="Private trigger", comment="Private comment"),
        Problem(HOST.id, "100002", 2, False),
    )
    text, markup = views.problem_list((HOST,), issues, 0, staff=False)
    assert text.count(HOST.name) == 1
    assert f"🚨 {HOST.name} · Критическая проблема" in text.splitlines()
    assert "👁 Подтверждена · 🔗 Связана с другой проблемой." in text
    assert all(label not in text for label in ("Высокая важность", "Предупреждение", "Ожидает подтверждения"))
    assert "Private" not in text and "119000" not in text
    assert _callback_names(markup) == {"status:issues:0", "menu:home"}


def test_subscriber_problem_pages_count_each_host_once_and_show_most_severe_first():
    hosts = tuple(Host(str(i), f"Сервер {i}") for i in range(1, 7))
    issues = tuple(
        Problem(host.id, str(i + 100), severity, False)
        for i, (host, severity) in enumerate(zip(hosts, (4, 2, 2, 2, 3, 5), strict=True))
    )
    issues += tuple(Problem(host.id, "108", 2, False) for host in hosts)

    first, markup = views.problem_list(hosts, issues, 0, staff=False)
    assert "Серверов с проблемами: 6" in first
    headings = [line for line in first.splitlines() if "Сервер " in line]
    assert headings == [
        "🚨 Сервер 6 · Критическая проблема",
        "🔴 Сервер 1 · Высокая важность",
        "🟠 Сервер 5 · Средняя важность",
        "🟡 Сервер 2 · Предупреждение",
        "🟡 Сервер 3 · Предупреждение",
    ]
    assert "status:issues:1" in _callback_names(markup)

    last, markup = views.problem_list(hosts, issues, 1, staff=False)
    assert [line for line in last.splitlines() if "Сервер " in line] == ["🟡 Сервер 4 · Предупреждение"]
    assert {"status:issues:0", "status:issues:1", "menu:home"} == _callback_names(markup)


def test_staff_problem_page_distinguishes_unique_active_and_suppressed_events_within_telegram_limit():
    long_text = "🛠" * 300
    hosts = (Host("42", f"Первый {long_text}"), Host("43", f"Второй {long_text}"))
    issues = tuple(
        Problem(
            host_id,
            event_id,
            5,
            True,
            suppressed=suppressed,
            cause_eventid="12345678901234567890",
            name=f"Триггер {long_text}",
            comment=f"Заметка {long_text}",
            source_host_name=f"Проверяющий узел {long_text}",
        )
        for host_id, event_id, suppressed in (
            ("42", "12345678901234567891", False),
            ("42", "12345678901234567892", False),
            ("42", "12345678901234567893", True),
            ("43", "12345678901234567891", False),
            ("43", "12345678901234567893", True),
        )
    )
    text, _ = views.problem_list(hosts, issues, 0, staff=True)
    assert utf16_length(text) <= 4000
    assert "Активных: 2 · 🔕 Подавленных: 1" in text
    assert text.count("🚨 Чрезвычайная · Триггер") == 5
    assert text.count("🔕 Подавлена · 👁 Подтверждена") == 2
    assert text.count("🔗 Симптом проблемы #12345678901234567890") == 5
    assert text.count("💬 Комментарий: Заметка") == 5
    assert text.count("Источник проверки · Проверяющий узел") == 5


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
        (handlers.status_host_page_cb, "status:metrics:42:load:0"),
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
    probe = Host("100", "Проверяющий узел")
    hosts = (probe, *tuple(Host(str(i), f"Сервер {i}") for i in range(1, views.HOSTS_PER_PAGE)), HOST)
    api.hosts.return_value = hosts
    api.problems.return_value = (
        Problem(probe.id, "20", 5, True, name="Чужая проблема"),
        Problem(HOST.id, "11", 2, True, name="Мало памяти"),
        Problem(HOST.id, "12", 4, True, name="Проверка HTTPS", comment="В работе", source_host_name=probe.name),
        Problem(HOST.id, "14", 5, True, suppressed=True, cause_eventid="12", name="Пауза"),
    )
    api.metrics.return_value = (Metric("system.cpu.util", "CPU", "%", "17", 1700000000),)
    update, context = _callback_update(1, "status:host:42")
    await handlers.status_host_cb(update, context)
    text, markup = rendered(update)
    assert all(label in text for label in (HOST.name, "Процессор", "17 %", "Проверка HTTPS", "подтверждена"))
    assert text.index("Проверка HTTPS") < text.index("Процессор")
    assert all(label not in text for label in ("system.cpu.util", "Пауза", "Чужая проблема"))
    assert "Других активных проблем: 1" in text and "Подавленных проблем: 1" in text
    assert "🕒 Последние данные Zabbix · 15.11.2023 01:13:20" in text
    assert {
        "status:metrics:42:load:0",
        "status:hostissues:42:0",
        "status:host:42",
        "status:hosts:1",
    } == _callback_names(markup)
    api.metrics.assert_awaited_once_with("42")
    api.problems.assert_awaited_once_with(hosts, staff=True)

    update.callback_query.data = "status:metrics:42:0"
    await handlers.status_host_page_cb(update, context)
    text, markup = rendered(update)
    assert "Подробные метрики" in text
    assert {"status:metrics:42:load:0", "status:metrics:42:0", "status:host:42", "menu:home"} == _callback_names(markup)

    update.callback_query.data = "status:metrics:42:load:0"
    for value in ("17", "30"):
        api.metrics.return_value = (Metric("system.cpu.util", "CPU", "%", value, 1700000001),)
        await handlers.status_host_page_cb(update, context)
        text, markup = rendered(update)
        assert all(label in text for label in ("Нагрузка", "Загрузка процессора", f"{value} %"))
        assert "system.cpu.util" not in text and "2023" not in text
        assert {"status:metrics:42:load:0", "status:host:42"} <= _callback_names(markup)
    api.problems.assert_awaited_once()

    update.callback_query.data = "status:hostissues:42:0"
    await handlers.status_host_page_cb(update, context)
    text, markup = rendered(update)
    assert all(value in text for value in ("Проверка HTTPS", "В работе", "Подавлена", "#12"))
    assert f"Источник проверки · {probe.name}" in text
    assert "Чужая проблема" not in text and "Активных: 2 · 🔕 Подавленных: 1" in text
    assert {"status:hostissues:42:0", "status:host:42"} <= _callback_names(markup)
    api.problems.assert_awaited_with(hosts, staff=True)
    assert api.problems.await_count == 2

    update.callback_query.data = "status:metrics:43:0"
    await handlers.status_host_page_cb(update, context)
    text, markup = rendered(update)
    assert "больше недоступен" in text and "status:hosts:0" in _callback_names(markup)
    assert api.metrics.await_count == 4


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("uid", "callback", "data"),
    [
        (1, handlers.status_hosts_cb, "status:hosts:0"),
        (1, handlers.status_host_cb, "status:host:42"),
        (1, handlers.status_host_page_cb, "status:hostissues:42:0"),
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
    metrics = tuple(Metric(f"net.if.in[eth{i}]", "Сеть", "bps", "0", 1700000000) for i in range(6))
    metrics += (
        Metric("docker.ping", "Docker", "", None, 0),
        Metric("system.cpu.util", "CPU", "%", "0", 1700000000),
    )
    first, markup = views.metric_list(HOST, metrics, 0, category="network")
    assert "eth0" in first and "0 бит/с" in first and "0 %" not in first
    assert "status:metrics:42:network:1" in _callback_names(markup)
    last, markup = views.metric_list(HOST, metrics, 999, category="network")
    assert "eth5" in last and "eth0" not in last and "Docker" not in last
    assert {
        "status:metrics:42:network:0",
        "status:metrics:42:network:1",
        "status:host:42",
        "menu:home",
    } == _callback_names(markup)
    missing, _ = views.metric_list(HOST, metrics, 0, category="docker")
    assert "Docker" in missing and "нет данных" in missing and "docker.ping" not in missing


def test_host_card_shows_available_metric_categories_and_newest_valid_measurement():
    metrics = (
        Metric("system.cpu.util", "CPU", "%", "0", 1700000000),
        Metric("vfs.fs.dependent.size[/,pused]", "Диск", "%", "2", 1699999999),
        Metric("net.if.in[eth0]", "Сеть", "bps", "0", 1700000001),
        Metric("docker.ping", "Docker", "", "1", 10**19),
        Metric("remnawave.users.online", "Пользователи", "", None, 1700000002),
        Metric("web.https.ok", "HTTPS", "", "0", 1700000003, supported=False),
    )
    text, markup = views.host_card(HOST, metrics, (), 0)
    assert text.count("🕒 Последние данные Zabbix · 15.11.2023 01:13:21") == 1
    categories = {
        f"status:metrics:42:{category}:0" for category in ("load", "disk", "network", "docker", "remnawave", "web")
    }
    assert categories <= _callback_names(markup)
    assert markup.inline_keyboard[0][0].callback_data == "status:hostissues:42:0"
    assert "status:metrics:42:0" not in _callback_names(markup)


def test_missing_metric_category_preserves_refresh_and_back_to_host():
    text, markup = views.metric_list(HOST, (), 99, category="docker")
    assert "Docker" in text and "нет метрик в этой категории" in text
    assert {"status:metrics:42:docker:0", "status:host:42", "menu:home"} == _callback_names(markup)
    card, markup = views.host_card(HOST, (), (), 0)
    assert "🕒 Последние данные Zabbix · время неизвестно" in card
    assert not any(callback.startswith("status:metrics:") for callback in _callback_names(markup))
