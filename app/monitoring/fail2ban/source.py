"""Select local Fail2Ban log access for a configured server."""

from __future__ import annotations

from zoneinfo import ZoneInfo

from ...config import SERVERS, TZ, ServerTarget, logger
from .local import (
    fail2ban_identity_with_sudo_async,
    read_text_range_with_sudo_async,
)
from .models import FileIdentity, FileIdentityChangedError, FileRangeRead


def get_server(server_key: str) -> ServerTarget:
    return SERVERS[server_key]


def first_server_key() -> str:
    return next(iter(SERVERS.keys()), "")


def server_timezone(server_key: str) -> ZoneInfo:
    server = get_server(server_key)
    try:
        return ZoneInfo(server.fail2ban_timezone or str(TZ))
    except Exception:
        logger.warning(
            "Invalid fail2ban timezone for server=%s; using bot timezone",
            server_key,
        )
        return TZ


async def file_identity(
    server_key: str,
    path: str,
) -> FileIdentity | None:
    server = get_server(server_key)
    if server.mode == "panel":
        return None
    return await fail2ban_identity_with_sudo_async(path)


async def try_file_identity(
    server_key: str,
    path: str,
) -> FileIdentity | None:
    try:
        return await file_identity(server_key, path)
    except (FileNotFoundError, PermissionError):
        return None


async def read_range(
    server_key: str,
    path: str,
    offset: int,
    limit: int,
    expected_identity: FileIdentity,
) -> FileRangeRead:
    server = get_server(server_key)
    if server.mode == "panel":
        raise PermissionError("Fail2ban недоступен в режиме панели")
    result = await read_text_range_with_sudo_async(path, offset, limit)
    if not result.identity.same_file_as(expected_identity):
        raise FileIdentityChangedError(f"fail2ban log changed before read: {path}")
    return result


__all__ = ["first_server_key", "get_server", "server_timezone"]
