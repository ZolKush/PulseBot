"""Local disk/UFW collection for direct host deployments."""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import cast

from ...config import TZ, ServerTarget
from ...monitoring.system.metrics import disk_root
from ...monitoring.system.ufw import ufw_summary_for_admin
from .cache import local_refresh_lock
from .common import exc_brief


async def collect_disk_ufw_uncached(server: ServerTarget, *, admin_mode: bool) -> dict[str, object]:
    if server.mode != "local":
        return {"ok": False, "error": "Disk/UFW доступны только для локального сервера"}
    local_disk, ufw_data = await asyncio.gather(
        disk_root(),
        ufw_summary_for_admin(),
        return_exceptions=True,
    )
    if isinstance(local_disk, Exception):
        return {"ok": False, "error": exc_brief(local_disk), "updated_at": datetime.now(TZ).isoformat()}
    if str(local_disk).strip().lower() in {"", "н/д"}:
        return {
            "ok": False,
            "error": "чтение локального диска завершилось ошибкой",
            "updated_at": datetime.now(TZ).isoformat(),
        }
    if isinstance(ufw_data, Exception):
        return {"ok": False, "error": exc_brief(ufw_data), "updated_at": datetime.now(TZ).isoformat()}
    ufw_state, allow, deny, reject = cast(tuple[str, list[str], list[str], list[str]], ufw_data)
    return {
        "ok": True,
        "disk_raw": str(local_disk),
        "ufw_state": str(ufw_state),
        "ufw_allow": list(allow),
        "ufw_deny": list(deny),
        "ufw_reject": list(reject),
        "updated_at": datetime.now(TZ).isoformat(),
    }


async def collect_disk_ufw(server: ServerTarget, *, admin_mode: bool) -> dict[str, object]:
    async with local_refresh_lock(server.key):
        return await collect_disk_ufw_uncached(server, admin_mode=admin_mode)


__all__ = ["collect_disk_ufw"]
