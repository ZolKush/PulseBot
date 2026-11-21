from __future__ import annotations

from pathlib import Path


def resolve_path(value: str, base: Path) -> str:
    raw = (value or "").strip()
    if not raw:
        return str(base)
    path = Path(raw)
    return str(path if path.is_absolute() else (base / path))


__all__ = ["resolve_path"]
