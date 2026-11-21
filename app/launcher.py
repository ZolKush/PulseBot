from __future__ import annotations

from .config import (
    ADMIN_PASSWORD,
    BOT_TOKEN,
    DATA_DIR,
    INSTANCE_LOCK_PATH,
    LOG_JSON,
    LOG_LEVEL,
    OWNER_PASSWORD,
    ZABBIX_API_TOKEN,
)
from .runtime.lock import ALREADY_RUNNING_EXIT_CODE, InstanceAlreadyRunning, SingleInstanceLock
from .runtime.logging import configure_logging, logger


def main() -> None:
    configure_logging(
        level=LOG_LEVEL,
        use_json=LOG_JSON,
        force=True,
        secrets=(BOT_TOKEN, ADMIN_PASSWORD, OWNER_PASSWORD, ZABBIX_API_TOKEN),
    )
    lock = SingleInstanceLock(INSTANCE_LOCK_PATH)
    try:
        lock.acquire()
    except InstanceAlreadyRunning as exc:
        logger.warning("MaintBot не запущен: %s", exc)
        raise SystemExit(ALREADY_RUNNING_EXIT_CODE) from exc
    try:
        # Storage recovery is allowed only after the process owns the instance
        # lock. Imports themselves remain free of filesystem side effects.
        from .storage import initialize_storage

        initialize_storage(DATA_DIR)

        # Import handlers only after the SQLite database has been verified. The
        # application module does not import this launcher, so ``app.main`` can
        # safely delegate here without creating an internal import cycle.
        from .bot.application import run_application

        run_application(instance_lock=lock)
    finally:
        lock.release()


if __name__ == "__main__":
    main()
