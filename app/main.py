"""Thin entry point for the MaintBot Telegram application."""

from __future__ import annotations

import sys
from pathlib import Path

if __package__ in (None, ""):
    _PROJECT_ROOT = Path(__file__).resolve().parent.parent
    if str(_PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(_PROJECT_ROOT))

from telegram.ext import Application

from app.bot.application import build_application
from app.bot.application import run_application as _run_application
from app.bot.conversations import NavigableConversationHandler
from app.bot.errors import blocked_user_guard
from app.runtime.lock import SingleInstanceLock

__all__ = [
    "NavigableConversationHandler",
    "blocked_user_guard",
    "build_app",
    "main",
    "run_application",
]


def build_app() -> Application:
    """Build the Telegram application from the current runtime settings."""
    return build_application()


def run_application(*, instance_lock: SingleInstanceLock | None = None) -> None:
    """Run polling through the canonical application module."""
    _run_application(instance_lock=instance_lock)


def main() -> None:
    # Keep every process entry point on the same startup path.  Importing the
    # launcher lazily preserves this module's side-effect-free import contract
    # while ensuring storage recovery happens only after the instance lock.
    from app.launcher import main as launch

    launch()


if __name__ == "__main__":
    main()
