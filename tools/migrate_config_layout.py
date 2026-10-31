"""Migrate the current dotenv + TOML configuration into ``data/conf`` JSON files."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import uuid
from contextlib import suppress
from pathlib import Path
from typing import Any, TextIO

from dotenv.parser import parse_stream

if sys.version_info >= (3, 11):  # pragma: no branch
    import tomllib
else:  # pragma: no cover - Python 3.10 only
    import tomli as tomllib

_SERVER_KEY = re.compile(r"^[a-z0-9_-]{1,12}$")
_SECRET_KEYS = {
    "BOT_TOKEN",
    "ADMIN_PASSWORD",
    "OWNER_PASSWORD",
    "REMNAWAVE_METRICS_USER",
    "REMNAWAVE_METRICS_PASS",
}
_DISCARDED_KEYS = {
    "SERVER_INVENTORY_FILE",
    "SSH_IDENTITY_FILE",
    "SSH_KNOWN_HOSTS_FILE",
    "SSH_STRICT_HOST_KEY_CHECKING",
}


class ConfigMigrationError(ValueError):
    """A migration error that is safe to print without exposing values."""


def _read_env(stream: TextIO, *, source: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for binding in parse_stream(stream):
        if binding.error:
            raise ConfigMigrationError(f"{source}: invalid dotenv syntax at line {binding.original.line}")
        if binding.key is None:
            continue
        key = str(binding.key)
        if key in result:
            raise ConfigMigrationError(f"{source}: duplicate key {key}")
        if binding.value is None:
            raise ConfigMigrationError(f"{source}: key {key} has no assigned value")
        result[key] = str(binding.value)
    return result


def _load_env(path: Path) -> dict[str, str]:
    if not path.is_file():
        raise ConfigMigrationError(f"public env is not a regular file: {path}")
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as stream:
            return _read_env(stream, source="public env")
    except UnicodeError as exc:
        raise ConfigMigrationError(f"public env is not valid UTF-8: {path}") from exc


def _load_json_object(path: Path, *, source: str) -> dict[str, Any]:
    if not path.is_file():
        raise ConfigMigrationError(f"{source} is not a regular file: {path}")

    def no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ConfigMigrationError(f"{source}: duplicate JSON key {key}")
            result[key] = value
        return result

    try:
        raw = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=no_duplicates)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ConfigMigrationError(f"cannot read {source} {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigMigrationError(f"{source} root must be an object")
    return raw


def _parse_bool(value: str, *, key: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ConfigMigrationError(f"public env: {key} is not a boolean")


def _convert_value(key: str, value: str, default: Any) -> Any:
    try:
        if isinstance(default, bool):
            return _parse_bool(value, key=key)
        if isinstance(default, int):
            return int(value.strip())
        if isinstance(default, list):
            stripped = value.strip()
            if stripped.startswith("["):
                parsed = json.loads(stripped)
                if not isinstance(parsed, list):
                    raise ValueError
                return parsed
            return [part.strip() for part in stripped.split(",") if part.strip()]
        return value.strip()
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ConfigMigrationError(f"public env: {key} has a value incompatible with the JSON template") from exc


def build_bot_document(env: dict[str, str], template: dict[str, Any]) -> dict[str, Any]:
    if type(template.get("version")) is not int or template["version"] != 1:
        raise ConfigMigrationError("bot template must have version=1")
    allowed = set(template) - {"version"}
    secrets = sorted(set(env) & _SECRET_KEYS)
    if secrets:
        raise ConfigMigrationError(
            "public env contains secret keys that must remain in app/env.secrets: " + ", ".join(secrets)
        )
    unknown = sorted(set(env) - allowed - _DISCARDED_KEYS)
    if unknown:
        raise ConfigMigrationError(f"public env contains unknown active keys: {', '.join(unknown)}")
    result = dict(template)
    for key, value in env.items():
        if key in allowed:
            result[key] = _convert_value(key, value, template[key])
    return result


def load_toml_servers(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        raise ConfigMigrationError(f"server inventory is not a regular file: {path}")
    try:
        with path.open("rb") as stream:
            raw = tomllib.load(stream)
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise ConfigMigrationError(f"cannot read server inventory {path}: {exc}") from exc
    if (
        not isinstance(raw, dict)
        or set(raw) != {"version", "servers"}
        or type(raw.get("version")) is not int
        or raw["version"] != 1
    ):
        raise ConfigMigrationError("server inventory must contain exactly version=1 and servers")
    raw_servers = raw.get("servers")
    if not isinstance(raw_servers, dict) or not raw_servers:
        raise ConfigMigrationError("server inventory contains no servers")

    result: list[dict[str, Any]] = []
    local_count = 0
    for index, (raw_key, raw_server) in enumerate(raw_servers.items(), start=1):
        key = str(raw_key)
        if not _SERVER_KEY.fullmatch(key):
            raise ConfigMigrationError(f"server inventory contains invalid key: {key}")
        if not isinstance(raw_server, dict):
            raise ConfigMigrationError(f"server inventory entry is not an object: {key}")
        if {"key", "version"} & raw_server.keys():
            raise ConfigMigrationError(f"server entry must not override key or version: {key}")
        document = {"version": 1, "key": key, **raw_server}
        document.setdefault("display_order", index * 10)
        connection = document.get("connection")
        if isinstance(connection, dict):
            connection.pop("target", None)
            if connection.get("transport") == "ssh":
                monitoring = document.get("monitoring", {})
                if (
                    not isinstance(monitoring, dict)
                    or monitoring.get("source") != "remnawave"
                    or not monitoring.get("node_uuid")
                ):
                    raise ConfigMigrationError(
                        f"SSH monitoring was removed; configure a RemnaWave node before migrating server: {key}"
                    )
                connection["transport"] = "panel"
                document["docker"] = {"containers": []}
                fail2ban = document.get("fail2ban", {})
                if not isinstance(fail2ban, dict):
                    raise ConfigMigrationError(f"server inventory contains an invalid fail2ban block: {key}")
                document["fail2ban"] = {**fail2ban, "enabled": False}
            if connection.get("transport") == "local":
                local_count += 1
        result.append(document)
    if local_count > 1:
        raise ConfigMigrationError("server inventory contains more than one local server")
    return result


def _encode(document: dict[str, Any]) -> bytes:
    try:
        return (json.dumps(document, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
    except (ValueError, TypeError, RecursionError):
        raise ConfigMigrationError("configuration contains non-JSON values") from None


def _write_private(path: Path, payload: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _sync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def write_layout(output_dir: Path, bot: dict[str, Any], servers: list[dict[str, Any]]) -> None:
    if output_dir.exists():
        raise ConfigMigrationError(f"refusing to overwrite existing output: {output_dir}")
    keys = [document.get("key") for document in servers]
    if not keys or any(not isinstance(key, str) or not _SERVER_KEY.fullmatch(key) for key in keys):
        raise ConfigMigrationError("server documents contain invalid keys")
    if len(set(keys)) != len(keys):
        raise ConfigMigrationError("server documents contain duplicate keys")
    encoded_bot, encoded_servers = _encode(bot), [_encode(document) for document in servers]
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_dir.parent / f".{output_dir.name}.{uuid.uuid4().hex}.tmp"
    try:
        temporary.mkdir(mode=0o700)
        server_dir = temporary / "servers"
        server_dir.mkdir(mode=0o700)
        _write_private(temporary / "bot.json", encoded_bot)
        for index, (document, payload) in enumerate(zip(servers, encoded_servers, strict=True), start=1):
            filename = f"{index * 10:03d}-{document['key']}.json"
            _write_private(server_dir / filename, payload)
        _sync_directory(server_dir)
        _sync_directory(temporary)
        temporary.replace(output_dir)
        _sync_directory(output_dir.parent)
    except BaseException:
        if temporary.exists():
            with suppress(OSError):
                shutil.rmtree(temporary)
        raise


def migrate(*, env_path: Path, inventory_path: Path, template_path: Path, output_dir: Path) -> int:
    env = _load_env(env_path)
    template = _load_json_object(template_path, source="bot template")
    bot = build_bot_document(env, template)
    servers = load_toml_servers(inventory_path)
    write_layout(output_dir, bot, servers)
    return len(servers)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", type=Path, default=Path("app/.env"))
    parser.add_argument("--inventory", type=Path, default=Path("app/servers.toml"))
    parser.add_argument("--template", type=Path, default=Path("examples/conf/bot.json"))
    parser.add_argument("--output-dir", type=Path, default=Path("data/conf"))
    args = parser.parse_args()
    try:
        count = migrate(
            env_path=args.env,
            inventory_path=args.inventory,
            template_path=args.template,
            output_dir=args.output_dir,
        )
    except (ConfigMigrationError, OSError) as exc:
        print(f"Ошибка миграции конфигурации: {' '.join(str(exc).split())}", file=sys.stderr)
        return 1
    print(f"Created {args.output_dir / 'bot.json'} and {count} server JSON files.")
    print("Run app.config_check before starting MaintBot; the source files were not modified.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
