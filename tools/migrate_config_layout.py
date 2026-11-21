"""Convert a previous bot.json or public dotenv to the Zabbix-based configuration."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import uuid
from contextlib import suppress
from pathlib import Path
from typing import Any, TextIO

from dotenv.parser import parse_stream

_SECRET_KEYS = {
    "BOT_TOKEN",
    "ZABBIX_API_TOKEN",
    "ADMIN_PASSWORD",
    "OWNER_PASSWORD",
    "REMNAWAVE_METRICS_USER",
    "REMNAWAVE_METRICS_PASS",
}
_DISCARDED_KEYS = {
    "SERVER_INVENTORY_FILE",
    "DNS_RESOLVERS",
    "DNS_DAILY_REFRESH_AT",
    "DNS_STARTUP_REFRESH_DELAY_SEC",
    "FAIL2BAN_DAILY_AT",
    "FAIL2BAN_DIGEST_TAIL_LINES",
    "FAIL2BAN_DIGEST_MAX_BYTES",
    "SUBPROC_SHORT_TIMEOUT",
    "SUBPROC_MEDIUM_TIMEOUT",
    "SUBPROC_MAX_OUTPUT_BYTES",
    "PRIVILEGED_HELPER_BIN",
    "STATUS_CACHE_TTL_SEC",
    "REMNAWAVE_METRICS_URL",
    "REMNAWAVE_METRICS_TIMEOUT_SEC",
    "REMNAWAVE_METRICS_CACHE_TTL_SEC",
    "REMNAWAVE_METRICS_MAX_BYTES",
    "REMNAWAVE_HIDDEN_UUIDS",
    "DAILY_NODE_STATUS_REFRESH_AT",
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


def write_layout(output_dir: Path, bot: dict[str, Any]) -> None:
    if output_dir.exists():
        raise ConfigMigrationError(f"refusing to overwrite existing output: {output_dir}")
    encoded_bot = _encode(bot)
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_dir.parent / f".{output_dir.name}.{uuid.uuid4().hex}.tmp"
    try:
        temporary.mkdir(mode=0o700)
        _write_private(temporary / "bot.json", encoded_bot)
        _sync_directory(temporary)
        temporary.replace(output_dir)
        _sync_directory(output_dir.parent)
    except BaseException:
        if temporary.exists():
            with suppress(OSError):
                shutil.rmtree(temporary)
        raise


def migrate(
    *, env_path: Path | None, bot_path: Path | None, template_path: Path, output_dir: Path, zabbix_api_url: str
) -> None:
    template = _load_json_object(template_path, source="bot template")
    if bot_path is not None and env_path is not None:
        raise ConfigMigrationError("select exactly one source: --bot-json or --env")
    if bot_path is not None:
        original = _load_json_object(bot_path, source="bot configuration")
        if type(original.get("version")) is not int or original["version"] != 1:
            raise ConfigMigrationError("bot configuration must have version=1")
        unknown = set(original) - set(template) - _DISCARDED_KEYS
        if unknown:
            raise ConfigMigrationError("bot configuration contains unknown keys: " + ", ".join(sorted(unknown)))
        bot = {**template, **{key: value for key, value in original.items() if key in template}}
    else:
        if env_path is None:
            raise ConfigMigrationError("public env path is required")
        bot = build_bot_document(_load_env(env_path), template)
    bot["ZABBIX_API_URL"] = zabbix_api_url
    write_layout(output_dir, bot)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--env", type=Path)
    source.add_argument("--bot-json", type=Path)
    parser.add_argument("--zabbix-api-url", required=True)
    parser.add_argument("--template", type=Path, default=Path("examples/conf/bot.json"))
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    try:
        migrate(
            env_path=args.env,
            bot_path=args.bot_json,
            template_path=args.template,
            output_dir=args.output_dir,
            zabbix_api_url=args.zabbix_api_url,
        )
    except (ConfigMigrationError, OSError) as exc:
        print(f"Ошибка миграции конфигурации: {' '.join(str(exc).split())}", file=sys.stderr)
        return 1
    print(f"Created {args.output_dir / 'bot.json'}.")
    print("Configure ZABBIX_API_TOKEN in your secret env and run app.config_check before starting MaintBot.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
