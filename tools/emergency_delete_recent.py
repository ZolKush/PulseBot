"""Резервная очистка явного диапазона Telegram ID в известных личных чатах.

Запуск из корня checkout: python -m tools.emergency_delete_recent --help
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TextIO

from telegram import Bot
from telegram.request import HTTPXRequest

from app.runtime.lock import ALREADY_RUNNING_EXIT_CODE, InstanceAlreadyRunning, SingleInstanceLock
from app.runtime.logging import configure_logging
from tools._emergency_delete import RangeDeleter, cancel_pending_broadcasts, known_recipient_ids


def _positive(raw: str) -> int:
    value = int(raw)
    if value < 1:
        raise argparse.ArgumentTypeError("значение должно быть положительным")
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--min-message-id", type=_positive, required=True, help="нижняя граница включительно")
    parser.add_argument("--max-message-id", type=_positive, required=True, help="верхняя граница включительно")
    scope = parser.add_mutually_exclusive_group(required=True)
    scope.add_argument("--all-chats", action="store_true", help="все сохранённые профили, включая отключённые")
    scope.add_argument("--chat-id", type=_positive, action="append", help="личный чат; можно повторять")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--execute", action="store_true", help="выполнить необратимое удаление в указанном диапазоне")
    mode.add_argument("--dry-run", action="store_true", help="показать план без Telegram-запросов (по умолчанию)")
    parser.add_argument("--attempts", type=_positive, default=5, help="попыток на запрос, по умолчанию 5")
    parser.add_argument("--batch-size", type=_positive, default=100, help="ID на запрос, 1..100")
    parser.add_argument("--read-timeout", type=_positive, default=60, help="таймаут чтения ответа в секундах")
    parser.add_argument("--report", type=Path, help="новый JSONL-файл отчёта; существующий не перезаписывается")
    parser.add_argument(
        "--cancel-pending-broadcasts",
        action="store_true",
        help="при --execute также отменить ВСЕ оставшиеся admin_broadcast в outbox",
    )
    return parser


class Reporter:
    def __init__(self, stream: TextIO | None, secrets: tuple[str, ...]) -> None:
        self.stream = stream
        self.secrets = tuple(sorted({value for value in secrets if value}, key=len, reverse=True))

    def __call__(self, event: str, **fields: Any) -> None:
        record = {"time": datetime.now(timezone.utc).isoformat(), "event": event, **fields}
        # Escape secrets the same way as the JSON payload, including special characters.
        line = json.dumps(record, ensure_ascii=False)
        for secret in self.secrets:
            line = line.replace(json.dumps(secret, ensure_ascii=False)[1:-1], "[REDACTED]")
        print(line, flush=True)
        if self.stream is not None:
            self.stream.write(line + "\n")
            self.stream.flush()


def _private_report(path: Path) -> TextIO:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        return os.fdopen(fd, "w", encoding="utf-8")
    except BaseException:
        os.close(fd)
        raise


def _run_locked(args: argparse.Namespace, token: str, data_dir: Path | str, emit: Reporter) -> int:
    from app.storage import authorized_users_snapshot, initialize_storage

    initialize_storage(data_dir)
    chats = sorted(set(args.chat_id)) if args.chat_id else known_recipient_ids(authorized_users_snapshot())
    if not chats:
        emit("ERROR", reason="Не найдено личных чатов в хранилище")
        return 1
    lower, upper = args.min_message_id, args.max_message_id
    emit(
        "PLAN",
        execute=args.execute,
        chat_ids=chats,
        min_id=lower,
        max_id=upper,
        candidate_ids=len(chats) * (upper - lower + 1),
        cancel_pending_broadcasts=args.cancel_pending_broadcasts,
    )
    if not args.execute:
        emit("DRY_RUN", note="Telegram-запросов нет; для удаления добавьте --execute")
        return 0

    async def run() -> int:
        request = HTTPXRequest(
            connect_timeout=30,
            read_timeout=args.read_timeout,
            write_timeout=30,
            pool_timeout=30,
        )
        async with Bot(token=token, request=request) as bot:
            if args.cancel_pending_broadcasts:
                emit("CANCELLED_BROADCASTS", count=await cancel_pending_broadcasts())
            results = await RangeDeleter(
                bot,
                emit,
                attempts=args.attempts,
                batch_size=args.batch_size,
            ).run(chats, lower, upper)
        incomplete = [result.chat_id for result in results if not result.complete]
        emit(
            "SUMMARY",
            chats=len(results),
            incomplete_chat_ids=incomplete,
            accepted_ids=sum(result.accepted_ids for result in results),
            rejected_ids=sum(result.rejected_ids for result in results),
            unresolved_ids=sum(result.unresolved_ids for result in results),
            deleted_count=None,
            note="accepted_ids не является количеством удалённых сообщений",
        )
        return 1 if incomplete else 0

    return asyncio.run(run())


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.min_message_id > args.max_message_id or args.max_message_id > 2**31 - 1:
        parser.error("диапазон должен удовлетворять 1 <= min <= max <= 2147483647")
    if args.batch_size > 100:
        parser.error("--batch-size должен быть не больше 100")
    # Import runtime configuration only after --help and argument validation.
    emit = Reporter(None, ())
    try:
        from app import config

        secrets = (config.BOT_TOKEN, config.ADMIN_PASSWORD, config.OWNER_PASSWORD, config.ZABBIX_API_TOKEN)
        emit = Reporter(None, secrets)
        configure_logging(level=config.LOG_LEVEL, use_json=config.LOG_JSON, force=True, secrets=secrets)
        with SingleInstanceLock(config.INSTANCE_LOCK_PATH), ExitStack() as stack:
            stream = stack.enter_context(_private_report(args.report)) if args.report else None
            emit = Reporter(stream, secrets)
            try:
                return _run_locked(args, config.BOT_TOKEN, config.DATA_DIR, emit)
            except KeyboardInterrupt:
                emit("INTERRUPTED", note="Текущий запрос мог выполниться; повторите тот же диапазон")
                return 130
            except Exception as exc:
                emit("ERROR", reason=str(exc), error_type=type(exc).__name__)
                return 1
    except InstanceAlreadyRunning:
        emit("ERROR", reason="Остановите maintbot.service: process lock занят")
        return ALREADY_RUNNING_EXIT_CODE
    except PermissionError:
        emit("ERROR", reason="Нет доступа к process lock или отчёту; проверьте владельца каталогов (см. README)")
        return 1
    except Exception as exc:
        # Configuration errors may precede discovery of the secrets to redact.
        print(f"Не удалось открыть конфигурацию, lock или отчёт: {type(exc).__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
