"""Human-readable Zabbix readings for the host card and its detailed view."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal, InvalidOperation
from itertools import groupby

from ...bot.ui import clip_text, humanize_hhmm, plural_ru
from ...config import TZ
from ..zabbix import Metric
from ..zabbix.metric_keys import metric_family

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
_METRICS_PER_PAGE = 5
_DETAIL_SECTIONS: tuple[tuple[str, dict[str, str]], ...] = (
    (
        "🎛 Использование ресурсов",
        {
            "system.cpu.util": "Загрузка процессора",
            "vm.memory.util": "Память",
            "vfs.fs.dependent.size[/,pused]": "Корневой диск /",
        },
    ),
    (
        "🖥 Система",
        {
            "system.cpu.load[all,avg15]": "Средняя нагрузка за 15 мин",
            "system.cpu.util[,steal]": "Ожидание процессора (steal)",
            "system.uptime": "Время работы",
        },
    ),
    (
        "💽 Задержки дисков",
        {"vfs.dev.read.await[*]": "Чтение", "vfs.dev.write.await[*]": "Запись"},
    ),
    ("🌐 Сеть", {f"{key}[*]": label for key, label in _NETWORK.items()}),
    (
        "🐳 Docker",
        {
            "docker.ping": "Docker API",
            "docker.containers.running": "Работает контейнеров",
            "docker.containers.stopped": "Остановлено контейнеров",
            "docker.containers.total": "Всего контейнеров",
        },
    ),
    ("🔗 RemnaWave", {}),
    ("🔎 Проверки сервисов", {}),
)
_DETAIL_ORDER = {
    family: (section, position)
    for section, (_, labels) in enumerate(_DETAIL_SECTIONS)
    for position, family in enumerate(labels)
}


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


def _item_argument(argument: str) -> str:
    argument = argument.removesuffix("]")
    if argument.startswith('"') and argument.endswith('"'):
        return argument[1:-1].replace(r"\"", '"')
    return argument


def _network_blocks(metrics: Sequence[Metric]) -> list[str]:
    interfaces: dict[str, dict[str, Metric]] = {}
    for metric in metrics:
        prefix, _, argument = metric.key.partition("[")
        if prefix in _NETWORK:
            interface = _item_argument(argument)
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


def _detail_position(family: str) -> tuple[int, int]:
    is_remnawave = family.startswith("remnawave.") or family == "proc.num[rw-core]"
    fallback = len(_DETAIL_SECTIONS) - (2 if is_remnawave else 1)
    return _DETAIL_ORDER.get(family, (fallback, 0))


def _detail_sort_key(metric: Metric) -> tuple[int, str, int, str]:
    family = metric_family(metric.key) or metric.key
    section, position = _detail_position(family)
    argument = _item_argument(metric.key.partition("[")[2]) if family.endswith("[*]") else ""
    return section, argument, position, metric.key


def metric_pages(metrics: Sequence[Metric]) -> list[list[Metric]]:
    pages: list[list[Metric]] = [[]]
    ordered = sorted(metrics, key=_detail_sort_key)
    for _, items in groupby(ordered, key=lambda metric: _detail_sort_key(metric)[:2]):
        group = list(items)
        for offset in range(0, len(group), _METRICS_PER_PAGE):
            chunk = group[offset : offset + _METRICS_PER_PAGE]
            if len(pages[-1]) + len(chunk) > _METRICS_PER_PAGE:
                pages.append([])
            pages[-1].extend(chunk)
    return pages


def _plottable_percentage(metric: Metric) -> Decimal | None:
    if metric.key not in _PERCENTAGES or _short(metric.units, 32) not in {"%", ""} or not metric.supported:
        return None
    value = _number(metric.value) if metric.value is not None else None
    return value if value is not None and 0 <= value <= 100 else None


def _percentage_scale(value: Decimal) -> str:
    # Keep exact zero and full usage distinct from fractional readings.
    filled = int(value) // 10 if value in (0, 100) else max(1, min(9, int((value + 5) // 10)))
    return "■" * filled + "□" * (10 - filled)


def metric_detail(metric: Metric) -> str:
    family = metric_family(metric.key) or metric.key
    section, _ = _detail_position(family)
    label = _DETAIL_SECTIONS[section][1].get(family)
    if label and family.endswith("[*]"):
        label += f" · {_item_argument(metric.key.partition('[')[2])}"
    label = _short(label or metric.name or metric.key, 90)
    value = _reading(metric)
    percent = _plottable_percentage(metric)
    if percent is not None and percent < 100 and value == "100 %":
        value = ">99,9 %"
    if metric.key == "docker.ping":
        value = {"0": "недоступен", "1": "доступен"}.get(value, value)
    if not metric.supported:
        value += " · элемент не поддерживается"
    key = _short(metric.key, 195)
    metadata = [f"Ключ {key}"] if label != key else []
    if metric.value is not None and metric.supported and metric.lastclock:
        try:
            stamp = datetime.fromtimestamp(metric.lastclock, TZ)
            metadata.append(f"Измерено {stamp:%d.%m.%Y %H:%M %Z}")
        except (OverflowError, OSError, ValueError):
            metadata.append("время измерения неизвестно")
        raw = f"{_short(metric.value, 140)} {_short(metric.units, 32).removeprefix('!')}".strip()
        if value != "нет данных" and raw.replace(".", ",") != value.replace(".", ","):
            metadata.append(f"В Zabbix {raw}")
    lines = [f"{label} · {value}"]
    if percent is not None:
        lines.append(_percentage_scale(percent))
    if metadata:
        lines.append("↳ " + " · ".join(metadata))
    return "\n".join(lines)


def metric_sections(metrics: Sequence[Metric]) -> list[str]:
    """Render a page of ordered metrics with a heading for each visible section."""
    blocks = []
    for section, items in groupby(metrics, key=lambda metric: _detail_sort_key(metric)[0]):
        details = "\n\n".join(metric_detail(item) for item in items)
        blocks.append(f"{_DETAIL_SECTIONS[section][0]}\n{details}")
    return blocks
