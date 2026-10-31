"""Compose local and RemnaWave inputs into status snapshots."""

from __future__ import annotations

import asyncio
from typing import cast

from telegram import Update

from ...bot.guards import is_admin
from ...bot.ui import now_str
from ...config import ServerTarget
from ..remnawave import (
    NodeMetrics,
    format_memory_bytes,
    format_uptime_seconds,
    get_metrics_snapshot,
)
from ..system.metrics import check_uptime, disk_root, meminfo
from ..system.ufw import ufw_status_basic, ufw_summary_for_admin
from .cache import (
    cached_snapshot,
    daily_cache_for,
    docker_views_from_cache,
    tls_views,
)
from .common import (
    default_server_target,
    format_iso_short,
    get_server_target,
    safe_nonnegative_int,
    server_flag,
)
from .dns import dns_payload_from_cache_or_empty
from .models import StatusSnapshot
from .source_policy import node_metrics_problem, server_uses_metrics

StatusPayload = tuple[
    object,
    object,
    object,
    list[tuple[str, bool, str, str]],
    str,
    list[str],
    list[str],
    list[str],
]


async def build_status_payload_local(
    admin_mode: bool,
    server: ServerTarget,
) -> StatusPayload:
    uptime, memory, disk, ufw_data = await asyncio.gather(
        check_uptime(),
        meminfo(),
        disk_root(),
        ufw_summary_for_admin() if admin_mode else ufw_status_basic(),
        return_exceptions=True,
    )
    if isinstance(uptime, Exception):
        uptime = "н/д"
    if isinstance(memory, Exception):
        memory = "н/д"
    if isinstance(disk, Exception):
        disk = "н/д"
    if isinstance(ufw_data, Exception):
        ufw_data = ("н/д", [], [], []) if admin_mode else "н/д"
    if admin_mode:
        ufw_state, allow, deny, reject = cast(
            tuple[str, list[str], list[str], list[str]],
            ufw_data,
        )
    else:
        ufw_state = str(ufw_data)
        allow, deny, reject = [], [], []
    return uptime, memory, disk, [], ufw_state, allow, deny, reject


def _dns_snapshot_values(
    server: ServerTarget,
) -> tuple[int, int, int, int, list[str]]:
    payload = dns_payload_from_cache_or_empty(server)
    raw_details = payload.get("details", [])
    details = [str(value) for value in raw_details] if isinstance(raw_details, list) else []
    return (
        safe_nonnegative_int(payload.get("ok")),
        safe_nonnegative_int(payload.get("total"), len(server.check_a_domains)),
        safe_nonnegative_int(payload.get("bad")),
        safe_nonnegative_int(payload.get("unknown")),
        details,
    )


def _mixed_unavailable_snapshot(
    server: ServerTarget,
    *,
    admin_mode: bool,
    node_online: bool | None,
    metrics_error: str,
    last_seen_text: str,
) -> StatusSnapshot:
    dns_ok, dns_total, dns_bad, dns_unknown, dns_details = _dns_snapshot_values(server)
    _, _, ufw_updated, _, _, _, disk_updated, raw_updated = daily_cache_for(server)
    return StatusSnapshot(
        title="🧭 Статус сервера",
        server_label=server.label,
        server_flag=server_flag(server),
        now_text=now_str(),
        uptime_text="н/д",
        memory_raw="н/д",
        disk_raw="н/д",
        ufw_state="н/д",
        dns_ok_domains=dns_ok,
        dns_total_domains=dns_total,
        dns_bad_domains=dns_bad,
        dns_unknown_domains=dns_unknown,
        dns_error_details=dns_details,
        containers=docker_views_from_cache(server),
        tls_certificates=tls_views(server.key, admin_mode=admin_mode),
        admin_mode=admin_mode,
        source_mode="mixed",
        node_online=node_online,
        online_users=None,
        last_seen_text=last_seen_text or format_iso_short(raw_updated),
        metrics_error=metrics_error,
        disk_updated_at_text=disk_updated,
        ufw_updated_at_text=ufw_updated,
        show_containers_block=server.mode != "panel",
    )


async def build_status_snapshot_mixed(
    update: Update,
    server: ServerTarget,
) -> StatusSnapshot:
    admin_mode = is_admin(update)
    metrics = await get_metrics_snapshot()
    node: NodeMetrics | None = metrics.get(server.remnawave_uuid)
    metrics_error = "" if metrics.ok else metrics.error
    node_problem = node_metrics_problem(node)
    if metrics_error or node_problem:
        error = metrics_error or node_problem
        fetched = metrics.fetched_at.strftime("%d.%m %H:%M") if metrics_error and metrics.fetched_at else ""
        return _mixed_unavailable_snapshot(
            server,
            admin_mode=admin_mode,
            node_online=None,
            metrics_error=error,
            last_seen_text=fetched,
        )
    if node is None:
        return _mixed_unavailable_snapshot(
            server,
            admin_mode=admin_mode,
            node_online=None,
            metrics_error="метрики настроенной ноды отсутствуют",
            last_seen_text="",
        )
    if not node.is_online:
        return _mixed_unavailable_snapshot(
            server,
            admin_mode=admin_mode,
            node_online=False,
            metrics_error="",
            last_seen_text="",
        )

    dns_ok, dns_total, dns_bad, dns_unknown, dns_details = _dns_snapshot_values(server)
    disk, ufw_state, ufw_updated, allow, deny, reject, disk_updated, _ = daily_cache_for(server)
    return StatusSnapshot(
        title="🧭 Статус сервера",
        server_label=server.label,
        server_flag=server_flag(server),
        now_text=now_str(),
        uptime_text=format_uptime_seconds(node.uptime_s),
        memory_raw=format_memory_bytes(node.mem_used, node.mem_total),
        disk_raw=disk,
        ufw_state=ufw_state,
        dns_ok_domains=dns_ok,
        dns_total_domains=dns_total,
        dns_bad_domains=dns_bad,
        dns_unknown_domains=dns_unknown,
        dns_error_details=dns_details,
        ufw_allow=list(allow),
        ufw_deny=list(deny),
        ufw_reject=list(reject),
        containers=docker_views_from_cache(server),
        tls_certificates=tls_views(server.key, admin_mode=admin_mode),
        admin_mode=admin_mode,
        source_mode="mixed",
        node_online=True,
        online_users=node.online_users,
        last_seen_text="",
        disk_updated_at_text=disk_updated,
        ufw_updated_at_text=ufw_updated,
        show_containers_block=server.mode != "panel",
    )


async def build_status_snapshot_uncached(
    update: Update,
    server: ServerTarget,
) -> StatusSnapshot:
    if server_uses_metrics(server):
        return await build_status_snapshot_mixed(update, server)

    admin_mode = is_admin(update)
    payload = await build_status_payload_local(admin_mode, server)
    uptime, memory, disk, _, ufw_state, allow, deny, reject = payload
    dns_ok, dns_total, dns_bad, dns_unknown, dns_details = _dns_snapshot_values(server)
    return StatusSnapshot(
        title="🧭 Статус сервера",
        server_label=server.label,
        server_flag=server_flag(server),
        now_text=now_str(),
        uptime_text=str(uptime),
        memory_raw=str(memory),
        disk_raw=str(disk),
        ufw_state=str(ufw_state),
        dns_ok_domains=dns_ok,
        dns_total_domains=dns_total,
        dns_bad_domains=dns_bad,
        dns_unknown_domains=dns_unknown,
        dns_error_details=dns_details,
        ufw_allow=list(allow),
        ufw_deny=list(deny),
        ufw_reject=list(reject),
        containers=docker_views_from_cache(server),
        tls_certificates=tls_views(server.key, admin_mode=admin_mode),
        admin_mode=admin_mode,
        source_mode="local",
    )


async def build_status_snapshot(
    update: Update,
    server: ServerTarget,
) -> StatusSnapshot:
    admin_mode = is_admin(update)
    return await cached_snapshot(
        server,
        admin_mode,
        lambda: build_status_snapshot_uncached(update, server),
    )


async def build_status_snapshot_and_server(
    update: Update,
    server_key: str | None,
) -> tuple[StatusSnapshot | None, ServerTarget | None]:
    server = get_server_target(server_key) if server_key else default_server_target()
    if not server:
        return None, None
    return await build_status_snapshot(update, server), server


__all__ = [
    "build_status_snapshot",
    "build_status_snapshot_and_server",
    "build_status_snapshot_uncached",
]
