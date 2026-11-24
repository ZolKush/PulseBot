from app.bot.text_limits import utf16_length
from app.monitoring.status.metrics import metric_detail, metric_summary
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
    assert all(value in metric_detail(disk) for value in ("Задержка чтения sda", "180 ms", "2023", disk.key))
    huge = Metric("web.https.code", "😀" * 9000, "", "😀" * 9000, 10**20)
    assert utf16_length(metric_detail(huge)) < 500
    assert "время измерения неизвестно" in metric_detail(huge)


def test_details_preserve_the_original_precision_and_units_of_converted_readings():
    assert "12,4 Мбит/с (12400357 bps)" in metric_detail(reading('net.if.in["test0"]', "12400357", "bps"))
    assert "0,42 (0.41987)" in metric_detail(reading("system.cpu.load[all,avg15]", "0.41987"))
