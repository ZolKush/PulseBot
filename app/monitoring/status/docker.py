"""Refresh the shared Docker inventory for one server."""

from __future__ import annotations

import asyncio
import time
from datetime import datetime

from ...config import TZ, ServerTarget, logger
from ...storage import set_docker_status_cache
from ..docker.local import docker_containers
from .cache import docker_failure_rows, invalidate_status_cache
from .common import exc_brief

_REFRESH_LOCKS: dict[str, asyncio.Lock] = {}


async def refresh_docker_status(server: ServerTarget, *, source: str = "manual") -> None:
    """Persist a fresh inventory, including failures, before releasing the lock."""
    if server.mode == "panel":
        return
    async with _REFRESH_LOCKS.setdefault(server.key, asyncio.Lock()):
        started = time.monotonic()
        try:
            containers = await docker_containers(server.monitor_containers)
        except Exception as exc:
            logger.exception("Docker status refresh failed for server=%s", server.key)
            containers = docker_failure_rows(server, f"ошибка: {exc_brief(exc)}")
        payload = {
            "updated_at": datetime.now(TZ).isoformat(),
            "containers": [
                [str(name), bool(is_up), str(status), str(restarts)] for name, is_up, status, restarts in containers
            ],
        }
        await set_docker_status_cache(server.key, payload)
        invalidate_status_cache(server.key)
        problem_count = sum(
            1
            for _, is_up, status, _ in containers
            if not is_up or "unhealthy" in str(status).lower() or str(status).lower() == "не найден"
        )
        logger.info(
            "Docker status refreshed source=%s server=%s containers=%s problems=%s duration_ms=%s",
            source,
            server.key,
            len(containers),
            problem_count,
            round((time.monotonic() - started) * 1000),
            extra={
                "action": "docker_refresh",
                "source": source,
                "server_key": server.key,
                "total": len(containers),
                "problems": problem_count,
                "duration_ms": round((time.monotonic() - started) * 1000),
            },
        )
