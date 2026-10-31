"""Legacy stage 1: normalize old MaintBot dotenv files before JSON migration.

The intermediate public and secret key sets come from ``app/.env.example`` and
``app/env.secrets.example``.  Server-specific legacy keys are accepted only so
they can be discarded after ``migrate_server_inventory.py`` has produced the
intermediate TOML inventory.  Output files are always created exclusively; this
tool never overwrites an existing file.  Finish with ``migrate_config_layout.py``;
MaintBot no longer reads the generated public dotenv or TOML at runtime.
"""

from __future__ import annotations

import argparse
import ntpath
import os
import posixpath
import re
from dataclasses import dataclass
from io import StringIO
from pathlib import Path
from typing import TextIO

from dotenv.parser import Binding, parse_stream


class MigrationError(ValueError):
    """A safe-to-display migration failure that never includes env values."""


@dataclass(frozen=True)
class EnvDocument:
    values: dict[str, str]
    bindings: tuple[Binding, ...]


@dataclass(frozen=True)
class MigratedEnv:
    public_text: str
    secrets_text: str


_MIGRATED_SECRET_KEYS = frozenset({"REMNAWAVE_METRICS_USER", "REMNAWAVE_METRICS_PASS"})

_RENAMED_PUBLIC_KEYS = {
    "MESSAGE_CLEANUP_ENABLED": "NAVIGATION_CLEANUP_ENABLED",
    "MESSAGE_RETENTION_HOURS": "NAVIGATION_RETENTION_HOURS",
    "MESSAGE_CLEANUP_INTERVAL_SEC": "NAVIGATION_CLEANUP_INTERVAL_SEC",
}

# These fields belonged either to the old positional server configuration or
# to the pre-split storage layout.  They are accepted as migration input but
# must never be copied to the canonical public env output.
_DISCARDED_LEGACY_PUBLIC_KEYS = frozenset(
    {
        "BOT_MODE",
        "CHECK_A_DOMAINS",
        "EXPECTED_A_IP",
        "FAIL2BAN_ENABLED",
        "FAIL2BAN_LOG_PATH",
        "FAIL2BAN_TIMEZONE",
        "IMPORTANT_DATA_PATH",
        "LOCAL_SERVER_CODE",
        "LOCAL_SERVER_FLAG",
        "LOCAL_SERVER_LABEL",
        "LOCAL_SERVER_REMNAWAVE_UUID",
        "MONITOR_CONTAINERS",
        "REMOTE_SERVER_CHECK_A_DOMAINS",
        "REMOTE_SERVER_CODE",
        "REMOTE_SERVER_CODES",
        "REMOTE_SERVER_DOMAINS",
        "REMOTE_SERVER_ENABLED",
        "REMOTE_SERVER_EXPECTED_A_IP",
        "REMOTE_SERVER_EXPECTED_A_IPS",
        "REMOTE_SERVER_FAIL2BAN_ENABLED",
        "REMOTE_SERVER_FAIL2BAN_LOG_PATH",
        "REMOTE_SERVER_FAIL2BAN_LOG_PATHS",
        "REMOTE_SERVER_FAIL2BAN_TIMEZONE",
        "REMOTE_SERVER_FAIL2BAN_TIMEZONES",
        "REMOTE_SERVER_FLAG",
        "REMOTE_SERVER_FLAGS",
        "REMOTE_SERVER_LABEL",
        "REMOTE_SERVER_LABELS",
        "REMOTE_SERVER_MONITOR_CONTAINERS",
        "REMOTE_SERVER_MONITOR_CONTAINERS_BY_SERVER",
        "REMOTE_SERVER_REMNAWAVE_UUID",
        "REMOTE_SERVER_REMNAWAVE_UUIDS",
        "REMOTE_SERVER_SSH_TARGET",
        "REMOTE_SERVER_SSH_TARGETS",
        "SSH_IDENTITY_FILE",
        "SSH_KNOWN_HOSTS_FILE",
        "SSH_STRICT_HOST_KEY_CHECKING",
        "USER_DATA_PATH",
    }
)


def _parse_stream_safely(stream: TextIO, *, source: str) -> EnvDocument:
    bindings = tuple(parse_stream(stream))
    values: dict[str, str] = {}
    for binding in bindings:
        if binding.error:
            raise MigrationError(f"{source}: invalid dotenv syntax at line {binding.original.line}")
        if binding.key is None:
            continue
        key = str(binding.key)
        if key in values:
            raise MigrationError(f"{source}: duplicate key {key}")
        if binding.value is None:
            raise MigrationError(f"{source}: key {key} has no assigned value")
        values[key] = str(binding.value)
    return EnvDocument(values=values, bindings=bindings)


def _read_env(path: Path, *, source: str) -> EnvDocument:
    if not path.is_file():
        raise MigrationError(f"{source}: file not found or is not a regular file: {path}")
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as stream:
            return _parse_stream_safely(stream, source=source)
    except UnicodeError as exc:
        raise MigrationError(f"{source}: file is not valid UTF-8: {path}") from exc


def _encoded(value: str) -> str:
    """Return a python-dotenv single-quoted value with lossless escaping."""

    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _line_ending(original: str) -> str:
    if original.endswith("\r\n"):
        return "\r\n"
    if original.endswith("\n"):
        return "\n"
    if original.endswith("\r"):
        return "\r"
    return ""


def _render_template(template: EnvDocument, values: dict[str, str], *, source: str) -> str:
    expected = set(template.values)
    if set(values) != expected:
        raise MigrationError(f"{source}: rendered key set does not match the canonical template")

    rendered: list[str] = []
    for binding in template.bindings:
        if binding.key is None:
            rendered.append(binding.original.string)
            continue
        key = str(binding.key)
        rendered.append(f"{key}={_encoded(values[key])}{_line_ending(binding.original.string)}")
    text = "".join(rendered)

    round_trip = _parse_stream_safely(StringIO(text), source=f"rendered {source}")
    if round_trip.values != values:
        raise MigrationError(f"{source}: dotenv round-trip verification failed")
    return text


def _reject_unknown_keys(*, actual: set[str], allowed: set[str], source: str) -> None:
    unknown = sorted(actual - allowed)
    if unknown:
        raise MigrationError(f"{source}: unknown active keys: {', '.join(unknown)}")


def _apply_renamed_public_keys(source: dict[str, str], destination: dict[str, str]) -> None:
    for legacy_key, canonical_key in _RENAMED_PUBLIC_KEYS.items():
        if legacy_key not in source:
            continue
        legacy_value = source[legacy_key]
        if canonical_key in source and source[canonical_key] != legacy_value:
            raise MigrationError(f"public env: conflicting values for {legacy_key} and {canonical_key}")
        destination[canonical_key] = legacy_value


def _path_module(first: str, second: str):
    windows_path = bool(re.match(r"^[A-Za-z]:[\\/]", first)) or "\\" in first
    other_is_windows = bool(re.match(r"^[A-Za-z]:[\\/]", second)) or "\\" in second
    if windows_path != other_is_windows:
        raise MigrationError("public env: USER_DATA_PATH and IMPORTANT_DATA_PATH use different path styles")
    return ntpath if windows_path else posixpath


def _derive_data_dir(public_source: dict[str, str]) -> str | None:
    path_keys = ("USER_DATA_PATH", "IMPORTANT_DATA_PATH")
    present = [key for key in path_keys if key in public_source]
    if not present:
        return None
    if len(present) != len(path_keys) or any(not public_source[key].strip() for key in path_keys):
        raise MigrationError("public env: USER_DATA_PATH and IMPORTANT_DATA_PATH are both required to derive DATA_DIR")

    user_path = public_source["USER_DATA_PATH"].strip()
    important_path = public_source["IMPORTANT_DATA_PATH"].strip()
    path_module = _path_module(user_path, important_path)
    try:
        common_parent = path_module.commonpath([path_module.dirname(user_path), path_module.dirname(important_path)])
    except ValueError as exc:
        raise MigrationError("public env: legacy data paths do not have a common parent") from exc
    if not common_parent or common_parent in {".", "/"} or re.fullmatch(r"[A-Za-z]:[\\/]?", common_parent):
        raise MigrationError("public env: legacy data paths have an unsafe common parent")
    return common_parent


def _merge_migrated_secrets(public_source: dict[str, str], secret_source: dict[str, str]) -> dict[str, str]:
    result = dict(secret_source)
    for key in _MIGRATED_SECRET_KEYS:
        public_value = public_source.get(key, "")
        secret_value = secret_source.get(key, "")
        if public_value and secret_value and public_value != secret_value:
            raise MigrationError(f"public/secret env: conflicting values for {key}")
        if public_value and not secret_value:
            result[key] = public_value
    return result


def build_migration(
    *,
    public_env: Path,
    secrets_env: Path,
    public_template: Path,
    secrets_template: Path,
    server_inventory_file: str,
) -> MigratedEnv:
    """Build verified canonical dotenv contents without writing any files."""

    inventory_path = server_inventory_file.strip()
    if not inventory_path:
        raise MigrationError("--server-inventory-file must not be empty")

    public_source = _read_env(public_env, source="public env")
    secret_source = _read_env(secrets_env, source="secret env")
    canonical_public = _read_env(public_template, source="public template")
    canonical_secrets = _read_env(secrets_template, source="secret template")

    public_keys = set(canonical_public.values)
    secret_keys = set(canonical_secrets.values)
    if public_keys & secret_keys:
        raise MigrationError("canonical templates contain overlapping public and secret keys")
    if not secret_keys >= _MIGRATED_SECRET_KEYS:
        raise MigrationError("secret template is missing Remnawave credential keys")
    if not {"DATA_DIR", "SERVER_INVENTORY_FILE"} <= public_keys:
        raise MigrationError("public template is missing DATA_DIR or SERVER_INVENTORY_FILE")

    allowed_public = (
        public_keys
        | secret_keys.intersection(_MIGRATED_SECRET_KEYS)
        | set(_RENAMED_PUBLIC_KEYS)
        | set(_DISCARDED_LEGACY_PUBLIC_KEYS)
    )
    _reject_unknown_keys(actual=set(public_source.values), allowed=allowed_public, source="public env")
    _reject_unknown_keys(actual=set(secret_source.values), allowed=secret_keys, source="secret env")

    public_values = dict(canonical_public.values)
    public_values.update({key: value for key, value in public_source.values.items() if key in public_keys})
    _apply_renamed_public_keys(public_source.values, public_values)

    derived_data_dir = _derive_data_dir(public_source.values)
    if derived_data_dir is not None:
        configured_data_dir = public_source.values.get("DATA_DIR", "").strip()
        if configured_data_dir and configured_data_dir != derived_data_dir:
            raise MigrationError("public env: DATA_DIR conflicts with the legacy data path parent")
        public_values["DATA_DIR"] = derived_data_dir
    public_values["SERVER_INVENTORY_FILE"] = inventory_path

    merged_secret_source = _merge_migrated_secrets(public_source.values, secret_source.values)
    secret_values = dict(canonical_secrets.values)
    secret_values.update({key: value for key, value in merged_secret_source.items() if key in secret_keys})

    return MigratedEnv(
        public_text=_render_template(canonical_public, public_values, source="public env"),
        secrets_text=_render_template(canonical_secrets, secret_values, source="secret env"),
    )


def _exclusive_write(path: Path, text: str, *, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
            descriptor = -1
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        if descriptor >= 0:
            os.close(descriptor)
            descriptor = -1
        path.unlink(missing_ok=True)
        raise
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def migrate_files(
    *,
    public_env: Path,
    secrets_env: Path,
    public_template: Path,
    secrets_template: Path,
    output_public: Path,
    output_secrets: Path,
    server_inventory_file: str,
) -> None:
    """Create both migrated files exclusively, rolling back partial output."""

    if output_public.resolve() == output_secrets.resolve():
        raise MigrationError("public and secret output paths must be different")
    existing = [path for path in (output_public, output_secrets) if path.exists()]
    if existing:
        raise MigrationError(f"refusing to overwrite existing output: {existing[0]}")

    migrated = build_migration(
        public_env=public_env,
        secrets_env=secrets_env,
        public_template=public_template,
        secrets_template=secrets_template,
        server_inventory_file=server_inventory_file,
    )

    created: list[Path] = []
    try:
        _exclusive_write(output_public, migrated.public_text, mode=0o600)
        created.append(output_public)
        _exclusive_write(output_secrets, migrated.secrets_text, mode=0o600)
        created.append(output_secrets)
    except BaseException:
        for path in created:
            path.unlink(missing_ok=True)
        raise


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", type=Path, default=Path("app/.env"), help="legacy public env input")
    parser.add_argument("--secrets", type=Path, default=Path("app/env.secrets"), help="legacy secret env input")
    parser.add_argument("--env-template", type=Path, default=Path("app/.env.example"))
    parser.add_argument("--secrets-template", type=Path, default=Path("app/env.secrets.example"))
    parser.add_argument("--output-env", type=Path, required=True)
    parser.add_argument("--output-secrets", type=Path, required=True)
    parser.add_argument("--server-inventory-file", required=True)
    return parser


def main() -> int:
    parser = _parser()
    args = parser.parse_args()
    try:
        migrate_files(
            public_env=args.env,
            secrets_env=args.secrets,
            public_template=args.env_template,
            secrets_template=args.secrets_template,
            output_public=args.output_env,
            output_secrets=args.output_secrets,
            server_inventory_file=args.server_inventory_file,
        )
    except (MigrationError, OSError) as exc:
        parser.error(str(exc))
    print(f"Created intermediate public env: {args.output_env}")
    print(f"Created secret env: {args.output_secrets}")
    print("Next run tools/migrate_config_layout.py; MaintBot does not read the public env at runtime.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
