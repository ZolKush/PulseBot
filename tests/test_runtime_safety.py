from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config.schema import AppSettings
from app.config.secrets import load_required_secrets
from app.monitoring.system.ufw import _parse_ufw_status
from app.runtime.lock import InstanceAlreadyRunning, SingleInstanceLock


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Status: inactive", "inactive"),
        ("Status: active", "active"),
        ("Статус: неактивен", "inactive"),
        ("Статус: активен", "active"),
        ("permission denied", "н/д"),
    ],
)
def test_ufw_status_does_not_confuse_inactive_with_active(raw: str, expected: str) -> None:
    assert _parse_ufw_status(raw) == expected


def test_legacy_positional_server_settings_are_rejected() -> None:
    with pytest.raises(ValidationError, match="REMOTE_SERVER_SSH_TARGETS"):
        AppSettings(REMOTE_SERVER_SSH_TARGETS=["bot@one.example"])


def test_invalid_secret_is_not_exposed_in_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    secret = "secret-X"
    path = tmp_path / "secrets.env"
    path.write_text(f"BOT_TOKEN=123456:test-token\nADMIN_PASSWORD={secret}\n", encoding="utf-8")
    monkeypatch.delenv("BOT_TOKEN", raising=False)
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)

    with pytest.raises(RuntimeError) as captured:
        load_required_secrets(path)

    assert secret not in str(captured.value)
    assert captured.value.__cause__ is None


def test_equal_admin_and_owner_secrets_are_not_exposed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    secret = "same-generated-password-secret"
    path = tmp_path / "secrets.env"
    path.write_text(
        f"BOT_TOKEN=123456:test-token\nADMIN_PASSWORD={secret}\nOWNER_PASSWORD={secret}\n",
        encoding="utf-8",
    )
    for key in ("BOT_TOKEN", "ADMIN_PASSWORD", "OWNER_PASSWORD"):
        monkeypatch.delenv(key, raising=False)

    with pytest.raises(RuntimeError) as captured:
        load_required_secrets(path)

    error = str(captured.value)
    assert secret not in error
    assert "ADMIN_PASSWORD" in error
    assert "OWNER_PASSWORD" in error


def test_unknown_secret_key_is_rejected_without_exposing_its_value(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = "SECRET_VALUE_MUST_NOT_BE_LOGGED"
    path = tmp_path / "secrets.env"
    path.write_text(
        "BOT_TOKEN=123456:test-token\n"
        "ADMIN_PASSWORD=admin-password-long-enough\n"
        "OWNER_PASSWORD=owner-password-long-enough\n"
        f"OLD_BOT_TOKEN={marker}\n",
        encoding="utf-8",
    )
    for key in ("BOT_TOKEN", "ADMIN_PASSWORD", "OWNER_PASSWORD", "OLD_BOT_TOKEN"):
        monkeypatch.delenv(key, raising=False)

    with pytest.raises(RuntimeError, match="OLD_BOT_TOKEN") as captured:
        load_required_secrets(path)

    assert marker not in str(captured.value)


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (
            "BOT_TOKEN=first\nBOT_TOKEN=second\nADMIN_PASSWORD=admin-password-long-enough\n"
            "OWNER_PASSWORD=owner-password-long-enough\n",
            "Повторяющийся ключ.*BOT_TOKEN",
        ),
        (
            'BOT_TOKEN=token\nADMIN_PASSWORD="unterminated\nOWNER_PASSWORD=owner-password-long-enough\n',
            "Некорректный синтаксис",
        ),
        (
            "BOT_TOKEN=token\nADMIN_PASSWORD=admin-password-long-enough\n"
            "OWNER_PASSWORD=owner-password-long-enough\nUNKNOWN_KEY=\n",
            "UNKNOWN_KEY",
        ),
    ],
)
def test_secret_file_rejects_duplicates_syntax_errors_and_empty_unknown_keys(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    payload: str,
    message: str,
) -> None:
    path = tmp_path / "secrets.env"
    path.write_text(payload, encoding="utf-8")
    for key in ("BOT_TOKEN", "ADMIN_PASSWORD", "OWNER_PASSWORD"):
        monkeypatch.delenv(key, raising=False)

    with pytest.raises(RuntimeError, match=message):
        load_required_secrets(path)


def test_remnawave_credentials_must_be_configured_as_a_pair(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "secrets.env"
    path.write_text(
        "BOT_TOKEN=123456:test-token\n"
        "ADMIN_PASSWORD=admin-password-long-enough\n"
        "OWNER_PASSWORD=owner-password-long-enough\n"
        "REMNAWAVE_METRICS_USER=metrics-user\n",
        encoding="utf-8",
    )
    for key in (
        "BOT_TOKEN",
        "ADMIN_PASSWORD",
        "OWNER_PASSWORD",
        "REMNAWAVE_METRICS_USER",
        "REMNAWAVE_METRICS_PASS",
    ):
        monkeypatch.delenv(key, raising=False)

    with pytest.raises(RuntimeError, match="должны быть заданы вместе"):
        load_required_secrets(path)


def test_app_settings_validation_does_not_expose_dotenv_secrets() -> None:
    secret_marker = "THIS_SECRET_MUST_NEVER_REACH_VALIDATION_LOGS"

    with pytest.raises(ValidationError) as exc_info:
        AppSettings.model_validate(
            {
                "REMOTE_SERVER_ENABLED": True,
                "REMOTE_SERVER_SSH_TARGETS": ["root@example.test"],
                "ADMIN_PASSWORD": secret_marker,
            }
        )

    error = str(exc_info.value)
    assert secret_marker not in error
    assert "input_value" not in error


def test_invalid_timezone_is_reported_as_validation_error() -> None:
    with pytest.raises(ValidationError, match="unknown .*timezone"):
        AppSettings(TZ="Invalid/Nowhere")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("NAVIGATION_RETENTION_HOURS", 0),
        ("NAVIGATION_RETENTION_HOURS", 37),
        ("NAVIGATION_CLEANUP_INTERVAL_SEC", 59),
        ("NAVIGATION_CLEANUP_INTERVAL_SEC", 3601),
    ],
)
def test_message_cleanup_settings_stay_inside_telegram_deletion_window(field: str, value: int) -> None:
    with pytest.raises(ValidationError, match=field):
        AppSettings(**{field: value})


def test_single_instance_lock_is_first_process_wins(tmp_path: Path) -> None:
    path = tmp_path / "maintbot.lock"
    first = SingleInstanceLock(path)
    second = SingleInstanceLock(path)
    first.acquire()
    try:
        with pytest.raises(InstanceAlreadyRunning):
            second.acquire()
    finally:
        first.release()

    second.acquire()
    second.release()
