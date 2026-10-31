from __future__ import annotations

import re

SERVER_KEY_PATTERN = r"[a-z0-9_-]{1,12}"

_CONTAINER_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.\-]{0,62}$")
_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


def is_container_name(value: str) -> bool:
    return _CONTAINER_NAME_RE.fullmatch(value) is not None


def is_uuid(value: str) -> bool:
    return _UUID_RE.fullmatch(value) is not None


__all__ = [
    "SERVER_KEY_PATTERN",
    "is_container_name",
    "is_uuid",
]
