from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
ROOT_DIR = BASE_DIR.parent


def _resolve_bootstrap_path(value: str, default: Path) -> Path:
    """Resolve the very small environment-backed bootstrap surface."""

    raw = value.strip()
    if not raw:
        return default
    path = Path(raw)
    return path if path.is_absolute() else ROOT_DIR / path


# Non-secret runtime settings are deliberately file based.  Only the directory
# containing those files is a process-environment bootstrap value; the bot does
# not need one environment variable per setting or per server.
CONFIG_DIR = _resolve_bootstrap_path(os.getenv("MAINTBOT_CONFIG_DIR", ""), ROOT_DIR / "data" / "conf")
BOT_CONFIG_FILE = CONFIG_DIR / "bot.json"

_SECRETS_ENV_PATH = os.getenv("SECRETS_ENV_PATH", "").strip()
SECRETS_ENV_FILE = _resolve_bootstrap_path(_SECRETS_ENV_PATH, BASE_DIR / "env.secrets")

__all__ = [
    "BASE_DIR",
    "BOT_CONFIG_FILE",
    "CONFIG_DIR",
    "ROOT_DIR",
    "SECRETS_ENV_FILE",
]
