from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from app.config.json_files import JsonConfigError, load_json_object
from app.config.schema import AppSettings
from tools import migrate_runtime_env as env


@pytest.mark.parametrize("value", ["NaN", "Infinity", "1e999", "-1e999"])
def test_nonfinite_json_is_rejected(tmp_path, value):
    path = tmp_path / "bot.json"
    path.write_text('{"version":1,"value":' + value + "}", encoding="utf-8")
    with pytest.raises(JsonConfigError):
        load_json_object(path, field_name="test config")


@pytest.mark.parametrize("version", [True, 1.0])
def test_json_version_is_an_integer_not_a_coerced_literal(tmp_path, version):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"version": version}), encoding="utf-8")
    with pytest.raises(JsonConfigError):
        load_json_object(path, field_name="test config")


@pytest.mark.parametrize(
    "url",
    [
        "https://example.test:bad/api_jsonrpc.php",
        "https://exa mple.test/api_jsonrpc.php",
        "https://example.test/\x00",
        "http://example.test/api_jsonrpc.php",
        "https://user:password@example.test/api_jsonrpc.php",
        "https://example.test/api_jsonrpc.php?token=secret",
        "https://example.test/",
    ],
)
def test_invalid_zabbix_url_fails_preflight(url):
    with pytest.raises(ValidationError, match="ZABBIX_API_URL"):
        AppSettings(ZABBIX_API_URL=url)


def test_zabbix_frontend_subdirectory_is_supported():
    url = "https://example.test:8443/zabbix/api_jsonrpc.php"
    assert url == AppSettings(ZABBIX_API_URL=url).ZABBIX_API_URL


def test_env_writer_syncs_before_success(tmp_path, monkeypatch):
    synced = []
    monkeypatch.setattr(env.os, "fsync", synced.append)
    env._exclusive_write(tmp_path / "example.env", "TOKEN=synthetic", mode=0o600)
    assert synced


def test_legacy_env_stage_discards_retired_ssh_settings(tmp_path):
    source = tmp_path / "legacy.env"
    source.write_text(
        "DATA_DIR=/old/data\nSSH_IDENTITY_FILE=/old/key\nSSH_KNOWN_HOSTS_FILE=/old/hosts\n"
        "SSH_STRICT_HOST_KEY_CHECKING=yes\n",
        encoding="utf-8",
    )
    public_template = tmp_path / "public.example"
    public_template.write_text("DATA_DIR=/example/data\n", encoding="utf-8")
    secrets_template = tmp_path / "secrets.example"
    secrets_template.write_text("ZABBIX_API_TOKEN=\n", encoding="utf-8")
    secrets_source = tmp_path / "secrets.env"
    secrets_source.write_text("", encoding="utf-8")

    migrated = env.build_migration(
        public_env=source,
        secrets_env=secrets_source,
        public_template=public_template,
        secrets_template=secrets_template,
    )

    assert "DATA_DIR='/old/data'" in migrated.public_text
    assert "SSH_" not in migrated.public_text
