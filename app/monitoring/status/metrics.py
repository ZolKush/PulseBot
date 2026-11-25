"""Human-readable Zabbix readings for the host card and its detailed view."""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Sequence
from decimal import Decimal, InvalidOperation
from itertools import groupby
from urllib.parse import urlsplit

from ...bot.ui import clip_text, humanize_hhmm, plural_ru
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
METRIC_CATEGORY_LABELS = {
    "load": "🎛 Нагрузка",
    "disk": "💽 Диски",
    "network": "🌐 Сеть",
    "docker": "🐳 Docker",
    "remnawave": "🔗 RemnaWave",
    "web": "🔎 Сайты и проверки",
}
_METRIC_LABELS = {
    "load": {
        "system.cpu.util": "Загрузка процессора",
        "vm.memory.util": "Память",
        "system.cpu.load[all,avg15]": "Средняя нагрузка за 15 мин",
        "system.cpu.util[,steal]": "Время ожидания процессора",
        "system.uptime": "Время работы",
    },
    "disk": {
        "vfs.fs.dependent.size[/,pused]": "Корневой диск /",
        "vfs.dev.read.await[*]": "Задержка чтения",
        "vfs.dev.write.await[*]": "Задержка записи",
    },
    "network": {f"{key}[*]": label for key, label in _NETWORK.items()},
    "docker": {
        "docker.ping": "Docker API",
        "docker.containers.running": "Работает контейнеров",
        "docker.containers.stopped": "Остановлено контейнеров",
        "docker.containers.total": "Всего контейнеров",
    },
    "remnawave": {
        "remnawave.users.online": "Пользователи онлайн",
        "proc.num[rw-core]": "Процессы rw-core",
        "remnawave.node.status[*]": "Состояние узла",
        "remnawave.node.online[*]": "Пользователи онлайн на узле",
        "remnawave.node.rx[*]": "Входящий трафик узла",
        "remnawave.node.tx[*]": "Исходящий трафик узла",
    },
    "web": {
        "net.tcp.service[tcp,*,443]": "TCP 443",
        "web.https.ok": "Проверка HTTPS",
        "zeronet.site.http[*]": "Проверка сайта",
        "zeronet.tls.validation[*]": "Проверка сертификата TLS",
        "zeronet.tls.expiry[*]": "Срок действия сертификата TLS",
        "zeronet.derp.route[*]": "Маршрут DERP",
        "web.https.code": "Код ответа HTTPS",
        "web.https.time": "Время ответа HTTPS",
    },
}
_METRIC_ORDER = {
    family: (category, position)
    for category, labels in _METRIC_LABELS.items()
    for position, family in enumerate(labels)
}
_BINARY_RESULTS = {
    "docker.ping": ("🔴 недоступен", "✅ доступен"),
    "net.tcp.service[tcp,*,443]": ("🔴 соединение не установлено", "✅ соединение установлено"),
    "web.https.ok": ("🔴 не пройдена", "✅ пройдена"),
}
_LATENCIES = {"vfs.dev.read.await[*]", "vfs.dev.write.await[*]", "web.https.time"}
_HTTP_STATUS = {
    401: "требуется авторизация",
    403: "доступ запрещён",
    404: "страница не найдена",
    429: "слишком много запросов",
    502: "ошибка шлюза",
    503: "сервис недоступен",
    504: "шлюз не дождался ответа",
}
_HTTP_CLASSES = ("информация", "успешный ответ", "перенаправление", "ошибка запроса", "ошибка сервера")
_DNS_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
_DNS_NAME = re.compile(rf"{_DNS_LABEL}(?:\.{_DNS_LABEL})*", re.IGNORECASE | re.ASCII)
_DOMAIN_IN_NAME = re.compile(
    rf"(?<![\w./@=?&-])(?:{_DNS_LABEL}\.)+[a-z](?:[a-z0-9-]{{0,61}}[a-z0-9])?\.?(?![\w.@-])",
    re.IGNORECASE,
)
_URL_IN_NAME = re.compile(r"""https?://[^\s<>"']+""", re.IGNORECASE)


def _category(family: str) -> str:
    return _METRIC_ORDER.get(family, ("web", len(_METRIC_LABELS["web"])))[0]


def metric_categories(metrics: Sequence[Metric]) -> list[tuple[str, str]]:
    available = {_category(_family(item)) for item in metrics}
    return [(category, label) for category, label in METRIC_CATEGORY_LABELS.items() if category in available]


def _family(metric: Metric) -> str:
    return metric_family(metric.key) or metric.key


def _without_macro(value: str) -> str:
    return "" if "{" in value or "}" in value else value.strip()


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


def _unquote_argument(argument: str) -> str:
    if argument.startswith('"') and argument.endswith('"'):
        return argument[1:-1].replace(r"\"", '"')
    return argument


def _argument(key: str, family: str) -> str:
    prefix, wildcard, suffix = family.partition("*")
    if not wildcard:
        return ""
    return _unquote_argument(key[len(prefix) : len(key) - len(suffix)])


def _hostname(value: str) -> str:
    value = _without_macro(value)
    if not value or len(value) > 2048:
        return ""
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        pass
    try:
        host = urlsplit(value if "://" in value else f"//{value}").hostname or ""
        host = host.removesuffix(".")
        ascii_host = host.encode("idna").decode("ascii")
    except ValueError:
        return ""
    try:
        return str(ipaddress.ip_address(host))
    except ValueError:
        pass
    if len(ascii_host) > 253 or not _DNS_NAME.fullmatch(ascii_host):
        return ""
    return host


def _hostname_in_name(name: str) -> str:
    if not _without_macro(name) or len(name) > 4096:
        return ""
    targets = {_hostname(url) for url in _URL_IN_NAME.findall(name)}
    targets.update(_hostname(match) for match in _DOMAIN_IN_NAME.findall(_URL_IN_NAME.sub(" ", name)))
    targets.discard("")
    return targets.pop() if len(targets) == 1 else ""


def _web_target(metric: Metric, family: str) -> str:
    if target := _hostname(_argument(metric.key, family)):
        return target
    target = _hostname_in_name(metric.resolved_name) if metric.resolved_name != metric.key else ""
    if target and target != family:
        return target
    target = _hostname_in_name(metric.name) if metric.name != metric.key else ""
    return target if target != family else ""


def _network_blocks(metrics: Sequence[Metric]) -> list[str]:
    interfaces: dict[str, dict[str, Metric]] = {}
    for metric in metrics:
        prefix, _, argument = metric.key.partition("[")
        if prefix in _NETWORK:
            interface = _unquote_argument(argument.removesuffix("]"))
            interfaces.setdefault(interface, {})[prefix] = metric
    blocks = []
    for interface in sorted(interfaces)[:2]:
        label = _short(_without_macro(interface), 65) or "интерфейс не указан"
        lines = [f"🌐 Сеть · {label}"]
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
        return "\n".join(metric_detail(item).splitlines()[0] for item in metrics[:3])
    return "Для узла нет настроенных метрик из списка мониторинга."


def _detail_sort_key(metric: Metric) -> tuple[str, int, str]:
    family = _family(metric)
    category, position = _METRIC_ORDER.get(family, ("web", len(_METRIC_LABELS["web"])))
    argument = _argument(metric.key, family)
    if category == "web":
        # Keep availability checks ahead of response codes and timings.
        if family in _BINARY_RESULTS and metric.supported and metric.value == "0":
            return "", -1, metric.key
        return "", position, metric.key
    return argument, position, metric.key


def metric_pages(metrics: Sequence[Metric], category: str) -> list[list[Metric]]:
    pages: list[list[Metric]] = [[]]
    ordered = sorted((item for item in metrics if _category(_family(item)) == category), key=_detail_sort_key)
    for _, items in groupby(ordered, key=lambda metric: _detail_sort_key(metric)[0]):
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


def _detail_label(metric: Metric, family: str) -> str:
    category = _category(family)
    label = _METRIC_LABELS[category].get(family, "Показатель")
    if category == "web" and family in _METRIC_LABELS["web"]:
        target = _short(_web_target(metric, family), 90) or "адрес не указан"
        return f"{label} · {target}"
    if "*" in family and category in {"network", "disk"}:
        argument = _short(_without_macro(_argument(metric.key, family)), 65)
        missing = "интерфейс не указан" if category == "network" else "диск не указан"
        return f"{label} · {argument or missing}"
    if ("*" in family and category == "remnawave") or family not in _METRIC_ORDER:
        name = _without_macro(metric.name)
        return _short(name, 90) if name and name != metric.key else label
    return label


def _delay(value: Decimal, units: str, *, web: bool) -> str:
    milliseconds = value * 1000 if units in {"s", "с"} else value
    if web and milliseconds >= 1000:
        return f"{_rounded(milliseconds / 1000, 2)} с"
    if 0 < milliseconds < Decimal("0.01"):
        return "<0,01 мс"
    return f"{_rounded(milliseconds, 2)} мс"


def _detail_reading(metric: Metric, family: str) -> str:
    if not metric.supported or metric.value is None or not metric.value.strip():
        return "нет данных"
    value = _number(metric.value)
    units = metric.units.strip().removeprefix("!")
    if family in _BINARY_RESULTS and not units:
        if value is not None and value in (0, 1):
            return _BINARY_RESULTS[family][int(value)]
        return "нет корректного результата проверки"
    if family == "web.https.code":
        if value is not None and 100 <= value <= 599 and value == int(value):
            code = int(value)
            return f"HTTP {code} · {_HTTP_STATUS.get(code, _HTTP_CLASSES[code // 100 - 1])}"
        return "нет кода ответа HTTP"
    if family in _LATENCIES and units in {"s", "с", "ms", "мс"}:
        if value is None or value < 0:
            return "нет данных"
        return _delay(value, units, web=family == "web.https.time")
    return _reading(metric)


def metric_detail(metric: Metric) -> str:
    family = _family(metric)
    value = _detail_reading(metric, family)
    percent = _plottable_percentage(metric)
    if percent is not None and percent < 100 and value == "100 %":
        value = ">99,9 %"
    lines = [f"{_detail_label(metric, family)} · {value}"]
    if percent is not None:
        lines.append(_percentage_scale(percent))
    return "\n".join(lines)


def metric_sections(metrics: Sequence[Metric]) -> list[str]:
    return [metric_detail(item) for item in metrics]
