"""Prepare copied operator configuration for a panel-only Docker deployment."""

from __future__ import annotations

import copy
import json
import re
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


def docker_configuration(
    config_dir: Path, *, template: Path, panel_only: bool
) -> tuple[dict[str, Any], dict[str, dict[str, Any]], int]:
    """Preserve existing settings and identity; change only Docker boundaries."""
    original = load_object(config_dir / "bot.json", description="bot configuration")
    bot = {**load_object(template, description="bot template"), **original}
    bot.update(
        DATA_DIR="/var/lib/maintbot",
        INSTANCE_LOCK_PATH="",
        PTB_PERSISTENCE_PATH="",
    )
    for retired_key in ("SSH_IDENTITY_FILE", "SSH_KNOWN_HOSTS_FILE", "SSH_STRICT_HOST_KEY_CHECKING"):
        bot.pop(retired_key, None)
    if not isinstance(bot.get("REMNAWAVE_METRICS_URL"), str) or not bot["REMNAWAVE_METRICS_URL"].strip():
        raise DeploymentPreparationError("a panel metrics URL is required")
    server_dir = config_dir / "servers"
    if not server_dir.is_dir():
        raise DeploymentPreparationError("server configuration directory is missing")
    files = sorted(path for path in server_dir.iterdir() if path.suffix.lower() == ".json")
    if not files or len(files) > 256:
        raise DeploymentPreparationError("expected between 1 and 256 server configuration files")
    servers: dict[str, dict[str, Any]] = {}
    keys: set[str] = set()
    omitted = 0
    for path in files:
        document = load_object(path, description="server configuration")
        key = document.get("key")
        if not isinstance(key, str) or re.fullmatch(r"[a-z0-9_-]{1,12}", key) is None or key in keys:
            raise DeploymentPreparationError("server configuration has invalid or duplicate identity")
        keys.add(key)
        monitoring = document.get("monitoring", {})
        if not isinstance(monitoring, dict) or monitoring.get("source", "system") not in {"system", "remnawave"}:
            raise DeploymentPreparationError("server monitoring configuration is invalid")
        if monitoring.get("source", "system") != "remnawave":
            if panel_only:
                omitted += 1
                continue
            raise DeploymentPreparationError("host-only servers require --panel-only or an explicit panel UUID")
        node_uuid = monitoring.get("node_uuid")
        if (
            not isinstance(node_uuid, str)
            or re.fullmatch(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", node_uuid)
            is None
        ):
            raise DeploymentPreparationError("panel server configuration requires a valid existing node UUID")
        document = copy.deepcopy(document)
        document["connection"] = {"transport": "panel"}
        document["docker"] = {"containers": []}
        fail2ban = document.get("fail2ban", {})
        if not isinstance(fail2ban, dict):
            raise DeploymentPreparationError("server fail2ban configuration is invalid")
        document["fail2ban"] = {**fail2ban, "enabled": False}
        servers[path.name] = document
    if not servers:
        raise DeploymentPreparationError("no configured RemnaWave nodes remain for panel monitoring")
    return bot, servers, omitted


def write_object(path: Path, value: dict[str, Any]) -> None:
    payload = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    path.write_text(payload, encoding="utf-8", newline="\n")
    path.chmod(0o600)
