"""Scheduled refresh jobs for Docker, DNS and node disk/UFW status."""

from __future__ import annotations

import asyncio
import time

from telegram.ext import ContextTypes

from ...config import BOT_MODE, SERVERS, ServerTarget, logger
from ...storage import (
    set_daily_node_status_cache,
    set_dns_status_cache,
)
from .cache import invalidate_status_cache
from .dns import build_dns_status_payload_live
from .docker import refresh_docker_status
from .local import collect_disk_ufw

DOCKER_STATUS_REFRESH_INTERVAL_SEC = 6 * 60 * 60
DOCKER_STATUS_STARTUP_DELAY_SEC = 15


async def docker_status_refresh(
    _context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Refresh the persistent Docker inventory used by every status screen."""
    semaphore = asyncio.Semaphore(4)

    async def refresh(server: ServerTarget) -> None:
        async with semaphore:
            try:
                await refresh_docker_status(server, source="scheduled")
            except Exception:
                logger.exception(
                    "Docker status cache write failed for server=%s",
                    server.key,
                )

    await asyncio.gather(*(refresh(server) for server in SERVERS.values() if server.mode != "panel"))


async def dns_daily_refresh(
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    job_name = str(getattr(getattr(context, "job", None), "name", "") or "")
    source = "startup" if "startup" in job_name else "scheduled"
    semaphore = asyncio.Semaphore(4)

    async def refresh(server: ServerTarget) -> None:
        async with semaphore:
            started = time.monotonic()
            try:
                payload = await build_dns_status_payload_live(server)
                await set_dns_status_cache(server.key, payload)
                invalidate_status_cache(server.key)
                logger.info(
                    "DNS status refreshed source=%s server=%s ok=%s bad=%s unknown=%s total=%s duration_ms=%s",
                    source,
                    server.key,
                    payload.get("ok"),
                    payload.get("bad"),
                    payload.get("unknown"),
                    payload.get("total"),
                    round((time.monotonic() - started) * 1000),
                    extra={
                        "action": "dns_refresh",
                        "source": source,
                        "server_key": server.key,
                        "total": payload.get("total"),
                        "ok": payload.get("ok"),
                        "duration_ms": round((time.monotonic() - started) * 1000),
                    },
                )
            except Exception:
                logger.exception(
                    "DNS status refresh failed for server=%s",
                    server.key,
                )

    await asyncio.gather(*(refresh(server) for server in SERVERS.values()))


async def daily_node_status_refresh(
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if BOT_MODE != "mixed":
        return

    semaphore = asyncio.Semaphore(4)

    async def refresh(server: ServerTarget) -> None:
        async with semaphore:
            started = time.monotonic()
            try:
                payload = await collect_disk_ufw(server, admin_mode=True)
                if payload.get("ok"):
                    await set_daily_node_status_cache(server.key, payload)
                    invalidate_status_cache(server.key)
                logger.info(
                    "Daily node status refreshed source=scheduled server=%s ok=%s duration_ms=%s",
                    server.key,
                    payload.get("ok"),
                    round((time.monotonic() - started) * 1000),
                    extra={
                        "action": "daily_node_status_refresh",
                        "source": "scheduled",
                        "server_key": server.key,
                        "ok": bool(payload.get("ok")),
                        "duration_ms": round((time.monotonic() - started) * 1000),
                    },
                )
            except Exception:
                logger.exception(
                    "Daily node status refresh failed for server=%s",
                    server.key,
                )

    await asyncio.gather(*(refresh(server) for server in SERVERS.values() if server.mode != "panel"))


__all__ = [
    "DOCKER_STATUS_REFRESH_INTERVAL_SEC",
    "DOCKER_STATUS_STARTUP_DELAY_SEC",
    "daily_node_status_refresh",
    "dns_daily_refresh",
    "docker_status_refresh",
]
