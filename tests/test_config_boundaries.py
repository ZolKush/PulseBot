from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from app.config.json_files import JsonConfigError, load_json_object
from app.config.schema import AppSettings
from tools import migrate_config_layout as layout
from tools import migrate_runtime_env as env
from tools import migrate_server_inventory as inventory


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


@pytest.mark.parametrize("url", ["https://example.test:bad/", "https://exa mple.test/", "https://example.test/\x00"])
def test_invalid_metrics_url_fails_preflight(url):
    with pytest.raises(ValidationError):
        AppSettings(REMNAWAVE_METRICS_URL=url)


def test_nested_toml_identity_cannot_override_the_validated_key(tmp_path):
    source = tmp_path / "inventory.toml"
    source.write_text('version=1\n[servers.safe]\nkey="nested/name"\nlabel="test"\n', encoding="utf-8")
    with pytest.raises(layout.ConfigMigrationError):
        layout.load_toml_servers(source)


def test_layout_validates_all_keys_before_creating_any_output(tmp_path):
    with pytest.raises(layout.ConfigMigrationError):
        layout.write_layout(tmp_path / "conf", {"version": 1}, [{"version": 1, "key": "nested/name"}])
    assert not (tmp_path / "conf").exists()
    assert not list(tmp_path.glob(".conf.*.tmp"))


def test_single_remote_legacy_uuid_is_preserved():
    uuid = "00000000-0000-0000-0000-000000000001"
    servers = inventory.migrate(
        {"REMOTE_SERVER_SSH_TARGET": "user@host", "REMOTE_SERVER_REMNAWAVE_UUID": uuid}, fallbacks={}
    )
    assert servers[1]["node_uuid"] == uuid
    assert servers[1]["monitoring_source"] == "remnawave"
    assert servers[1]["transport"] == "panel"
    assert servers[1]["containers"] == []
    assert not servers[1]["fail2ban_enabled"]
    assert "target" not in servers[1]
    assert 'transport = "ssh"' not in inventory.render(servers)


def test_host_only_legacy_remote_server_requires_panel_configuration():
    with pytest.raises(ValueError, match="configure a RemnaWave UUID"):
        inventory.migrate({"REMOTE_SERVER_SSH_TARGET": "user@host"}, fallbacks={})


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
    public_template.write_text("DATA_DIR=/example/data\nSERVER_INVENTORY_FILE=\n", encoding="utf-8")
    secrets_template = tmp_path / "secrets.example"
    secrets_template.write_text("REMNAWAVE_METRICS_USER=\nREMNAWAVE_METRICS_PASS=\n", encoding="utf-8")
    secrets_source = tmp_path / "secrets.env"
    secrets_source.write_text("", encoding="utf-8")

    migrated = env.build_migration(
        public_env=source,
        secrets_env=secrets_source,
        public_template=public_template,
        secrets_template=secrets_template,
        server_inventory_file="/new/servers.toml",
    )

    assert "DATA_DIR='/old/data'" in migrated.public_text
    assert "SSH_" not in migrated.public_text
