"""Prepare copied operator configuration for a Zabbix-based Docker deployment."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class DeploymentPreparationError(ValueError):
    """A preparation failure whose message contains no configuration values."""


def _without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise DeploymentPreparationError("configuration contains duplicate JSON keys")
        result[key] = value
    return result


def load_object(path: Path, *, description: str) -> dict[str, Any]:
    try:
        with path.open("rb") as stream:
            payload = stream.read(1_000_001)
        if len(payload) > 1_000_000:
            raise DeploymentPreparationError(f"{description} exceeds the configuration size limit")
        raw = json.loads(payload, object_pairs_hook=_without_duplicates)
    except (OSError, ValueError, RecursionError) as exc:
        raise DeploymentPreparationError(f"cannot read valid {description}") from exc
    if not isinstance(raw, dict) or type(raw.get("version")) is not int or raw["version"] != 1:
        raise DeploymentPreparationError(f"{description} must be a version 1 JSON object")
    return raw


def docker_configuration(config_dir: Path, *, template: Path) -> dict[str, Any]:
    """Copy public bot settings, resolving data and lock paths inside Docker."""
    original = load_object(config_dir / "bot.json", description="bot configuration")
    defaults = load_object(template, description="bot template")
    if set(original) - set(defaults) or not original.get("ZABBIX_API_URL"):
        raise DeploymentPreparationError(
            "migrate bot.json with tools.migrate_config_layout and configure ZABBIX_API_TOKEN before packaging"
        )
    return {
        **defaults,
        **original,
        "DATA_DIR": "/var/lib/maintbot",
        "INSTANCE_LOCK_PATH": "",
        "PTB_PERSISTENCE_PATH": "",
    }


def write_object(path: Path, value: dict[str, Any]) -> None:
    payload = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    path.write_text(payload, encoding="utf-8", newline="\n")
    path.chmod(0o600)
