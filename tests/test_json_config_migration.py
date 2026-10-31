from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools import migrate_config_layout
from tools.migrate_config_layout import ConfigMigrationError, migrate
from tools.migrate_server_inventory import _load_legacy_env

ROOT = Path(__file__).resolve().parents[1]


def _source_files(tmp_path: Path) -> tuple[Path, Path]:
    env = tmp_path / "app.env"
    env.write_text(
        "DATA_DIR=/srv/maintbot/data\n"
        "LOG_JSON=true\n"
        "DNS_RESOLVERS=1.1.1.1,8.8.8.8\n"
        "SERVER_INVENTORY_FILE=/legacy/servers.toml\n",
        encoding="utf-8",
    )
    inventory = tmp_path / "servers.toml"
    inventory.write_text(
        """version = 1
[servers.main]
label = "Main"
[servers.main.connection]
transport = "local"
""",
        encoding="utf-8",
    )
    return env, inventory


def test_migration_creates_bot_file_and_one_json_per_server(tmp_path: Path) -> None:
    env, inventory = _source_files(tmp_path)
    output = tmp_path / "data" / "conf"

    count = migrate(
        env_path=env,
        inventory_path=inventory,
        template_path=ROOT / "examples" / "conf" / "bot.json",
        output_dir=output,
    )

    assert count == 1
    bot = json.loads((output / "bot.json").read_text(encoding="utf-8"))
    assert bot["DATA_DIR"] == "/srv/maintbot/data"
    assert bot["LOG_JSON"] is True
    assert bot["DNS_RESOLVERS"] == ["1.1.1.1", "8.8.8.8"]
    assert "SERVER_INVENTORY_FILE" not in bot
    server_files = list((output / "servers").glob("*.json"))
    assert len(server_files) == 1
    server = json.loads(server_files[0].read_text(encoding="utf-8"))
    assert server["key"] == "main"
    assert server["display_order"] == 10


def test_migration_refuses_to_overwrite_output(tmp_path: Path) -> None:
    env, inventory = _source_files(tmp_path)
    output = tmp_path / "conf"
    output.mkdir()

    with pytest.raises(ConfigMigrationError, match="refusing to overwrite"):
        migrate(
            env_path=env,
            inventory_path=inventory,
            template_path=ROOT / "examples" / "conf" / "bot.json",
            output_dir=output,
        )


def test_migration_rolls_back_partial_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env, inventory = _source_files(tmp_path)
    output = tmp_path / "conf"
    original = migrate_config_layout._write_private
    writes = 0

    def fail_second_write(path: Path, payload: bytes) -> None:
        nonlocal writes
        writes += 1
        if writes == 2:
            raise OSError("simulated full disk")
        original(path, payload)

    monkeypatch.setattr(migrate_config_layout, "_write_private", fail_second_write)

    with pytest.raises(OSError, match="simulated full disk"):
        migrate(
            env_path=env,
            inventory_path=inventory,
            template_path=ROOT / "examples" / "conf" / "bot.json",
            output_dir=output,
        )

    assert not output.exists()
    assert list(tmp_path.glob(".conf.*.tmp")) == []


def test_migration_rejects_duplicate_or_secret_env_keys(tmp_path: Path) -> None:
    env, inventory = _source_files(tmp_path)
    env.write_text("DATA_DIR=/one\nDATA_DIR=/two\n", encoding="utf-8")
    with pytest.raises(ConfigMigrationError, match="duplicate key DATA_DIR"):
        migrate(
            env_path=env,
            inventory_path=inventory,
            template_path=ROOT / "examples" / "conf" / "bot.json",
            output_dir=tmp_path / "first",
        )

    env.write_text("BOT_TOKEN=must-not-copy\n", encoding="utf-8")
    with pytest.raises(ConfigMigrationError, match="secret keys"):
        migrate(
            env_path=env,
            inventory_path=inventory,
            template_path=ROOT / "examples" / "conf" / "bot.json",
            output_dir=tmp_path / "second",
        )


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ("LOCAL_SERVER_CODE=one\nLOCAL_SERVER_CODE=two\n", "duplicate key"),
        ('LOCAL_SERVER_CODE="unterminated\n', "invalid syntax"),
    ],
)
def test_legacy_inventory_stage_rejects_ambiguous_env(
    tmp_path: Path,
    payload: str,
    message: str,
) -> None:
    source = tmp_path / "legacy.env"
    source.write_text(payload, encoding="utf-8")

    with pytest.raises(ValueError, match=message):
        _load_legacy_env(source)


def test_legacy_inventory_stage_rejects_missing_env(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="not a regular file"):
        _load_legacy_env(tmp_path / "missing.env")


def test_migration_converts_old_remote_nodes_without_ssh_configuration(tmp_path: Path) -> None:
    env, inventory = _source_files(tmp_path)
    with env.open("a", encoding="utf-8") as stream:
        stream.write("SSH_IDENTITY_FILE=/old/key\nSSH_KNOWN_HOSTS_FILE=/old/hosts\nSSH_STRICT_HOST_KEY_CHECKING=yes\n")
    with inventory.open("a", encoding="utf-8") as stream:
        stream.write("""
[servers.remote]
label = "Remote"
[servers.remote.connection]
transport = "ssh"
target = "user@example.com"
[servers.remote.monitoring]
source = "remnawave"
node_uuid = "00000000-0000-0000-0000-000000000001"
[servers.remote.docker]
containers = ["api"]
[servers.remote.fail2ban]
enabled = true
""")
    output = tmp_path / "conf"

    assert (
        migrate(
            env_path=env,
            inventory_path=inventory,
            template_path=ROOT / "examples/conf/bot.json",
            output_dir=output,
        )
        == 2
    )

    bot = json.loads((output / "bot.json").read_text(encoding="utf-8"))
    assert not any(key.startswith("SSH_") for key in bot)
    remote = json.loads((output / "servers/020-remote.json").read_text(encoding="utf-8"))
    assert remote["connection"] == {"transport": "panel"}
    assert remote["docker"]["containers"] == []
    assert remote["fail2ban"]["enabled"] is False


def test_migration_rejects_ssh_only_inventory_without_writing_output(tmp_path: Path) -> None:
    env, inventory = _source_files(tmp_path)
    inventory.write_text(
        'version=1\n[servers.remote]\nlabel="Remote"\n[servers.remote.connection]\ntransport="ssh"\n',
        encoding="utf-8",
    )
    output = tmp_path / "conf"
    with pytest.raises(ConfigMigrationError, match="configure a RemnaWave node"):
        migrate(
            env_path=env,
            inventory_path=inventory,
            template_path=ROOT / "examples/conf/bot.json",
            output_dir=output,
        )
    assert not output.exists()


def test_migration_rejects_malformed_legacy_remote_fail2ban(tmp_path: Path) -> None:
    source = tmp_path / "servers.toml"
    source.write_text(
        """version=1
[servers.remote]
label="Remote"
fail2ban=false
[servers.remote.connection]
transport="ssh"
[servers.remote.monitoring]
source="remnawave"
node_uuid="00000000-0000-0000-0000-000000000001"
""",
        encoding="utf-8",
    )
    with pytest.raises(ConfigMigrationError, match="invalid fail2ban block"):
        migrate_config_layout.load_toml_servers(source)
