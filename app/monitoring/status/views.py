"""Telegram screens for the current Zabbix inventory, problems and latest values."""

from __future__ import annotations

import re
from datetime import datetime

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from ...bot.ui import btn_back, btn_home, clip_text, pager_row
from ...config import TZ
from ..zabbix import Host, Metric, Problem
from .metrics import METRIC_CATEGORY_LABELS, metric_categories, metric_pages, metric_sections, metric_summary

Screen = tuple[str, InlineKeyboardMarkup]
HEALTHY = "✅ Все системы работают штатно, проблем не замечено."
UNAVAILABLE = "❔ Сейчас не удалось получить состояние серверов. Попробуйте обновить статус позже."

_PUBLIC_SEVERITIES = {
    2: ("Предупреждение", "В работе сервера замечено отклонение."),
    3: ("Средняя важность", "Отдельные функции могут работать с перебоями."),
    4: ("Высокая важность", "Возможны существенные перебои в работе сервера."),
    5: ("Критическая проблема", "Сервис может быть недоступен."),
}
_STAFF_SEVERITIES = ("Не классифицирована", "Информация", "Предупреждение", "Средняя", "Высокая", "Чрезвычайная")
_SEVERITY_MARKS = ("⚪", "ℹ️", "🟡", "🟠", "🔴", "🚨")


def _label(value: str, budget: int) -> str:
    return clip_text(" ".join(value.split()), limit=budget)


def _page(length: int, size: int, requested: int) -> tuple[int, int, slice]:
    total = max(1, (length + size - 1) // size)
    page = max(0, min(requested, total - 1))
    return page, total, slice(page * size, (page + 1) * size)


def _navigation(prefix: str, page: int, total: int) -> list[list[InlineKeyboardButton]]:
    rows = [pager_row(prefix, page, total)] if total > 1 else []
    rows.append([InlineKeyboardButton("🔄 Обновить", callback_data=f"{prefix}{page}")])
    return rows


HOSTS_PER_PAGE = 10
_COUNTRY_FLAGS = {
    "russia": "🇷🇺",
    "россия": "🇷🇺",
    "estonia": "🇪🇪",
    "эстония": "🇪🇪",
    "sweden": "🇸🇪",
    "швеция": "🇸🇪",
    "netherlands": "🇳🇱",
    "нидерланды": "🇳🇱",
    "poland": "🇵🇱",
    "польша": "🇵🇱",
}


def _host_title(host: Host) -> str:
    name = _label(host.name, 120)
    country = re.match(r"[^\W\d_]+", name, re.UNICODE)
    flag = _COUNTRY_FLAGS.get(country.group().casefold()) if country else None
    return f"{flag} {name}" if flag else name


def _metric_buttons(host: Host, metrics: tuple[Metric, ...]) -> list[list[InlineKeyboardButton]]:
    buttons = [
        InlineKeyboardButton(label, callback_data=f"status:metrics:{host.id}:{category}:0")
        for category, label in metric_categories(metrics)
    ]
    return [buttons[i : i + 2] for i in range(0, len(buttons), 2)]


def _latest_measurement(metrics: tuple[Metric, ...]) -> str:
    timestamps = {
        metric.lastclock
        for metric in metrics
        if metric.supported and metric.value is not None and metric.value.strip() and metric.lastclock > 0
    }
    for timestamp in sorted(timestamps, reverse=True):
        try:
            stamp = datetime.fromtimestamp(timestamp, TZ)
        except (OSError, OverflowError, ValueError):
            continue
        return f"🕒 Последние данные Zabbix · {stamp:%d.%m.%Y %H:%M:%S %Z}"
    return "🕒 Последние данные Zabbix · время неизвестно"


def host_list(hosts: tuple[Host, ...], problems: tuple[Problem, ...], requested: int) -> Screen:
    page, total, portion = _page(len(hosts), HOSTS_PER_PAGE, requested)
    visible = hosts[portion]
    affected = {p.host_id for p in problems if not p.suppressed}
    active_events = {p.event_id for p in problems if not p.suppressed}
    suppressed = {p.host_id for p in problems if p.suppressed}
    lines = ["🖥 Состояние инфраструктуры", f"Узлов под мониторингом: {len(hosts)}", ""]
    buttons = []
    for host in visible:
        marker = "⚠️" if host.id in affected else "🔕" if host.id in suppressed else "✅"
        lines.append(f"{_host_title(host)}  {marker}")
        buttons.append(InlineKeyboardButton(_label(host.name, 50), callback_data=f"status:host:{host.id}"))
    lines.extend(["", f"Активных проблем: {len(active_events)}"])
    rows = [buttons[i : i + 2] for i in range(0, len(buttons), 2)]
    if total > 1:
        rows.append(pager_row("status:hosts:", page, total))
    rows.extend(
        [
            [
                InlineKeyboardButton("⚠️ Проблемы", callback_data="status:issues:0"),
                InlineKeyboardButton("🔄 Обновить", callback_data=f"status:hosts:{page}"),
            ],
            [btn_home()],
        ]
    )
    return "\n".join(lines), InlineKeyboardMarkup(rows)


def host_card(host: Host, metrics: tuple[Metric, ...], problems: tuple[Problem, ...], list_page: int) -> Screen:
    active = sorted((p for p in problems if not p.suppressed), key=lambda p: (-p.severity, -int(p.event_id)))
    sections = [_host_title(host)]
    if active:
        lead = active[0]
        issue = f"{_SEVERITY_MARKS[lead.severity]} {_STAFF_SEVERITIES[lead.severity]} · {_label(lead.name, 200)}"
        if lead.acknowledged:
            issue += " · подтверждена"
        if len(active) > 1:
            issue += f"\nДругих активных проблем: {len(active) - 1}"
        sections.append(issue)
    elif not problems:
        sections.append("✅ Активных проблем нет")
    if suppressed_count := sum(p.suppressed for p in problems):
        sections.append(f"🔕 Подавленных проблем: {suppressed_count}")
    sections.extend([metric_summary(metrics), _latest_measurement(metrics)])
    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("⚠️ Проблемы сервера", callback_data=f"status:hostissues:{host.id}:0")],
            *_metric_buttons(host, metrics),
            [
                InlineKeyboardButton("🔄 Обновить", callback_data=f"status:host:{host.id}"),
                btn_back(f"status:hosts:{list_page}", "⬅️ К серверам"),
            ],
        ]
    )
    return "\n\n".join(sections), keyboard


def _acknowledgement(issue: Problem) -> str:
    return "👁 Подтверждена" if issue.acknowledged else "⏳ Ожидает подтверждения"


def _public_problems(hosts: tuple[Host, ...], problems: tuple[Problem, ...]) -> tuple[Problem, ...]:
    highest: dict[str, Problem] = {}
    for issue in problems:
        current = highest.get(issue.host_id)
        if current is None or (issue.severity, int(issue.event_id)) > (current.severity, int(current.event_id)):
            highest[issue.host_id] = issue
    in_inventory_order = (highest[host.id] for host in hosts if host.id in highest)
    return tuple(sorted(in_inventory_order, key=lambda issue: -issue.severity))


def _public_issue(issue: Problem, host_name: str) -> str:
    severity, impact = _PUBLIC_SEVERITIES[issue.severity]
    status = _acknowledgement(issue)
    if issue.cause_eventid != "0":
        status += " · 🔗 Связана с другой проблемой."
    host_name = host_name.replace("\n", " ")
    return f"{_SEVERITY_MARKS[issue.severity]} {host_name} · {severity}\n{impact}\n{status}"


def _staff_issue(issue: Problem, location: str) -> str:
    severity = f"{_SEVERITY_MARKS[issue.severity]} {_STAFF_SEVERITIES[issue.severity]}"
    status = _acknowledgement(issue)
    if issue.suppressed:
        status = f"🔕 Подавлена · {status}"
    lines = [f"{severity} · {_label(issue.name, 220)}", location]
    if issue.source_host_name:
        lines.append(f"Источник проверки · {_label(issue.source_host_name, 80)}")
    lines.append(status)
    if issue.cause_eventid != "0":
        lines.append(f"🔗 Симптом проблемы #{issue.cause_eventid}")
    if issue.comment:
        comment_limit = 180 if issue.source_host_name else 220
        lines.append(f"💬 Комментарий: {_label(issue.comment, comment_limit)}")
    return "\n".join(lines)


def problem_list(
    hosts: tuple[Host, ...], problems: tuple[Problem, ...], requested: int, *, staff: bool, host: Host | None = None
) -> Screen:
    names = {node.id: _host_title(node) for node in hosts}
    if not staff:
        problems = _public_problems(hosts, problems)
    page, total, portion = _page(len(problems), 5, requested)
    if staff:
        title = f"⚠️ Проблемы · {_host_title(host)}" if host else "⚠️ Открытые проблемы"
        if problems:
            active = {issue.event_id for issue in problems if not issue.suppressed}
            suppressed = {issue.event_id for issue in problems if issue.suppressed}
            blocks = [f"{title}\nАктивных: {len(active)} · 🔕 Подавленных: {len(suppressed)}"]
            for issue in problems[portion]:
                location = f"#{issue.event_id}" if host else f"📍 {names[issue.host_id]} · #{issue.event_id}"
                blocks.append(_staff_issue(issue, location))
        else:
            blocks = [title, "✅ Открытых проблем нет."]
    elif problems:
        blocks = [f"🖥 Статус серверов\nСерверов с проблемами: {len(problems)}"]
        blocks.extend(_public_issue(issue, names[issue.host_id]) for issue in problems[portion])
    else:
        blocks = [HEALTHY]

    prefix = f"status:hostissues:{host.id}:" if host else "status:issues:"
    rows = _navigation(prefix, page, total)
    if host:
        rows.append([btn_back(f"status:host:{host.id}", "⬅️ К серверу")])
    elif staff:
        rows.append([btn_back("status:hosts:0", "К списку узлов")])
    rows.append([btn_home()])
    return "\n\n".join(blocks), InlineKeyboardMarkup(rows)


def metric_list(host: Host, metrics: tuple[Metric, ...], requested: int, *, category: str = "") -> Screen:
    prefix = f"status:metrics:{host.id}:"
    title = METRIC_CATEGORY_LABELS[category] if category else "📊 Подробные метрики"
    blocks = [f"{title} · {_host_title(host)}"]
    if category:
        pages = metric_pages(metrics, category)
        page, total, _ = _page(len(pages), 1, requested)
        if pages[page]:
            blocks.extend(metric_sections(pages[page]))
        else:
            blocks.append("Для этого узла нет метрик в этой категории.")
        rows = _navigation(f"{prefix}{category}:", page, total)
    else:
        rows = _metric_buttons(host, metrics)
        if not rows:
            blocks.append("Для этого узла в Zabbix нет доступных метрик из списка мониторинга.")
        rows.extend(_navigation(prefix, 0, 1))
    rows.append([btn_back(f"status:host:{host.id}", "⬅️ К серверу"), btn_home()])
    return "\n\n".join(blocks), InlineKeyboardMarkup(rows)


def notice(text: str, retry: str) -> Screen:
    return text, InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Обновить", callback_data=retry)], [btn_home()]])
