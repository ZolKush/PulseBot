import pytest

from app.bot.text_limits import utf16_length
from app.monitoring.status.metrics import (
    metric_categories,
    metric_detail,
    metric_pages,
    metric_sections,
    metric_summary,
)
from app.monitoring.zabbix import Metric


def reading(key, value, units=""):
    return Metric(key, key, units, value, 1700000000)


def test_server_overview_formats_system_metrics_network_and_docker():
    metrics = [
        reading("system.cpu.util", "14.171", "%"),
        reading("vm.memory.util", "35.8", "%"),
        reading("vfs.fs.dependent.size[/,pused]", "25", "%"),
        reading("system.cpu.load[all,avg15]", "0.4199"),
        reading("system.uptime", str(3 * 86400 + 12 * 3600), "uptime"),
        reading('net.if.in["eth0"]', "12400000", "bps"),
        reading("net.if.out[eth0]", "4262500", "Bps"),
        reading("docker.containers.running", "2"),
        reading("docker.containers.total", "2"),
    ]
    text = metric_summary(metrics)
    assert all(value in text for value in ("14,2 %", "35,8 %", "25 %", "0,42", "3 дня 12 часов"))
    assert text.count("eth0") == 1
    assert "12,4 Мбит/с" in text and "34,1 Мбит/с" in text
    assert "2 из 2 работают" in text
    assert all(metric.key not in text for metric in metrics)


def test_summary_exposes_missing_values_without_inventing_interface_or_docker_totals():
    metrics = [
        reading("vm.memory.util", "NaN", "%"),
        reading("system.cpu.util", "0", "%"),
        reading("docker.containers.running", "2"),
        reading("docker.containers.total", None),
        reading("net.if.in[eth0]", "125", "Bps"),
        reading("net.if.in[eth1]", "12450", "B"),
        reading("net.if.in[lo]", "0", "bps"),
    ]
    text = metric_summary(metrics)
    assert "0 %" in text and "нет данных" in text
    assert "1 кбит/с" in text and "12450 B" in text
    assert "eth0" in text and "eth1" in text and "Ещё интерфейсов в подробных метриках: 1" in text
    assert "2 из" not in text and "остановлены" not in text.lower()
    assert "Docker    недоступен" in metric_summary([*metrics, reading("docker.ping", "0")])


def test_disk_delays_are_rounded_in_milliseconds_and_keep_zero_distinct():
    read = reading("vfs.dev.read.await[vda]", "0.46222222222222226", "ms")
    write = reading("vfs.dev.write.await[vda]", "1.4582366589327147", "ms")
    assert metric_detail(read) == "Задержка чтения · vda · 0,46 мс"
    assert metric_detail(write) == "Задержка записи · vda · 1,46 мс"
    assert metric_detail(reading(write.key, "0", "ms")).endswith("0 мс")
    assert metric_detail(reading(write.key, "0.001", "ms")).endswith("<0,01 мс")
    assert metric_detail(reading(write.key, None, "ms")).endswith("нет данных")
    assert "0,46 мс" in metric_summary([read, write])


def test_details_hide_source_precision_keys_and_measurement_timestamps():
    traffic = metric_detail(reading('net.if.in["test0"]', "12400357", "bps"))
    assert traffic == "↓ Входящий · test0 · 12,4 Мбит/с"
    load = metric_detail(reading("system.cpu.load[all,avg15]", "0.41987"))
    assert load == "Средняя нагрузка за 15 мин · 0,42"
    huge = Metric("unfamiliar.metric", "😀" * 9000, "", "😀" * 9000, 10**20)
    assert utf16_length(metric_detail(huge)) < 500


def test_categories_filter_available_metrics_and_order_resource_readings():
    metrics = [
        reading("docker.ping", "1"),
        reading("net.if.out[eth0]", "2", "bps"),
        reading("net.if.in[eth1]", "3", "bps"),
        reading('net.if.in["eth0"]', "4", "bps"),
        reading("vfs.fs.dependent.size[/,pused]", "44", "%"),
        reading("system.cpu.load[all,avg15]", "2.4"),
        reading("vm.memory.util", "90", "%"),
        reading("system.cpu.util", "10", "%"),
        Metric("web.https.code", "Код ответа сайта", "", "204", 1700000000),
    ]
    assert [category for category, _ in metric_categories(metrics)] == ["load", "disk", "network", "docker", "web"]
    load = metric_sections(metric_pages(metrics, "load")[0])
    assert [line.split(" · ")[0] for line in load] == [
        "Загрузка процессора",
        "Память",
        "Средняя нагрузка за 15 мин",
    ]
    assert metric_sections(metric_pages(metrics, "disk")[0])[0].startswith("Корневой диск / · 44 %")
    network = metric_sections(metric_pages(metrics, "network")[0])
    assert [line.rsplit(" · ", 1)[0] for line in network] == [
        "↓ Входящий · eth0",
        "↑ Исходящий · eth0",
        "↓ Входящий · eth1",
    ]
    assert metric_sections(metric_pages(metrics, "docker")[0]) == ["Docker API · ✅ доступен"]
    assert "HTTP 204 · успешный ответ" in metric_sections(metric_pages(metrics, "web")[0])[0]


def test_metric_pages_keep_related_readings_together_at_a_page_boundary():
    network = [
        reading(f"net.if.{direction}[eth{interface}]", "0", "bps")
        for interface in range(3)
        for direction in ("in", "out")
    ]
    assert metric_pages(list(reversed(network)), "network") == [network[:4], network[4:]]


def test_metric_pages_keep_pagination_bounded_for_an_oversized_section():
    checks = [reading(f"net.tcp.service[tcp,test{i}.invalid,443]", "1") for i in range(7)]
    assert metric_pages(checks, "web") == [checks[:5], checks[5:]]
    assert metric_pages(checks, "disk") == [[]]


def test_service_checks_explain_availability_response_codes_and_timings():
    tcp = reading("net.tcp.service[tcp,probe.example,443]", "0")
    ok = Metric("web.https.ok", "Сайт {$SWEB.DOMAIN}", "", "1", 1700000000)
    code = Metric("web.https.code", "HTTPS: HTTP код", "", "200", 1700000000)
    timing = Metric("web.https.time", "HTTPS: время ответа", "s", "0.021482", 1700000000)
    metrics = [timing, code, ok, tcp]
    lines = metric_sections(metric_pages(metrics, "web")[0])
    assert lines == [
        "TCP 443 · probe.example · 🔴 соединение не установлено",
        "Проверка HTTPS · адрес не указан · ✅ пройдена",
        "Код ответа HTTPS · адрес не указан · HTTP 200 · успешный ответ",
        "Время ответа HTTPS · адрес не указан · 21,48 мс",
    ]
    summary = metric_summary(metrics)
    assert "21,48 мс" in summary and "HTTP 200 · успешный ответ" in summary
    assert "{$SWEB.DOMAIN}" not in summary and "web.https." not in summary and "0.021482" not in summary


def test_web_targets_are_verified_per_item_without_rendering_full_resolved_urls():
    tcp = Metric(
        "net.tcp.service[tcp,{$SWEB.DOMAIN},443]",
        "Доступность TCP {$SWEB.DOMAIN}",
        "",
        "1",
        1700000000,
        resolved_name="Адрес https://example.test:secret@real.example/path?q=another.example",
    )
    assert metric_detail(tcp) == "TCP 443 · real.example · ✅ соединение установлено"
    literal = Metric("web.https.ok", "HTTPS admin.example", "", "0", 1700000000)
    assert metric_detail(literal) == "Проверка HTTPS · admin.example · 🔴 не пройдена"
    unresolved = Metric("net.tcp.service[tcp,{$SWEB.DOMAIN},443]", "TCP {$SWEB.DOMAIN}", "", "1", 1700000000)
    assert metric_detail(unresolved) == "TCP 443 · адрес не указан · ✅ соединение установлено"
    ambiguous = Metric("web.https.ok", "HTTPS first.example или second.example", "", "1", 1700000000)
    assert "адрес не указан" in metric_detail(ambiguous)
    with_literal_key = Metric(
        "net.tcp.service[tcp,key.example,443]",
        "TCP name.example",
        "",
        "1",
        1700000000,
        resolved_name="TCP name.example",
    )
    assert metric_detail(with_literal_key) == "TCP 443 · key.example · ✅ соединение установлено"


@pytest.mark.parametrize(
    ("value", "filled"),
    [("0", 0), ("0.01", 1), ("50", 5), ("99.999", 9), ("100", 10)],
)
def test_percentage_scale_preserves_endpoints(value, filled):
    detail = metric_detail(reading("system.cpu.util", value, "%"))
    assert detail.splitlines()[1] == "■" * filled + "□" * (10 - filled)
    if value == "99.999":
        assert ">99,9 %" in detail


@pytest.mark.parametrize(
    "metric",
    [
        reading("system.cpu.util", None, "%"),
        reading("system.cpu.util", "NaN", "%"),
        reading("system.cpu.util", "110", "%"),
        reading("system.cpu.util", "-1", "%"),
        reading("system.cpu.util", "5", "ms"),
        reading("system.cpu.load[all,avg15]", "5"),
        Metric("system.cpu.util", "", "%", "15", 1700000000, supported=False),
    ],
)
def test_details_never_plot_unavailable_or_non_percentage_readings(metric):
    detail = metric_detail(metric)
    assert "■" not in detail and "□" not in detail
    if not metric.supported:
        assert "нет данных" in detail
