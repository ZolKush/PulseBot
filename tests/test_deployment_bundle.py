from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from app.persistence import SplitJsonBackend
from tools.deployment_config import DeploymentPreparationError
from tools.prepare_deployment import main, prepare_deployment

ROOT = Path(__file__).resolve().parents[1]


def _json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    data = project / "data"
    data.mkdir(parents=True)
    SplitJsonBackend(data).bootstrap()
    (data / ".hidden").write_bytes(b"preserve-hidden-file")
    bot = json.loads((ROOT / "examples/conf/docker-bot.json").read_text(encoding="utf-8"))
    bot.update(
        DATA_DIR="/opt/old/data",
        PTB_PERSISTENCE_PATH="/opt/old/data/telegram/persistence.pickle",
        SSH_IDENTITY_FILE="/old/key",
        SSH_KNOWN_HOSTS_FILE="/old/known_hosts",
        SSH_STRICT_HOST_KEY_CHECKING="yes",
    )
    _json(data / "conf/bot.json", bot)
    server = json.loads((ROOT / "examples/conf/docker-server.json").read_text(encoding="utf-8"))
    server["connection"] = {"transport": "ssh", "target": "maintbot@example.com"}
    server["docker"] = {"containers": ["api"]}
    server["fail2ban"]["enabled"] = True
    _json(data / "conf/servers/remote.json", server)
    _json(
        data / "conf/servers/local.json",
        {"version": 1, "key": "local", "label": "Local", "connection": {"transport": "local"}},
    )
    telegram = data / "telegram/persistence.pickle"
    telegram.parent.mkdir()
    telegram.write_bytes(b"opaque-trusted-telegram-image")
    secret = project / "app/env.secrets"
    secret.parent.mkdir()
    secret.write_bytes(b"BOT_TOKEN=synthetic-test-only\n")
    template = project / "examples/conf/docker-bot.json"
    template.parent.mkdir(parents=True)
    template.write_bytes((ROOT / "examples/conf/docker-bot.json").read_bytes())
    compose = project / "deploy/compose.remote.yaml"
    compose.parent.mkdir()
    compose.write_bytes(b"services:\n  maintbot:\n    image: synthetic-test-only\n")
    (project / "deploy/compose.image.yaml").write_bytes((ROOT / "deploy/compose.image.yaml").read_bytes())
    return project


def _files(root: Path) -> dict[str, bytes]:
    return {path.relative_to(root).as_posix(): path.read_bytes() for path in root.rglob("*") if path.is_file()}


def test_bundle_copies_all_state_and_adapts_only_copied_config(tmp_path: Path) -> None:
    project = _project(tmp_path)
    before = _files(project)
    output = tmp_path / "release"

    report = prepare_deployment(project, output, panel_only=True)

    assert _files(project) == before
    assert report.source_format == "split-json-v1"
    assert report.server_count == 1
    assert report.omitted_host_servers == 1
    assert report.telegram_state_present
    assert sorted(path.name for path in output.iterdir()) == ["compose.yaml", "data"]
    assert (output / "compose.yaml").read_bytes() == (project / "deploy/compose.remote.yaml").read_bytes()
    for relative in ("storage_layout.json", "users/profiles.json", ".hidden", "telegram/persistence.pickle"):
        assert (output / "data" / relative).read_bytes() == (project / "data" / relative).read_bytes()
    assert (output / "data/env.secrets").read_bytes() == (project / "app/env.secrets").read_bytes()
    assert (output / "data/telegram-migration.pickle").read_bytes() == (
        project / "data/telegram/persistence.pickle"
    ).read_bytes()
    bot = json.loads((output / "data/conf/bot.json").read_text(encoding="utf-8"))
    assert bot["DATA_DIR"] == "/var/lib/maintbot"
    assert bot["INSTANCE_LOCK_PATH"] == bot["PTB_PERSISTENCE_PATH"] == ""
    assert not any(key.startswith("SSH_") for key in bot)
    assert bot["TZ"] == "Europe/Moscow"
    server = json.loads((output / "data/conf/servers/remote.json").read_text(encoding="utf-8"))
    assert server["key"] == "panel"
    assert server["monitoring"]["node_uuid"] == "00000000-0000-0000-0000-000000000001"
    assert server["connection"] == {"transport": "panel"}
    assert server["docker"] == {"containers": []}
    assert server["fail2ban"]["enabled"] is False
    assert not (output / "data/conf/servers/local.json").exists()
    backup = output / "data" / report.original_config_backup
    assert (backup / "bot.json").read_bytes() == (project / "data/conf/bot.json").read_bytes()
    assert (backup / "servers/local.json").is_file()


@pytest.mark.parametrize(
    "image", ["registry.example.com:5000/acme/team/maintbot:fixed-20261029", "maintbot@sha256:" + "a" * 64]
)
def test_loaded_image_bundle_pins_the_image_without_remote_build_or_pull(tmp_path: Path, image: str) -> None:
    project = _project(tmp_path)
    (project / "deploy/compose.remote.yaml").unlink()
    before = _files(project)
    output = tmp_path / "release"

    report = prepare_deployment(project, output, panel_only=True, image=image)

    assert _files(project) == before
    assert report.source_format == "split-json-v1"
    assert report.telegram_state_present
    compose = (output / "compose.yaml").read_text(encoding="utf-8")
    assert f'image: "{image}"' in compose
    assert "pull_policy: never" in compose
    assert "build:" not in compose
    assert "MAINTBOT_IMAGE" not in compose
    assert "entrypoint:" not in compose
    assert "command:" not in compose
    for relative in ("storage_layout.json", "users/profiles.json", ".hidden", "telegram/persistence.pickle"):
        assert (output / "data" / relative).read_bytes() == (project / "data" / relative).read_bytes()
    assert (output / "data/env.secrets").read_bytes() == (project / "app/env.secrets").read_bytes()
    assert (output / "data/telegram-migration.pickle").read_bytes() == (
        project / "data/telegram/persistence.pickle"
    ).read_bytes()


@pytest.mark.parametrize(
    "image",
    [
        "",
        "-bad",
        "maintbot:two:colons",
        "registry.example.com:5000/team:6000/maintbot:fixed",
        "registry_bad:5000/maintbot:fixed",
        "a" * 256,
        "${PRIVATE_IMAGE}",
        "maintbot:fixed\nbuild: .",
        "maintbot:fixed;PRIVATE",
    ],
)
def test_invalid_image_reference_is_refused_without_output_or_value_disclosure(tmp_path: Path, image: str) -> None:
    project = _project(tmp_path)
    before = _files(project)
    output = tmp_path / "release"

    with pytest.raises(DeploymentPreparationError, match="valid Docker image reference") as caught:
        prepare_deployment(project, output, panel_only=True, image=image)

    assert "PRIVATE" not in str(caught.value)
    assert _files(project) == before
    assert not output.exists()


def test_cli_image_option_selects_the_loaded_image_template(tmp_path: Path, monkeypatch, capsys) -> None:
    project = _project(tmp_path)
    output = tmp_path / "release"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "prepare_deployment",
            "--project-dir",
            str(project),
            "--output",
            str(output),
            "--panel-only",
            "--image",
            "maintbot:fixed-test",
        ],
    )

    assert main() == 0

    report = json.loads(capsys.readouterr().out)
    assert report["output_dir"] == str(output)
    assert 'image: "maintbot:fixed-test"' in (output / "compose.yaml").read_text(encoding="utf-8")


def test_host_only_omission_requires_explicit_flag(tmp_path: Path) -> None:
    project = _project(tmp_path)
    output = tmp_path / "release"
    with pytest.raises(DeploymentPreparationError, match="--panel-only"):
        prepare_deployment(project, output)
    assert not output.exists()


def test_zero_panel_nodes_is_refused(tmp_path: Path) -> None:
    project = _project(tmp_path)
    (project / "data/conf/servers/remote.json").unlink()
    with pytest.raises(DeploymentPreparationError, match="no configured RemnaWave"):
        prepare_deployment(project, tmp_path / "release", panel_only=True)


@pytest.mark.parametrize("target", ["existing", "data", "data/nested"])
def test_refuses_existing_output_and_source_descendants(tmp_path: Path, target: str) -> None:
    project = _project(tmp_path)
    output = project / target
    if target == "existing":
        output.mkdir()
    with pytest.raises(DeploymentPreparationError, match="refusing"):
        prepare_deployment(project, output, panel_only=True)


def test_missing_split_manifest_is_not_treated_as_empty_installation(tmp_path: Path) -> None:
    project = _project(tmp_path)
    (project / "data/storage_layout.json").unlink()
    with pytest.raises(DeploymentPreparationError, match="storage_layout.json"):
        prepare_deployment(project, tmp_path / "release", panel_only=True)


def test_monolithic_source_is_refused_without_modification_or_output(tmp_path: Path) -> None:
    project = tmp_path / "project"
    data = project / "data"
    data.mkdir(parents=True)
    (data / "user_data.json").write_bytes(b"{}")
    (data / "important_data.json").write_bytes(b"{}")
    before = _files(project)
    output = tmp_path / "release"

    with pytest.raises(DeploymentPreparationError, match="storage_layout.json"):
        prepare_deployment(project, output, panel_only=True)

    assert _files(project) == before
    assert not output.exists()


def test_explicit_custom_pickle_is_preserved_under_canonical_name(tmp_path: Path) -> None:
    project = _project(tmp_path)
    selected = project / "app/custom-state.pickle"
    selected.write_bytes(b"selected-state")
    report = prepare_deployment(project, tmp_path / "release", panel_only=True, ptb_pickle=selected)
    assert report.telegram_state_present
    assert (tmp_path / "release/data/telegram-migration.pickle").read_bytes() == b"selected-state"
    assert selected.read_bytes() == b"selected-state"


def test_missing_configured_pickle_requires_explicit_resolution(tmp_path: Path) -> None:
    project = _project(tmp_path)
    (project / "data/telegram/persistence.pickle").unlink()
    with pytest.raises(DeploymentPreparationError, match="configured Telegram state is absent"):
        prepare_deployment(project, tmp_path / "release", panel_only=True)


def test_bad_uuid_does_not_expose_configuration_values(tmp_path: Path) -> None:
    project = _project(tmp_path)
    path = project / "data/conf/servers/remote.json"
    server = json.loads(path.read_text(encoding="utf-8"))
    server["monitoring"]["node_uuid"] = "PRIVATE-UUID-VALUE"
    _json(path, server)
    with pytest.raises(DeploymentPreparationError) as caught:
        prepare_deployment(project, tmp_path / "release", panel_only=True)
    assert "PRIVATE-UUID-VALUE" not in str(caught.value)


def test_source_directory_symlink_is_refused(tmp_path: Path) -> None:
    project = _project(tmp_path)
    target = project / "data/linked"
    try:
        target.symlink_to(project / "app", target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks require platform support")
    with pytest.raises(DeploymentPreparationError, match="symbolic links"):
        prepare_deployment(project, tmp_path / "release", panel_only=True)
