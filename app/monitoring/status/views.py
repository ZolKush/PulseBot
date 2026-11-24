"""Telegram screens for the current Zabbix inventory, problems and latest values."""

from __future__ import annotations

import re
from datetime import datetime

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from ...bot.ui import btn_back, btn_home, clip_text, pager_row
from ...config import TZ
from ..zabbix import Host, Metric, Problem
from .metrics import metric_detail, metric_summary

Screen = tuple[str, InlineKeyboardMarkup]
HEALTHY = "Все системы работают штатно, проблем не замечено."
UNAVAILABLE = "Сейчас не удалось получить состояние серверов. Попробуйте обновить статус позже."

_PUBLIC_SEVERITIES = {
    2: "Предупреждение. В работе сервера замечено отклонение.",
    3: "Средняя важность. Отдельные функции могут работать с перебоями.",
    4: "Высокая важность. Возможны существенные перебои в работе сервера.",
    5: "Критическая проблема. Сервис может быть недоступен.",
}
_STAFF_SEVERITIES = ("Не классифицирована", "Информация", "Предупреждение", "Средняя", "Высокая", "Чрезвычайная")


def _label(value: str, budget: int) -> str:
    return clip_text(" ".join(value.split()), limit=budget)


def _page(length: int, size: int, requested: int) -> tuple[int, int, slice]:
    total = max(1, (length + size - 1) // size)
    page = max(0, min(requested, total - 1))
    return page, total, slice(page * size, (page + 1) * size)


def _navigation(prefix: str, page: int, total: int) -> list[list[InlineKeyboardButton]]:
    rows = [pager_row(prefix, page, total)] if total > 1 else []
    rows.append([InlineKeyboardButton("Обновить", callback_data=f"{prefix}{page}")])
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
    sections = [_host_title(host), metric_summary(metrics)]
    if active:
        lead = active[0]
        issue = f"⚠️ {_STAFF_SEVERITIES[lead.severity]} · {_label(lead.name, 200)}"
        if lead.acknowledged:
            issue += " · подтверждена"
        if len(active) > 1:
            issue += f"\nДругих активных проблем: {len(active) - 1}"
        sections.append(issue)
    elif not problems:
        sections.append("✅ Активных проблем нет")
    if suppressed_count := sum(p.suppressed for p in problems):
        sections.append(f"🔕 Подавленных проблем: {suppressed_count}")
    sections.append(f"Сводка обновлена в {datetime.now(TZ):%H:%M %Z}")
    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📊 Подробные метрики", callback_data=f"status:metrics:{host.id}:0")],
            [InlineKeyboardButton("⚠️ Проблемы сервера", callback_data=f"status:hostissues:{host.id}:0")],
            [
                InlineKeyboardButton("🔄 Обновить", callback_data=f"status:host:{host.id}"),
                btn_back(f"status:hosts:{list_page}", "⬅️ К серверам"),
            ],
        ]
    )
    return "\n\n".join(sections), keyboard


def problem_list(
    hosts: tuple[Host, ...], problems: tuple[Problem, ...], requested: int, *, staff: bool, host: Host | None = None
) -> Screen:
    names = {host.id: host.name for host in hosts}
    page, total, portion = _page(len(problems), 5, requested)
    blocks: list[str] = []
    for issue in problems[portion]:
        name = _label(names[issue.host_id], 128)
        status = "Подтверждена" if issue.acknowledged else "Ожидает подтверждения"
        if staff:
            lines = [
                f"{name} · #{issue.event_id}",
                f"{_STAFF_SEVERITIES[issue.severity]} · {_label(issue.name, 220)}",
                status,
            ]
            if issue.suppressed:
                lines.append("Подавлена")
            if issue.cause_eventid != "0":
                lines.append(f"Симптом проблемы #{issue.cause_eventid}")
            if issue.comment:
                lines.append(f"Комментарий: {_label(issue.comment, 220)}")
        else:
            lines = [name, _PUBLIC_SEVERITIES[issue.severity], f"Статус: {status.lower()}."]
            if issue.cause_eventid != "0":
                lines.append("Связана с другой проблемой.")
        blocks.append("\n".join(lines))
    prefix = f"status:hostissues:{host.id}:" if host else "status:issues:"
    rows = _navigation(prefix, page, total)
    if host:
        rows.append([btn_back(f"status:host:{host.id}", "⬅️ К серверу")])
    elif staff:
        rows.append([btn_back("status:hosts:0", "К списку узлов")])
    rows.append([btn_home()])
    text = "\n\n".join(blocks) if blocks else ("В Zabbix нет открытых проблем." if staff else HEALTHY)
    if host:
        text = f"⚠️ Проблемы · {_host_title(host)}\n\n" + ("\n\n".join(blocks) or "Открытых проблем нет.")
    return text, InlineKeyboardMarkup(rows)


def metric_list(host: Host, metrics: tuple[Metric, ...], requested: int) -> Screen:
    page, total, portion = _page(len(metrics), 5, requested)
    blocks = [f"📊 Подробные метрики · {_host_title(host)}"]
    blocks.extend(metric_detail(metric) for metric in metrics[portion])
    if not metrics:
        blocks.append("Для этого узла в Zabbix нет доступных метрик из списка мониторинга.")
    rows = _navigation(f"status:metrics:{host.id}:", page, total)
    rows.append([btn_back(f"status:host:{host.id}", "⬅️ К серверу"), btn_home()])
    return "\n\n".join(blocks), InlineKeyboardMarkup(rows)


def notice(text: str, retry: str) -> Screen:
    return text, InlineKeyboardMarkup([[InlineKeyboardButton("Обновить", callback_data=retry)], [btn_home()]])
