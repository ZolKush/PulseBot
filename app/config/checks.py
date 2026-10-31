"""Read-only production configuration and SQLite preflight checks."""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path


def _nearest_existing_parent(path: Path) -> Path | None:
    current = path
    while not current.exists():
        parent = current.parent
        if parent == current:
            return None
        current = parent
    return current


def _writable_parent(path_value: str, field_name: str) -> list[str]:
    path = Path(path_value)
    parent = path.parent
    existing = _nearest_existing_parent(parent)
    errors: list[str] = []
    if existing is None:
        errors.append(f"{field_name}: не найден существующий родительский каталог для {parent}")
    elif not existing.is_dir():
        errors.append(f"{field_name}: родительский путь не является каталогом: {existing}")
    elif not os.access(existing, os.W_OK | os.X_OK):
        errors.append(f"{field_name}: каталог недоступен для создания или записи: {existing}")
    if path.exists() and not path.is_file():
        errors.append(f"{field_name}: путь не является обычным файлом: {path}")
    elif path.exists() and not os.access(path, os.R_OK | os.W_OK):
        errors.append(f"{field_name}: файл недоступен для чтения и записи: {path}")
    return list(dict.fromkeys(errors))


def _readable_file(path_value: str, field_name: str) -> list[str]:
    path = Path(path_value)
    if not path.is_file():
        return [f"{field_name}: файл не найден: {path}"]
    if not os.access(path, os.R_OK):
        return [f"{field_name}: файл недоступен для чтения: {path}"]
    return []


def _check_private_data_permissions(path_value: str, field_name: str) -> list[str]:
    if os.name == "nt":
        return []
    path = Path(path_value)
    errors: list[str] = []
    if path.exists() and path.is_file() and stat.S_IMODE(path.stat().st_mode) & 0o077:
        errors.append(f"{field_name}: файл с данными должен иметь права 0600: {path}")
    parent = path if path.is_dir() else path.parent
    if parent.exists() and stat.S_IMODE(parent.stat().st_mode) & 0o007:
        errors.append(f"{field_name}: каталог данных не должен быть доступен остальным пользователям: {parent}")
    return errors


def _check_operator_config_permissions(path_value: str, field_name: str) -> list[str]:
    """Allow owner/group reads while forbidding group writes and all other access."""

    if os.name == "nt":
        return []
    path = Path(path_value)
    if not path.exists():
        return []
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except OSError as exc:
        return [f"{field_name}: не удалось проверить права доступа: {path} ({exc})"]
    forbidden = 0o027 if path.is_dir() else 0o037
    if mode & forbidden:
        return [f"{field_name}: конфигурация не должна быть доступна остальным или изменяема группой: {path}"]
    return []


def _check_json_object(path_value: str, field_name: str) -> list[str]:
    """Validate a JSON object without rewriting it."""
    path = Path(path_value)
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return [f"{field_name}: существующий файл содержит некорректный JSON: {path} ({exc})"]
    if not isinstance(raw, dict):
        return [f"{field_name}: корнем JSON должен быть объект: {path}"]
    return []


def _check_split_storage(data_dir: str) -> list[str]:
    import sqlite3

    from ..persistence.errors import PersistenceError
    from ..persistence.sqlite_backend import SQLiteBackend

    root = Path(data_dir)
    if not root.exists():
        return [f"DATA_DIR: каталог не найден: {root}"]
    if not root.is_dir():
        return [f"DATA_DIR: путь не является каталогом: {root}"]
    if not os.access(root, os.R_OK | os.W_OK | os.X_OK):
        return [f"DATA_DIR: каталог недоступен для чтения и записи: {root}"]

    backend = SQLiteBackend(root)
    try:
        if not backend.exists():
            return [
                f"DATA_DIR: SQLite не найдена ({backend.database_path}); "
                "для нового хранилища выполните python -m app.persistence.bootstrap, "
                "для переноса JSON — python -m app.persistence.migrate_sqlite"
            ]
        snapshot = backend.inspect()
    except (PersistenceError, OSError, UnicodeError, ValueError, sqlite3.Error) as exc:
        return [f"DATA_DIR: SQLite повреждена или требует recovery под блокировкой процесса: {exc}"]

    errors: list[str] = []
    if snapshot is not None:
        grants = snapshot.data("access.grants")
        owners = sum(
            1
            for meta in grants.values()
            if isinstance(meta, dict) and meta.get("role") == "admin" and meta.get("admin_level") == "owner"
        )
        if owners > 1:
            errors.append("DATA_DIR: найдено несколько руководителей сервиса")
    errors.extend(_check_private_data_permissions(str(backend.database_path), "DATA_DIR"))
    return list(dict.fromkeys(errors))


def validate_configuration() -> list[str]:
    # Resolve the public package at call time so diagnostic tests and one-shot
    # deployment checks can safely override settings.
    from . import (
        BOT_CONFIG_FILE,
        BOT_TOKEN,
        DATA_DIR,
        INSTANCE_LOCK_PATH,
        PRIVILEGED_HELPER_BIN,
        SERVER_CONFIG_DIR,
        SERVERS,
        SUDO_BIN,
    )
    from .locations import SECRETS_ENV_FILE

    errors: list[str] = []
    if ":" not in BOT_TOKEN or len(BOT_TOKEN) < 20:
        errors.append("BOT_TOKEN: значение не похоже на токен Telegram")
    if not SERVERS:
        errors.append("не настроено ни одного сервера")
    errors.extend(_readable_file(str(BOT_CONFIG_FILE), "BOT_CONFIG_FILE"))
    server_dir = Path(SERVER_CONFIG_DIR)
    server_files: list[Path] = []
    if not server_dir.is_dir():
        errors.append(f"SERVER_CONFIG_DIR: каталог не найден: {server_dir}")
    elif not os.access(server_dir, os.R_OK | os.X_OK):
        errors.append(f"SERVER_CONFIG_DIR: каталог недоступен для чтения: {server_dir}")
    else:
        try:
            server_files = sorted(
                (path for path in server_dir.iterdir() if path.suffix.lower() == ".json"),
                key=lambda path: (path.name.casefold(), path.name),
            )
        except OSError as exc:
            errors.append(f"SERVER_CONFIG_DIR: не удалось прочитать каталог {server_dir}: {exc}")
        else:
            for server_file in server_files:
                errors.extend(_readable_file(str(server_file), f"SERVER_CONFIG_DIR/{server_file.name}"))
    if os.name != "nt" and any(server.mode == "local" for server in SERVERS.values()):
        helper = Path(PRIVILEGED_HELPER_BIN)
        if not helper.is_absolute() or not helper.is_file() or not os.access(helper, os.X_OK):
            errors.append(f"PRIVILEGED_HELPER_BIN: helper не найден или не исполняемый: {helper}")
        if not SUDO_BIN:
            errors.append("sudo не найден, безопасный privileged helper недоступен")

    errors.extend(_check_split_storage(DATA_DIR))
    errors.extend(_writable_parent(INSTANCE_LOCK_PATH, "INSTANCE_LOCK_PATH"))

    if os.name != "nt":
        errors.extend(_check_operator_config_permissions(str(BOT_CONFIG_FILE), "BOT_CONFIG_FILE"))
        errors.extend(_check_operator_config_permissions(str(SERVER_CONFIG_DIR), "SERVER_CONFIG_DIR"))
        for server_file in server_files:
            errors.extend(_check_operator_config_permissions(str(server_file), f"SERVER_CONFIG_DIR/{server_file.name}"))
        for path, name in ((SECRETS_ENV_FILE, "SECRETS_ENV_PATH"),):
            if path.exists() and stat.S_IMODE(path.stat().st_mode) & 0o007:
                errors.append(f"{name}: env-файл не должен быть доступен остальным пользователям: {path}")
    return list(dict.fromkeys(errors))


def main() -> int:
    try:
        errors = validate_configuration()
    except Exception as exc:
        print(f"Ошибка конфигурации: {exc}", file=sys.stderr)
        return 1
    if errors:
        for error in errors:
            print(f"Ошибка конфигурации: {error}", file=sys.stderr)
        return 1
    from . import SERVERS

    dns_count = sum(len(server.check_a_domains) for server in SERVERS.values())
    tls_count = sum(len(server.tls_endpoints) for server in SERVERS.values())
    container_count = sum(len(server.monitor_containers) for server in SERVERS.values())
    print(
        "Конфигурация MaintBot корректна: "
        f"servers={len(SERVERS)} dns_domains={dns_count} tls_endpoints={tls_count} containers={container_count}"
    )
    return 0


__all__ = [
    "main",
    "validate_configuration",
]
