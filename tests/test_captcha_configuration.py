from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest
from pydantic import ValidationError

from app.config.schema import AppSettings, load_app_settings

ROOT = Path(__file__).resolve().parents[1]


def test_existing_v1_config_keeps_captcha_disabled(tmp_path):
    doc = json.loads((ROOT / "examples/conf/bot.json").read_text(encoding="utf-8"))
    path = tmp_path / "bot.json"
    path.write_text(json.dumps({k: v for k, v in doc.items() if not k.startswith(("CAPTCHA_", "TURNSTILE_"))}))
    settings = load_app_settings(path)
    assert not settings.CAPTCHA_ENABLED
    assert settings.CAPTCHA_BIND_HOST == "127.0.0.1"
    assert settings.CAPTCHA_PORT == 8094


@pytest.mark.parametrize(
    "overrides",
    [
        {"CAPTCHA_URL": "http://verify.example.org"},
        {"CAPTCHA_URL": "https://user@verify.example.org"},
        {"CAPTCHA_URL": "https://verify.example.org/path"},
        {"CAPTCHA_URL": "https://verify.example.org?key=value"},
        {"CAPTCHA_URL": "https://verify.example.org\\foo"},
        {"CAPTCHA_PORT": 65536},
        {"CAPTCHA_PORT": True},
        {"TURNSTILE_SITE_KEY": 'key"attribute'},
    ],
)
def test_invalid_captcha_settings(overrides):
    with pytest.raises(ValidationError):
        AppSettings.model_validate({"ZABBIX_API_URL": "https://z.example.org/api_jsonrpc.php", **overrides})


def test_origin_matches_browser_serialization():
    settings = AppSettings(
        ZABBIX_API_URL="https://z.example.org/api_jsonrpc.php", CAPTCHA_URL="https://VÉRIFY.example:443/"
    )
    assert settings.CAPTCHA_URL == "https://xn--vrify-bsa.example"


def test_enabled_without_secret_stops_before_polling(tmp_path):
    doc = json.loads((ROOT / "examples/conf/bot.json").read_text(encoding="utf-8"))
    doc.update(CAPTCHA_ENABLED=True, CAPTCHA_URL="https://verify.example.org", TURNSTILE_SITE_KEY="test-site")
    (tmp_path / "bot.json").write_text(json.dumps(doc))
    env = {**os.environ, "MAINTBOT_CONFIG_DIR": str(tmp_path), "PYTHONUTF8": "1", "TURNSTILE_SECRET_KEY": ""}
    result = subprocess.run(
        [sys.executable, "-m", "app.config_check"], env=env, cwd=ROOT, capture_output=True, encoding="utf-8"
    )
    assert result.returncode == 1
    assert "TURNSTILE_SECRET_KEY" in result.stderr


@pytest.mark.asyncio
async def test_optional_listener_follows_bot_lifecycle(monkeypatch):
    from app.bot import application as assembly
    from app.bot import jobs

    server = AsyncMock()
    factory = Mock(return_value=server)
    monkeypatch.setattr(assembly, "CaptchaServer", factory)
    monkeypatch.setattr(assembly, "NAVIGATION_CLEANUP_ENABLED", False)
    monkeypatch.setattr(assembly, "CAPTCHA_ENABLED", False)
    disabled = assembly.build_application()
    await disabled.post_init(disabled)
    factory.assert_not_called()

    monkeypatch.setattr(assembly, "CAPTCHA_ENABLED", True)
    monkeypatch.setattr(jobs, "CAPTCHA_ENABLED", True)
    enabled = assembly.build_application()
    assert enabled.job_queue.get_jobs_by_name("captcha_cleanup")
    server.start.assert_not_awaited()
    await enabled.post_init(enabled)
    server.start.assert_awaited_once_with("127.0.0.1", 8094)
    await enabled.post_stop(enabled)
    await enabled.post_shutdown(enabled)
    server.close.assert_awaited_once()
