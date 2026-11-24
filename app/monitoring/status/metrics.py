"""Human-readable Zabbix readings for the host card and its detailed view."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal, InvalidOperation

from ...bot.ui import clip_text, humanize_hhmm, plural_ru
from ...config import TZ
from ..zabbix import Metric

_SYSTEM = {
    "system.cpu.util": "⚙️ Процессор",
    "vm.memory.util": "🧠 Память",
    "vfs.fs.dependent.size[/,pused]": "💾 Диск /",
    "system.cpu.load[all,avg15]": "📊 Нагрузка за 15 мин",
    "system.uptime": "⏱ Работает",
}
_PERCENTAGES = {"system.cpu.util", "vm.memory.util", "vfs.fs.dependent.size[/,pused]", "system.cpu.util[,steal]"}
_RATES = {"bps": 1, "bit/s": 1, "b/s": 1, "Bps": 8, "B/s": 8}
_NETWORK = {"net.if.in": "↓ Входящий", "net.if.out": "↑ Исходящий"}


def _short(value: str, limit: int) -> str:
    return clip_text(" ".join(value.split()), limit)


def _number(value: str) -> Decimal | None:
    if len(value) > 64:
        return None
    try:
        number = Decimal(value)
    except InvalidOperation:
        return None
    return number if number.is_finite() and abs(number.adjusted()) <= 308 else None


def _rounded(value: Decimal, places: int) -> str:
    if value and (abs(value) < Decimal(1).scaleb(-places) or abs(value) >= Decimal("1e18")):
        return f"{value:.2g}".replace(".", ",")
    return f"{value:.{places}f}".rstrip("0").rstrip(".").replace(".", ",")


def _uptime(seconds: int) -> str:
    minutes = seconds // 60
    days, hours = divmod(minutes // 60, 24)
    if days:
        text = f"{days} {plural_ru(days, 'день', 'дня', 'дней')}"
        return f"{text} {humanize_hhmm(hours, 0)}" if hours else text
    return humanize_hhmm(hours, minutes % 60) if minutes else f"{seconds} с"


def _reading(metric: Metric) -> str:
    if metric.value is None or not metric.value.strip() or not metric.supported:
        return "нет данных"
    raw = _short(metric.value, 140)
    units = _short(metric.units, 32)
    value = _number(metric.value)
    if metric.key in _PERCENTAGES and units in {"%", ""}:
        return f"{_rounded(value, 1)} %" if value is not None else "нет данных"
    if metric.key == "system.uptime" and units in {"s", "uptime", ""}:
        return _uptime(int(value)) if value is not None and 0 <= value <= 2**64 - 1 else "нет данных"
    if units in _RATES:
        if value is None or value < 0:
            return "нет данных"
        bits = value * _RATES[units]
        for scale, label in ((10**9, "Гбит/с"), (10**6, "Мбит/с"), (10**3, "кбит/с"), (1, "бит/с")):
            if bits >= scale or scale == 1:
                return f"{_rounded(bits / scale, 2)} {label}"
    if metric.key == "system.cpu.load[all,avg15]" and not units:
        return _rounded(value, 2) if value is not None else "нет данных"
    return f"{raw} {units.removeprefix('!')}".strip()


def _network_blocks(metrics: Sequence[Metric]) -> list[str]:
    interfaces: dict[str, dict[str, Metric]] = {}
    for metric in metrics:
        prefix, _, argument = metric.key.partition("[")
        if prefix in _NETWORK:
            interface = argument.removesuffix("]")
            if interface.startswith('"') and interface.endswith('"'):
                interface = interface[1:-1].replace(r"\"", '"')
            interfaces.setdefault(interface, {})[prefix] = metric
    blocks = []
    for interface in sorted(interfaces)[:2]:
        lines = [f"🌐 Сеть · {_short(interface, 65)}"]
        for key, label in _NETWORK.items():
            if key in interfaces[interface]:
                lines.append(f"{label}   {_reading(interfaces[interface][key])}")
        blocks.append("\n".join(lines))
    if len(interfaces) > 2:
        blocks.append(f"Ещё интерфейсов в подробных метриках: {len(interfaces) - 2}")
    return blocks


def _docker_block(by_key: dict[str, Metric]) -> str:
    keys = {
        "docker.containers.running": "Работают",
        "docker.containers.total": "Всего",
        "docker.containers.stopped": "Остановлены",
    }
    values = {key: _reading(by_key[key]) for key in keys if key in by_key}
    running, total = values.get("docker.containers.running", ""), values.get("docker.containers.total", "")
    ping = _reading(by_key["docker.ping"]) if "docker.ping" in by_key else ""
    if ping == "0":
        return "🐳 Docker    недоступен"
    if running.isdecimal() and total.isdecimal() and int(running) <= int(total):
        verb = plural_ru(int(running), "работает", "работают", "работают")
        return f"🐳 Docker    {running} из {total} {verb}"
    if values:
        return "🐳 Docker\n" + "\n".join(f"{keys[key]}   {value}" for key, value in values.items())
    if ping:
        return "🐳 Docker    " + ("доступен" if ping == "1" else ping)
    return ""


def metric_summary(metrics: Sequence[Metric]) -> str:
    by_key = {metric.key: metric for metric in metrics}
    system = [f"{label}   {_reading(by_key[key])}" for key, label in _SYSTEM.items() if key in by_key]
    blocks = ["\n".join(system), *_network_blocks(metrics), _docker_block(by_key)]
    if any(blocks):
        return "\n\n".join(block for block in blocks if block)
    # Some monitored hosts have only service-specific items.
    if metrics:
        return "\n".join(f"{_short(item.name or item.key, 80)}   {_reading(item)}" for item in metrics[:3])
    return "Для узла нет настроенных метрик из списка мониторинга."


def metric_detail(metric: Metric) -> str:
    label = _short(metric.name or metric.key, 90)
    key = _short(metric.key, 195)
    value = _reading(metric)
    if metric.value is not None and metric.supported and metric.lastclock:
        raw = f"{_short(metric.value, 140)} {_short(metric.units, 32).removeprefix('!')}".strip()
        if value != "нет данных" and raw.replace(".", ",") != value.replace(".", ","):
            value += f" ({raw})"
        try:
            stamp = datetime.fromtimestamp(metric.lastclock, TZ)
            value += f" · {stamp:%d.%m.%Y %H:%M %Z}"
        except (OverflowError, OSError, ValueError):
            value += " · время измерения неизвестно"
    return "\n".join([label, value] if label == key else [label, value, key])
