"""Bounded presentation helpers for cached Docker status data."""

from __future__ import annotations

from collections.abc import Iterable

from ...bot.ui import breadcrumbs, html_escape
from ..status.models import DockerContainerView

MAX_DOCKER_STATUS_CHARS = 600
MAX_MAIN_DOCKER_PROBLEM_CHARS = 1500
MAX_MAIN_DOCKER_PROBLEM_GROUPS = 5
MAX_DOCKER_REPORT_CHARS = 3800
_MAX_NAMES_PER_GROUP = 6
_MAX_ESCAPED_STATUS_CHARS = 320


def normalize_docker_status(value: object, *, max_chars: int = MAX_DOCKER_STATUS_CHARS) -> str:
    """Collapse whitespace, remove repeated stderr lines and bound stored status text."""
    max_chars = max(1, int(max_chars))
    raw = str(value or "")
    fragments: list[str] = []
    seen: set[str] = set()
    for raw_line in raw.splitlines() or [raw]:
        fragment = " ".join(raw_line.split())
        if not fragment or fragment in seen:
            continue
        seen.add(fragment)
        fragments.append(fragment)
    normalized = " ".join(fragments) or "н/д"
    if len(normalized) <= max_chars:
        return normalized
    return normalized[: max_chars - 1].rstrip() + "…"


def is_docker_problem(item: DockerContainerView) -> bool:
    status = normalize_docker_status(item.status_text).lower()
    return bool(
        not item.is_up
        or "unhealthy" in status
        or status == "не найден"
        or "недоступен" in status
        or status.startswith("ошибка:")
    )


def _escaped_status(value: object) -> str:
    """Escape a normalized value while bounding the resulting Telegram HTML."""
    normalized = normalize_docker_status(value)
    escaped_parts: list[str] = []
    escaped_length = 0
    for character in normalized:
        escaped = html_escape(character)
        if escaped_length + len(escaped) > _MAX_ESCAPED_STATUS_CHARS - 1:
            escaped_parts.append("…")
            break
        escaped_parts.append(escaped)
        escaped_length += len(escaped)
    return "".join(escaped_parts)


def _group_problems(
    items: Iterable[DockerContainerView],
) -> list[tuple[str, list[DockerContainerView]]]:
    groups: list[tuple[str, list[DockerContainerView]]] = []
    positions: dict[str, int] = {}
    for item in items:
        if not is_docker_problem(item):
            continue
        status = normalize_docker_status(item.status_text)
        key = status.casefold()
        position = positions.get(key)
        if position is None:
            positions[key] = len(groups)
            groups.append((status, [item]))
        else:
            groups[position][1].append(item)
    return groups


def _names_html(items: list[DockerContainerView], *, code: bool) -> str:
    visible = items[:_MAX_NAMES_PER_GROUP]
    names = ", ".join(f"<code>{html_escape(item.name)}</code>" if code else html_escape(item.name) for item in visible)
    hidden = len(items) - len(visible)
    return f"{names} и ещё {hidden}" if hidden else names


def main_docker_problem_lines(items: Iterable[DockerContainerView]) -> list[str]:
    """Return a bounded, deduplicated problem list for the main status screen."""
    groups = _group_problems(items)
    if not groups:
        return []
    lines: list[str] = []
    represented = 0
    for status, containers in groups[:MAX_MAIN_DOCKER_PROBLEM_GROUPS]:
        line = f"⚠️ {_names_html(containers, code=False)} — <i>{_escaped_status(status)}</i>"
        prospective = "\n".join([*lines, line])
        if len(prospective) > MAX_MAIN_DOCKER_PROBLEM_CHARS:
            break
        lines.append(line)
        represented += len(containers)
    total = sum(len(containers) for _status, containers in groups)
    hidden = total - represented
    if hidden:
        lines.append(f"… ещё {hidden} проблемных контейнеров — подробности в разделе Docker")
    return lines


def format_docker_report(
    server_label: str,
    items: list[DockerContainerView],
    *,
    updated_at: str = "",
) -> str:
    """Build a full cached report without exceeding Telegram's message limit."""
    lines = [f"<b>{html_escape(breadcrumbs('Статус', server_label, 'Docker'))}</b>"]
    if updated_at:
        lines.append(f"<i>обновлено {html_escape(updated_at)}</i>")
    if not items:
        lines.extend(["", "🟢 Docker доступен, отслеживаемых контейнеров нет."])
        return "\n".join(lines)

    problem_groups = _group_problems(items)
    problem_by_name = {item.name: (status, containers) for status, containers in problem_groups for item in containers}
    rendered_problem_statuses: set[str] = set()
    entries: list[tuple[int, list[str]]] = []
    for item in items:
        grouped = problem_by_name.get(item.name)
        if grouped:
            status, containers = grouped
            key = status.casefold()
            if key in rendered_problem_statuses:
                continue
            rendered_problem_statuses.add(key)
            restart_values = {normalize_docker_status(value.restarts, max_chars=40) for value in containers}
            restarts = next(iter(restart_values)) if len(restart_values) == 1 else "разные значения"
            entries.append(
                (
                    len(containers),
                    [
                        f"🔴 {_names_html(containers, code=True)} — {_escaped_status(status)}",
                        f"   перезапуски: <code>{html_escape(restarts)}</code>",
                    ],
                )
            )
            continue
        status = normalize_docker_status(item.status_text)
        entries.append(
            (
                1,
                [
                    f"🟢 <code>{html_escape(item.name)}</code> — {_escaped_status(status)}",
                    f"   перезапуски: <code>{html_escape(normalize_docker_status(item.restarts, max_chars=40))}</code>",
                ],
            )
        )

    lines.append("")
    represented = 0
    reserve_chars = 180
    for count, block in entries:
        prospective = "\n".join([*lines, *block])
        if len(prospective) + reserve_chars > MAX_DOCKER_REPORT_CHARS:
            continue
        lines.extend(block)
        represented += count
    hidden = len(items) - represented
    if hidden:
        lines.append(f"… ещё {hidden} контейнеров не показано из-за ограничения Telegram")
    lines.extend(["", "Выберите контейнер для inspect или просмотра логов:"])
    return "\n".join(lines)


__all__ = [
    "MAX_DOCKER_REPORT_CHARS",
    "MAX_DOCKER_STATUS_CHARS",
    "format_docker_report",
    "is_docker_problem",
    "main_docker_problem_lines",
    "normalize_docker_status",
]
