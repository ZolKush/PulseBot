import pytest

from app.bot.text_limits import utf16_length
from app.monitoring.status.metrics import metric_detail, metric_pages, metric_sections, metric_summary
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


def test_service_only_summary_and_detail_preserve_real_data_and_bound_telegram_text():
    disk = Metric("vfs.dev.read.await[sda]", "Задержка чтения sda", "ms", "180", 1700000000)
    unknown = Metric("zeronet.site.http[site.test]", "Сайт", "", "1", 1700000000, supported=False)
    assert "180 ms" in metric_summary([disk, unknown])
    assert "нет данных" in metric_detail(unknown)
    assert all(value in metric_detail(disk) for value in ("Чтение · sda", "180 ms", "2023", disk.key))
    huge = Metric("web.https.code", "😀" * 9000, "", "😀" * 9000, 10**20)
    assert utf16_length(metric_detail(huge)) < 500
    assert "время измерения неизвестно" in metric_detail(huge)


def test_details_preserve_the_original_precision_and_units_of_converted_readings():
    traffic = metric_detail(reading('net.if.in["test0"]', "12400357", "bps"))
    assert traffic.startswith("↓ Входящий · test0 · 12,4 Мбит/с\n")
    assert "В Zabbix 12400357 bps" in traffic
    load = metric_detail(reading("system.cpu.load[all,avg15]", "0.41987"))
    assert load.startswith("Средняя нагрузка за 15 мин · 0,42\n")
    assert "В Zabbix 0.41987" in load


def test_details_group_core_resources_first_and_pair_network_readings_by_interface():
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
    resources, system, network, docker, checks = (
        section for page in metric_pages(metrics) for section in metric_sections(page)
    )
    assert resources.startswith("🎛 Использование ресурсов\n")
    assert resources.index("Загрузка процессора") < resources.index("Память") < resources.index("Корневой диск /")
    assert system.startswith("🖥 Система\n")
    assert network.index("↓ Входящий · eth0") < network.index("↑ Исходящий · eth0") < network.index("↓ Входящий · eth1")
    assert "Docker API · доступен" in docker and "В Zabbix 1" in docker
    assert "Код ответа сайта · 204" in checks


def test_metric_pages_keep_related_readings_together_at_a_page_boundary():
    resources = [
        reading("system.cpu.util", "10", "%"),
        reading("vm.memory.util", "90", "%"),
        reading("vfs.fs.dependent.size[/,pused]", "44", "%"),
    ]
    disk = reading("vfs.dev.read.await[sda]", "1", "ms")
    network = [reading('net.if.in["eth0"]', "10", "bps"), reading("net.if.out[eth0]", "2", "bps")]
    assert metric_pages([*network, disk, *resources]) == [[*resources, disk], network]


def test_metric_pages_keep_pagination_bounded_for_an_oversized_section():
    checks = [reading(f"net.tcp.service[tcp,test{i}.invalid,443]", "1") for i in range(7)]
    assert metric_pages(checks) == [checks[:5], checks[5:]]
    assert metric_pages(()) == [[]]


@pytest.mark.parametrize(
    ("value", "filled"),
    [("0", 0), ("0.01", 1), ("50", 5), ("99.999", 9), ("100", 10)],
)
def test_percentage_scale_preserves_endpoints(value, filled):
    detail = metric_detail(reading("system.cpu.util", value, "%"))
    assert detail.splitlines()[1] == "■" * filled + "□" * (10 - filled)
    if value == "99.999":
        assert ">99,9 %" in detail and "В Zabbix 99.999 %" in detail


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
        assert "нет данных · элемент не поддерживается" in detail
