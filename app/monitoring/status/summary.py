"""Compact status chips and failure-only detail blocks."""

from __future__ import annotations

from datetime import datetime

from ...bot.ui import html_escape, section
from .models import StatusSnapshot

MAX_DNS_DETAIL_LINES = 10


def dns_detail_line(snapshot: StatusSnapshot) -> str | None:
    total = int(snapshot.dns_total_domains or 0)
    ok = int(snapshot.dns_ok_domains or 0)
    bad = int(snapshot.dns_bad_domains or 0)
    unknown = int(snapshot.dns_unknown_domains or 0)
    if total <= 0:
        return "🌐 DNS: проверка не настроена"
    if ok == 0 and bad == 0 and unknown == 0:
        return f"🌐 DNS: нет свежих данных ({total} доменов)"
    if bad == 0 and unknown == 0:
        return None
    parts = []
    if bad:
        parts.append(f"ошибки: {bad}")
    if unknown:
        parts.append(f"нет ответа: {unknown}")
    return "🌐 DNS: " + ", ".join(parts)


def dns_failure_block(snapshot: StatusSnapshot) -> list[str]:
    if not snapshot.dns_error_details:
        return []
    lines: list[str] = ["", section("DNS — проблемы", "🌐")]
    details = list(snapshot.dns_error_details)
    lines.extend(details[:MAX_DNS_DETAIL_LINES])
    hidden = len(details) - MAX_DNS_DETAIL_LINES
    if hidden > 0:
        lines.append(f"… ещё {hidden}")
    return lines


def _dns_chip(snapshot: StatusSnapshot) -> str:
    total = int(snapshot.dns_total_domains or 0)
    ok = int(snapshot.dns_ok_domains or 0)
    bad = int(snapshot.dns_bad_domains or 0)
    unknown = int(snapshot.dns_unknown_domains or 0)
    if total <= 0 or (ok == 0 and bad == 0 and unknown == 0):
        return "🌐 DNS ⚠️"
    emoji = "🟢" if (bad == 0 and unknown == 0) else ("🔴" if bad else "⚠️")
    return f"🌐 DNS {emoji} {ok}/{total}"


def _docker_chip(snapshot: StatusSnapshot) -> str:
    running = stopped = unhealthy = missing = 0
    unavailable = False
    for container in snapshot.containers:
        status = (container.status_text or "").strip().lower()
        if "docker недоступен" in status or status.startswith("ошибка:"):
            unavailable = True
            continue
        if status == "не найден":
            missing += 1
            continue
        running += int(container.is_up)
        stopped += int(not container.is_up)
        unhealthy += int("unhealthy" in status)
    if unavailable:
        return "🐳 Docker ⚠️ н/д"
    degraded = bool(stopped or unhealthy or missing)
    healthy = max(0, running - unhealthy)
    return f"🐳 Docker {'🔴' if degraded else '🟢'} {healthy}/{running + stopped + missing}"


def _tls_chip(snapshot: StatusSnapshot) -> str:
    certificates = snapshot.tls_certificates
    if not certificates:
        return "🔐 TLS ⚠️ нет данных"
    ok = sum(1 for item in certificates if item.status == "ok")
    critical = sum(1 for item in certificates if item.status in {"expired", "invalid"})
    warning = len(certificates) - ok - critical
    emoji = "🔴" if critical else ("⚠️" if warning else "🟢")
    return f"🔐 TLS {emoji} {ok}/{len(certificates)}"


def summary_chips_line(snapshot: StatusSnapshot, *, ufw_emoji: str) -> str:
    if not snapshot.show_containers_block:
        return "\n".join([_dns_chip(snapshot), _tls_chip(snapshot)])
    ufw = f"🛡 UFW {ufw_emoji}"
    return "\n".join([f"{_dns_chip(snapshot)}   {ufw}", _docker_chip(snapshot), _tls_chip(snapshot)])


def _format_tls_expiry(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return value or "н/д"
    return parsed.strftime("%d.%m.%Y %H:%M %Z").strip()


def tls_failure_block(snapshot: StatusSnapshot) -> list[str]:
    if not snapshot.admin_mode:
        return []
    failures = [item for item in snapshot.tls_certificates if item.status != "ok"]
    if not failures:
        return []
    lines = ["", section("TLS — проблемы", "🔐")]
    for item in failures:
        emoji = "🔴" if item.status in {"expired", "invalid"} else "⚠️"
        if item.status == "expired":
            state = "просрочен"
        elif item.status == "expiring":
            state = f"истекает через {max(0, int(item.remaining_seconds) // 3600)} ч."
        elif item.status == "invalid":
            state = "невалиден"
        else:
            state = "проверка не удалась"
        lines.append(f"{emoji} <code>{html_escape(item.domain)}:{item.port}</code> — <b>{html_escape(state)}</b>")
        if item.not_after:
            lines.append(f"   до <code>{html_escape(_format_tls_expiry(item.not_after))}</code>")
        if item.error:
            lines.append(f"   <i>{html_escape(item.error[:300])}</i>")
    return lines


__all__ = ["dns_detail_line", "dns_failure_block", "summary_chips_line", "tls_failure_block"]
