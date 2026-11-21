from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from app.config.checks import _check_split_storage
from app.persistence.layout import default_store_data
from app.persistence.sqlite_backend import SQLiteBackend

ROOT = Path(__file__).resolve().parents[1]


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


@pytest.mark.parametrize(
    ("document", "detail"),
    [("{broken", "invalid BOT_CONFIG_FILE"), ("[]", "root must be a JSON object")],
    ids=["invalid-json", "not-an-object"],
)
def test_config_check_cli_catches_settings_import_failure_without_traceback(
    tmp_path: Path, document: str, detail: str
) -> None:
    config_dir = tmp_path / "conf"
    config_dir.mkdir()
    (config_dir / "bot.json").write_text(document, encoding="utf-8")
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
    assert detail in result.stderr
    assert "Traceback" not in result.stderr
