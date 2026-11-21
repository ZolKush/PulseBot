from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools import migrate_config_layout as layout

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "examples" / "conf" / "bot.json"
URL = "https://monitor.example.test/zabbix/api_jsonrpc.php"


def test_upgrade_preserves_bot_settings_and_ignores_old_inventory(tmp_path: Path) -> None:
    source = tmp_path / "bot.json"
    source.write_text(
        json.dumps({"version": 1, "DATA_DIR": "/old", "REMNAWAVE_METRICS_URL": "https://old.example.test"}),
        encoding="utf-8",
    )
    output = tmp_path / "new-conf"
    layout.migrate(bot_path=source, env_path=None, template_path=TEMPLATE, output_dir=output, zabbix_api_url=URL)
    bot = json.loads((output / "bot.json").read_text(encoding="utf-8"))
    assert bot["DATA_DIR"] == "/old"
    assert bot["ZABBIX_API_URL"] == URL
    assert "REMNAWAVE_METRICS_URL" not in bot
    assert list(output.iterdir()) == [output / "bot.json"]
    assert "REMNAWAVE_METRICS_URL" in json.loads(source.read_text(encoding="utf-8"))
    with pytest.raises(layout.ConfigMigrationError, match="refusing to overwrite"):
        layout.migrate(bot_path=source, env_path=None, template_path=TEMPLATE, output_dir=output, zabbix_api_url=URL)


def test_migration_converts_legacy_public_env(tmp_path: Path) -> None:
    source = tmp_path / "old.env"
    source.write_text("DATA_DIR=/old\nLOG_JSON=true\nDNS_RESOLVERS=8.8.8.8\n", encoding="utf-8")
    output = tmp_path / "conf"
    layout.migrate(bot_path=None, env_path=source, template_path=TEMPLATE, output_dir=output, zabbix_api_url=URL)
    bot = json.loads((output / "bot.json").read_text(encoding="utf-8"))
    assert bot["DATA_DIR"] == "/old"
    assert bot["LOG_JSON"] is True
    assert "DNS_RESOLVERS" not in bot


def test_migration_rolls_back_partial_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    output = tmp_path / "conf"

    def fail(path: Path, payload: bytes) -> None:
        path.write_bytes(payload[:5])
        raise OSError("simulated full disk")

    monkeypatch.setattr(layout, "_write_private", fail)
    with pytest.raises(OSError, match="full disk"):
        layout.write_layout(output, {"version": 1})
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    ("source", "message"),
    [
        ("DATA_DIR=/one\nDATA_DIR=/two\n", "duplicate key"),
        ("ZABBIX_API_TOKEN=private\n", "secret keys"),
        ("UNKNOWN_SETTING=foo\n", "unknown active keys"),
    ],
)
def test_migration_rejects_invalid_public_env(tmp_path: Path, source: str, message: str) -> None:
    path = tmp_path / "old.env"
    path.write_text(source, encoding="utf-8")
    with pytest.raises(layout.ConfigMigrationError, match=message):
        layout.migrate(
            bot_path=None, env_path=path, template_path=TEMPLATE, output_dir=tmp_path / "c", zabbix_api_url=URL
        )
