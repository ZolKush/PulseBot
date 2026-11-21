"""Telegram screens for the current Zabbix inventory, problems and latest values."""

from __future__ import annotations

from datetime import datetime

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from ...bot.ui import btn_back, btn_home, clip_text, pager_row
from ...config import TZ
from ..zabbix import Host, Metric, Problem

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


def host_list(hosts: tuple[Host, ...], requested: int) -> Screen:
    page, total, portion = _page(len(hosts), 10, requested)
    rows = [
        [InlineKeyboardButton(_label(host.name, 60), callback_data=f"status:metrics:{host.id}:0")]
        for host in hosts[portion]
    ]
    rows.append([InlineKeyboardButton("Проблемы Zabbix", callback_data="status:issues:0")])
    rows += _navigation("status:hosts:", page, total)
    rows.append([btn_home()])
    return f"Узлы Zabbix · {len(hosts)}\nВыберите узел для просмотра метрик.", InlineKeyboardMarkup(rows)


def problem_list(hosts: tuple[Host, ...], problems: tuple[Problem, ...], requested: int, *, staff: bool) -> Screen:
    names = {host.id: host.name for host in hosts}
    page, total, portion = _page(len(problems), 5, requested)
    blocks: list[str] = []
    for issue in problems[portion]:
        host = _label(names[issue.host_id], 128)
        status = "Подтверждена" if issue.acknowledged else "Ожидает подтверждения"
        if staff:
            lines = [
                f"{host} · #{issue.event_id}",
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
            lines = [host, _PUBLIC_SEVERITIES[issue.severity], f"Статус: {status.lower()}."]
            if issue.cause_eventid != "0":
                lines.append("Связана с другой проблемой.")
        blocks.append("\n".join(lines))
    rows = _navigation("status:issues:", page, total)
    if staff:
        rows.append([btn_back("status:hosts:0", "К списку узлов")])
    rows.append([btn_home()])
    text = "\n\n".join(blocks) if blocks else ("В Zabbix нет открытых проблем." if staff else HEALTHY)
    return text, InlineKeyboardMarkup(rows)


def metric_list(host: Host, metrics: tuple[Metric, ...], requested: int) -> Screen:
    page, total, portion = _page(len(metrics), 5, requested)
    blocks = [f"{_label(host.name, 128)} · последние значения Zabbix"]
    for metric in metrics[portion]:
        label = _label(metric.name, 100)
        key = _label(metric.key, 230)
        value = "нет данных" if metric.value is None else _label(f"{metric.value} {metric.units}", 190)
        reading = f"{label}\n{key}\n{value}" if label and label != key else f"{key}\n{value}"
        if metric.value is not None and metric.lastclock:
            try:
                stamp = datetime.fromtimestamp(metric.lastclock, TZ)
                reading += f" · {stamp:%d.%m.%Y %H:%M %Z}"
            except (OverflowError, OSError, ValueError):
                reading += " · время измерения неизвестно"
        blocks.append(reading)
    if not metrics:
        blocks.append("Для этого узла в Zabbix нет доступных метрик из списка мониторинга.")
    rows = _navigation(f"status:metrics:{host.id}:", page, total)
    rows.append([btn_back("status:hosts:0", "К списку узлов"), btn_home()])
    return "\n\n".join(blocks), InlineKeyboardMarkup(rows)


def notice(text: str, retry: str) -> Screen:
    return text, InlineKeyboardMarkup([[InlineKeyboardButton("Обновить", callback_data=retry)], [btn_home()]])
