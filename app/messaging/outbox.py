"""Durable Telegram outbox delivery."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

from telegram.error import BadRequest, Forbidden, NetworkError, RetryAfter, TimedOut

from ..config import logger
from ..storage import get_outbox_event, get_user_meta_copy, mutate_outbox_event, outbox_snapshot
from .outbox_redrive import redrive_outbox_dead_letters as _redrive_outbox_dead_letters
from .outbox_state import (
    ACTIVE_RECIPIENT_STATUSES,
    DEAD_LETTER_STATUS,
    delivery_coordinates,
    parse_time,
    recipient_mutation,
    should_dead_letter,
)
from .payloads import deliver_payload
from .payloads import document_text_payload as document_text_payload
from .payloads import message_payload as message_payload
from .review_delivery import complete_review_registration
from .telegram_rate import extend_flood_gate, flood_wait_remaining, retry_after_seconds

_PROCESS_LOCK = asyncio.Lock()
MAX_DELIVERIES_PER_RUN = 100
MAX_RUN_SECONDS = 10.0


def _retry_after_seconds(exc: RetryAfter) -> float:
    return retry_after_seconds(exc, minimum=1.0)


async def _finalize_if_done(source: str, event_id: str, event: dict[str, Any] | None) -> bool:
    if not isinstance(event, dict):
        return False
    recipients = event.get("recipients")
    if not isinstance(recipients, dict) or not recipients:
        return False
    statuses = [state.get("status") for state in recipients.values() if isinstance(state, dict)]
    if len(statuses) != len(recipients) or any(
        status in ACTIVE_RECIPIENT_STATUSES or status == DEAD_LETTER_STATUS for status in statuses
    ):
        return False
    await mutate_outbox_event(source, event_id, lambda _: None)
    return True


async def process_outbox(bot) -> int:
    if _PROCESS_LOCK.locked():
        return 0
    processed = 0
    async with _PROCESS_LOCK:
        deadline = asyncio.get_running_loop().time() + MAX_RUN_SECONDS
        for source, event in outbox_snapshot():
            if processed >= MAX_DELIVERIES_PER_RUN:
                break
            event_id = str(event.get("id") or "")
            payload = event.get("payload")
            recipients = event.get("recipients")
            if not event_id or not isinstance(payload, dict) or not isinstance(recipients, dict):
                continue
            # Pre-upgrade queues may contain notifications from retired probes.
            kind = str(event.get("kind", ""))
            if source == "important" and (kind == "fail2ban_daily_digest" or kind.startswith("tls_certificate_")):
                await mutate_outbox_event(source, event_id, lambda _: None)
                continue
            if await _finalize_if_done(source, event_id, event):
                continue
            for uid_text in list(recipients):
                if flood_wait_remaining() or asyncio.get_running_loop().time() >= deadline:
                    return processed
                if processed >= MAX_DELIVERIES_PER_RUN:
                    break
                # Delivery and disk commits yield; authorization/cancellation may
                # have changed since the batch snapshot was taken.
                current = get_outbox_event(source, event_id)
                if current is None:
                    break
                event, payload = current, current["payload"]
                raw_state = current["recipients"].get(uid_text)
                if not isinstance(raw_state, dict) or raw_state.get("status") not in ACTIVE_RECIPIENT_STATUSES:
                    continue
                now = datetime.now(timezone.utc)
                if parse_time(raw_state.get("next_attempt_at")) > now:
                    continue
                try:
                    uid = int(uid_text)
                except (TypeError, ValueError):
                    continue
                try:
                    attempts = max(0, int(raw_state.get("attempts", 0) or 0)) + 1
                except (TypeError, ValueError):
                    attempts = 1
                if raw_state.get("status") == "delivered_pending_registration":
                    updated_event = await complete_review_registration(
                        bot,
                        source=source,
                        event_id=event_id,
                        event=event,
                        uid=uid,
                        state=raw_state,
                        attempts=attempts,
                    )
                    await _finalize_if_done(source, event_id, updated_event)
                    processed += 1
                    continue
                meta = get_user_meta_copy(uid)
                if (
                    isinstance(meta, dict)
                    and meta.get("access_state") == "blocked"
                    and not bool(event.get("allow_blocked_delivery", False))
                ):
                    updated_event = await mutate_outbox_event(
                        source,
                        event_id,
                        recipient_mutation(
                            uid,
                            status="terminal",
                            attempts=max(0, int(raw_state.get("attempts", 0) or 0)),
                            error="delivery suppressed: recipient is blocked",
                        ),
                    )
                    await _finalize_if_done(source, event_id, updated_event)
                    processed += 1
                    continue
                try:
                    delivered_message = await deliver_payload(bot, uid, payload)
                except RetryAfter as exc:
                    delay = _retry_after_seconds(exc) + 0.5
                    await extend_flood_gate(delay)
                    dead_letter = should_dead_letter(event, attempts=attempts, now=now, state=raw_state)
                    updated_event = await mutate_outbox_event(
                        source,
                        event_id,
                        recipient_mutation(
                            uid,
                            status=DEAD_LETTER_STATUS if dead_letter else "pending",
                            attempts=attempts,
                            error="RetryAfter",
                            retry_after=delay,
                        ),
                    )
                except (Forbidden, BadRequest) as exc:
                    status = "terminal" if isinstance(exc, Forbidden) else DEAD_LETTER_STATUS
                    logger.warning(
                        "Outbox permanent delivery error event=%s recipient=%s type=%s status=%s",
                        event_id,
                        uid,
                        exc.__class__.__name__,
                        status,
                        extra={"user_id": uid, "action": f"outbox_{status}"},
                    )
                    updated_event = await mutate_outbox_event(
                        source,
                        event_id,
                        recipient_mutation(uid, status=status, attempts=attempts, error=exc.__class__.__name__),
                    )
                except (TimedOut, NetworkError, OSError) as exc:
                    dead_letter = should_dead_letter(event, attempts=attempts, now=now, state=raw_state)
                    delay = min(3600.0, 2.0 ** min(attempts, 10))
                    if dead_letter:
                        logger.warning(
                            "Outbox moved to dead letter event=%s recipient=%s type=%s attempts=%s",
                            event_id,
                            uid,
                            exc.__class__.__name__,
                            attempts,
                            extra={"user_id": uid, "action": "outbox_dead_letter"},
                        )
                    updated_event = await mutate_outbox_event(
                        source,
                        event_id,
                        recipient_mutation(
                            uid,
                            status=DEAD_LETTER_STATUS if dead_letter else "pending",
                            attempts=attempts,
                            error=exc.__class__.__name__,
                            retry_after=delay,
                        ),
                    )
                except Exception as exc:
                    logger.exception("Unexpected outbox delivery error event=%s recipient=%s", event_id, uid)
                    updated_event = await mutate_outbox_event(
                        source,
                        event_id,
                        recipient_mutation(
                            uid,
                            status=DEAD_LETTER_STATUS,
                            attempts=attempts,
                            error=exc.__class__.__name__,
                        ),
                    )
                else:
                    completion = event.get("completion")
                    if isinstance(completion, dict) and completion.get("type") == "review_card":
                        coordinates = delivery_coordinates(delivered_message, uid)
                        if coordinates is None:
                            updated_event = await mutate_outbox_event(
                                source,
                                event_id,
                                recipient_mutation(
                                    uid,
                                    status=DEAD_LETTER_STATUS,
                                    attempts=attempts,
                                    error="Telegram response has no message coordinates",
                                ),
                            )
                        else:
                            chat_id, message_id = coordinates
                            pending_registration = await mutate_outbox_event(
                                source,
                                event_id,
                                recipient_mutation(
                                    uid,
                                    status="delivered_pending_registration",
                                    attempts=attempts,
                                    chat_id=chat_id,
                                    message_id=message_id,
                                ),
                            )
                            if isinstance(pending_registration, dict):
                                pending_state = (pending_registration.get("recipients") or {}).get(str(uid), {})
                                updated_event = await complete_review_registration(
                                    bot,
                                    source=source,
                                    event_id=event_id,
                                    event=pending_registration,
                                    uid=uid,
                                    state=pending_state,
                                    attempts=attempts,
                                )
                            else:
                                updated_event = pending_registration
                    else:
                        updated_event = await mutate_outbox_event(
                            source,
                            event_id,
                            recipient_mutation(uid, status="delivered", attempts=attempts),
                        )
                await _finalize_if_done(source, event_id, updated_event)
                processed += 1
    return processed


async def redrive_outbox_dead_letters(source: str, event_id: str) -> bool:
    """Compatibility facade for the explicit dead-letter redrive service."""

    return await _redrive_outbox_dead_letters(source, event_id)


async def process_outbox_job(context) -> None:
    count = await process_outbox(context.bot)
    if count:
        logger.info("Outbox deliveries processed: %s", count, extra={"action": "outbox"})
