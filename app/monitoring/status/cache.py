"""In-memory and persistent status cache projections."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable

from ...config import STATUS_CACHE_TTL_SEC, ServerTarget
from ...storage import (
    get_daily_node_status_cache,
    get_docker_status_cache,
)
from ..docker.presentation import normalize_docker_status
from ..tls.service import tls_snapshot_for_server
from .common import format_iso_short
from .models import DockerContainerView, StatusSnapshot, TLSCertificateView

_STATUS_CACHE: dict[tuple[str, bool], tuple[float, StatusSnapshot]] = {}
_STATUS_LOCKS: dict[tuple[str, bool], asyncio.Lock] = {}
_STATUS_GENERATIONS: dict[str, int] = {}
_LOCAL_REFRESH_LOCKS: dict[str, asyncio.Lock] = {}


def docker_failure_rows(
    server: ServerTarget,
    status_text: str,
) -> list[tuple[str, bool, str, str]]:
    names = [name for name in server.monitor_containers if name]
    return [(name, False, status_text, "-") for name in names] or [
        ("Docker API", False, status_text, "-"),
    ]


def docker_views_from_cache(server: ServerTarget) -> list[DockerContainerView]:
    if server.mode == "panel":
        return []
    payload = get_docker_status_cache(server.key)
    raw_containers = payload.get("containers") if isinstance(payload, dict) else None
    if not isinstance(raw_containers, list):
        raw_containers = docker_failure_rows(server, "docker недоступен")
    result: list[DockerContainerView] = []
    for item in raw_containers:
        if not isinstance(item, (list, tuple)) or len(item) < 3:
            continue
        name = str(item[0] or "").strip()
        if not name:
            continue
        result.append(
            DockerContainerView(
                name=name,
                is_up=bool(item[1]),
                status_text=normalize_docker_status(item[2]),
                restarts=str(item[3] if len(item) >= 4 else "-"),
            )
        )
    return result


def tls_views(server_key: str, *, admin_mode: bool) -> list[TLSCertificateView]:
    _ = admin_mode
    result: list[TLSCertificateView] = []
    for item in tls_snapshot_for_server(server_key):
        try:
            port = int(item.get("port", 443) or 443)
            remaining_seconds = int(item.get("remaining_seconds", 0) or 0)
        except (TypeError, ValueError, OverflowError):
            continue
        result.append(
            TLSCertificateView(
                domain=str(item.get("domain") or "-"),
                port=port,
                status=str(item.get("status") or "error"),
                primary_port=int(item.get("primary_port", port) or port),
                fallback_ports=tuple(int(value) for value in item.get("fallback_ports", []) if str(value).isdigit()),
                used_fallback=bool(item.get("used_fallback", False)),
                not_after=str(item.get("not_after") or ""),
                remaining_seconds=remaining_seconds,
                hostname_valid=bool(item.get("hostname_valid", False)),
                trust_valid=bool(item.get("trust_valid", False)),
                error=str(item.get("error") or ""),
                checked_at=str(item.get("checked_at") or ""),
                last_attempt_at=str(item.get("last_attempt_at") or item.get("checked_at") or ""),
                last_success_at=str(item.get("last_success_at") or ""),
                attempt_errors=tuple(str(value) for value in item.get("attempt_errors", []) if str(value).strip()),
            )
        )
    return result


def daily_cache_for(
    server: ServerTarget,
) -> tuple[str, str, str, list[str], list[str], list[str], str, str]:
    if server.mode == "panel":
        return "н/д", "н/д", "", [], [], [], "", ""
    raw = get_daily_node_status_cache(server.key) or {}
    disk = str(raw.get("disk_raw") or "").strip() or "н/д"
    ufw_state = str(raw.get("ufw_state") or "").strip() or "н/д"
    allow_raw = raw.get("ufw_allow")
    deny_raw = raw.get("ufw_deny")
    reject_raw = raw.get("ufw_reject")
    allow = [str(value) for value in allow_raw if str(value).strip()] if isinstance(allow_raw, list) else []
    deny = [str(value) for value in deny_raw if str(value).strip()] if isinstance(deny_raw, list) else []
    reject = [str(value) for value in reject_raw if str(value).strip()] if isinstance(reject_raw, list) else []
    updated_text = format_iso_short(raw.get("updated_at"))
    return (
        disk,
        ufw_state,
        updated_text,
        allow,
        deny,
        reject,
        updated_text,
        str(raw.get("updated_at") or ""),
    )


def invalidate_status_cache(server_key: str) -> None:
    _STATUS_GENERATIONS[server_key] = _STATUS_GENERATIONS.get(server_key, 0) + 1
    for key in [key for key in _STATUS_CACHE if key[0] == server_key]:
        _STATUS_CACHE.pop(key, None)


async def cached_snapshot(
    server: ServerTarget,
    admin_mode: bool,
    loader: Callable[[], Awaitable[StatusSnapshot]],
) -> StatusSnapshot:
    cache_key = (server.key, admin_mode)
    cached = _STATUS_CACHE.get(cache_key)
    now = time.monotonic()
    if cached and now - cached[0] < STATUS_CACHE_TTL_SEC:
        return cached[1]
    lock = _STATUS_LOCKS.setdefault(cache_key, asyncio.Lock())
    async with lock:
        cached = _STATUS_CACHE.get(cache_key)
        now = time.monotonic()
        if cached and now - cached[0] < STATUS_CACHE_TTL_SEC:
            return cached[1]
        generation = _STATUS_GENERATIONS.get(server.key, 0)
        snapshot = await loader()
        # An invalidation also retires loads started before the refresh.
        if generation == _STATUS_GENERATIONS.get(server.key, 0):
            _STATUS_CACHE[cache_key] = (time.monotonic(), snapshot)
        return snapshot


def local_refresh_lock(server_key: str) -> asyncio.Lock:
    return _LOCAL_REFRESH_LOCKS.setdefault(server_key, asyncio.Lock())
