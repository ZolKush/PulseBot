from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from app import config
from app.config.checks import _check_split_storage
from app.config_check import _check_json_object, validate_configuration
from app.persistence.backend import SplitJsonBackend
from app.persistence.layout import default_store_data
from app.persistence.sqlite_backend import SQLiteBackend

ROOT = Path(__file__).resolve().parents[1]


class SimulatedCrash(BaseException):
    pass


def test_config_check_rejects_malformed_or_non_object_json(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text("{broken", encoding="utf-8")
    assert _check_json_object(str(path), "STATE")

    path.write_text("[]", encoding="utf-8")
    assert "корнем JSON должен быть объект" in _check_json_object(str(path), "STATE")[0]

    path.write_text("{}", encoding="utf-8")
    assert _check_json_object(str(path), "STATE") == []


def test_config_check_requires_readable_server_directory(tmp_path: Path, monkeypatch) -> None:
    missing = tmp_path / "missing-servers"
    monkeypatch.setattr(config, "SERVER_CONFIG_DIR", missing)

    errors = validate_configuration()

    assert any("SERVER_CONFIG_DIR: каталог не найден" in error for error in errors)


def test_config_check_requires_sqlite_without_creating_it(tmp_path: Path) -> None:
    errors = _check_split_storage(str(tmp_path))

    assert any("SQLite не найдена" in error for error in errors)
    assert any("app.persistence.migrate_sqlite" in error for error in errors)
    assert list(tmp_path.iterdir()) == []


def test_config_check_rejects_multiple_owners_across_split_access_store(tmp_path: Path) -> None:
    stores = default_store_data()
    stores["access.grants"] = {
        "1": {"role": "admin", "admin_level": "owner"},
        "2": {"role": "admin", "admin_level": "owner"},
    }
    SQLiteBackend(tmp_path).bootstrap(stores=stores)

    errors = _check_split_storage(str(tmp_path))

    assert any("несколько руководителей" in error for error in errors)


def test_config_check_refuses_legacy_pending_redo_without_changing_it(tmp_path: Path) -> None:
    root = tmp_path / "data"
    SplitJsonBackend(root).bootstrap()

    def crash(name: str) -> None:
        if name == "after_prepare":
            raise SimulatedCrash

    backend = SplitJsonBackend(root, failpoint=crash)
    with backend.unit_of_work() as uow:
        uow.profiles.put(42, {"user_id": 42})
        with pytest.raises(SimulatedCrash):
            uow.commit()

    assert any("migrate_sqlite" in error for error in _check_split_storage(str(root)))
    assert SplitJsonBackend(root).has_pending_transactions() is True


def test_config_check_cli_catches_inventory_import_failure_without_traceback(tmp_path: Path) -> None:
    config_dir = tmp_path / "conf"
    server_dir = config_dir / "servers"
    server_dir.mkdir(parents=True)
    (config_dir / "bot.json").write_text(config.BOT_CONFIG_FILE.read_text(encoding="utf-8"), encoding="utf-8")
    (server_dir / "broken.json").write_text("{broken", encoding="utf-8")
    env = os.environ.copy()
    env.update({"PYTHONUTF8": "1", "MAINTBOT_CONFIG_DIR": str(config_dir)})

    result = subprocess.run(
        [sys.executable, "-m", "app.config_check"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )

    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr.startswith("Ошибка конфигурации: ")
    assert "Traceback" not in result.stderr
