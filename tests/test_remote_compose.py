from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ROOT / "deploy/compose.remote.yaml"


def test_remote_build_uses_pulsebot_dev_with_an_optional_pinned_reference():
    document = COMPOSE.read_text(encoding="utf-8")

    assert "context: https://github.com/ZolKush/PulseBot.git#${MAINTBOT_SOURCE_REF:-dev}" in document
    assert "pull_policy: build" in document


def test_remote_compose_inherits_image_startup_and_allows_explicit_maintenance_commands():
    document = COMPOSE.read_text(encoding="utf-8")

    # The shared image entrypoint owns migration/preflight for normal startup
    # and executes explicit CLI commands directly; test_docker_entrypoint.py
    # exercises both paths. Compose must not replace either image default.
    assert "entrypoint:" not in document
    assert "command:" not in document
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert 'ENTRYPOINT ["/bin/sh", "/usr/local/bin/maintbot-entrypoint"]' in dockerfile
    assert 'CMD ["python", "-m", "app.launcher"]' in dockerfile


def test_remote_and_prebuilt_deployments_share_state_mounts_migration_environment_and_security():
    remote = COMPOSE.read_text(encoding="utf-8")
    prebuilt = (ROOT / "deploy/compose.image.yaml").read_text(encoding="utf-8")

    # Only image acquisition differs. Storage paths, MAINTBOT_PTB_PICKLE,
    # read-only config/secrets, nonprivileged settings and shutdown stay equal.
    assert remote.split("    restart:", 1)[1] == prebuilt.split("    restart:", 1)[1]
    assert "MAINTBOT_PTB_PICKLE: ${MAINTBOT_PTB_PICKLE:-}" in remote
    for source, target in (
        ("./data", "/var/lib/maintbot"),
        ("./data/conf", "/etc/maintbot/conf"),
        ("./data/env.secrets", "/run/secrets/maintbot.env"),
    ):
        assert f"source: {source}\n        target: {target}" in remote
