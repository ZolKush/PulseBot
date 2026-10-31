"""Изолирует тесты от реальных секретов и JSON-файлов проекта."""

from __future__ import annotations

import atexit
import json
import os
import shutil
import tempfile
from pathlib import Path

import pytest

_TEST_ROOT = Path(tempfile.mkdtemp(prefix="maintbot-tests-"))
atexit.register(shutil.rmtree, _TEST_ROOT, True)
_TEST_CONFIG_DIR = _TEST_ROOT / "conf"
_TEST_SERVER_DIR = _TEST_CONFIG_DIR / "servers"
_TEST_SERVER_DIR.mkdir(parents=True)
_BOT_CONFIG = json.loads(
    (Path(__file__).resolve().parents[1] / "examples" / "conf" / "bot.json").read_text(encoding="utf-8")
)
_BOT_CONFIG.update(
    {
        "DATA_DIR": str(_TEST_ROOT / "data"),
        "INSTANCE_LOCK_PATH": str(_TEST_ROOT / "maintbot.lock"),
        "PTB_PERSISTENCE_PATH": str(_TEST_ROOT / "data" / "telegram" / "persistence.pickle"),
    }
)
(_TEST_CONFIG_DIR / "bot.json").write_text(
    json.dumps(_BOT_CONFIG),
    encoding="utf-8",
)
(_TEST_SERVER_DIR / "anything.json").write_text(
    json.dumps(
        {
            "version": 1,
            "key": "local",
            "label": "Local server",
            "flag": "",
            "connection": {"transport": "local"},
            "monitoring": {"source": "system", "node_uuid": ""},
        }
    ),
    encoding="utf-8",
)

os.environ.update(
    {
        "MAINTBOT_CONFIG_DIR": str(_TEST_CONFIG_DIR),
        "SECRETS_ENV_PATH": str(_TEST_ROOT / "missing.secrets"),
        "BOT_TOKEN": "123456:TEST_TOKEN_NOT_USED_BY_TESTS_ABCDEFGHIJKLMNOPQRSTUVWXYZ",
        "ADMIN_PASSWORD": "test-admin-password-that-is-never-used",
        "OWNER_PASSWORD": "test-owner-password-that-is-never-used",
        "PTB_TIMEDELTA": "1",
    }
)


@pytest.fixture
def isolated_storage(tmp_path: Path):
    """Give feature tests a fresh SQLite database without production files."""
    from app import storage

    storage.initialize_empty_storage_for_tests(tmp_path / "data")
    yield
