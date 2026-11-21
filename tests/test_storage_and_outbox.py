from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app import storage
from app.messaging import outbox
from app.messaging.review_sync import review_completion, sync_access_review_messages
from app.persistence.errors import SchemaError
from app.persistence.normalization import normalize_outbox
from app.persistence.sqlite_backend import SQLiteBackend


@pytest.mark.asyncio
async def test_split_runtime_rejects_multiple_service_owners(isolated_storage: None) -> None:
    def seed(cfg: storage.UserData) -> None:
        cfg.authorized_users.update(
            {
                "1": storage.UserData._normalize_user(
                    {
                        "user_id": 1,
                        "role": "admin",
                        "access_state": "approved",
                        "admin_level": "owner",
                    }
                ),
                "2": storage.UserData._normalize_user(
                    {
                        "user_id": 2,
                        "role": "admin",
                        "access_state": "approved",
                        "admin_level": "owner",
                    }
                ),
            }
        )

    with pytest.raises(SchemaError, match="multiple service owners"):
        await storage.update_user_data(seed)


@pytest.mark.asyncio
async def test_user_update_is_persisted_in_separate_domain_stores(isolated_storage: None) -> None:
    user = storage.UserData._normalize_user(
        {
            "user_id": 42,
            "role": "user",
            "access_state": "approved",
            "nickname": "Tester",
            "service_tier": "subscriber",
            "is_paid": True,
        }
    )
    await storage.update_user_data(lambda cfg: cfg.authorized_users.update({"42": user}))

    snapshot = SQLiteBackend(storage.storage_data_dir()).inspect()

    assert snapshot.data("users.profiles")["42"]["nickname"] == "Tester"
    assert snapshot.data("access.grants")["42"]["access_state"] == "approved"
    assert snapshot.data("subscriptions.accounts")["42"]["service_tier"] == "subscriber"


def test_malformed_outbox_state_is_normalized_without_crash() -> None:
    normalized = normalize_outbox(
        {
            "event": {
                "id": "event",
                "payload": {"method": "send_message", "text": "hello"},
                "recipients": {"42": {"attempts": "bad", "part_index": "bad"}, "-1": {}},
            }
        }
    )

    assert normalized["event"]["recipients"] == {
        "42": {
            "status": "pending",
            "attempts": 0,
            "part_index": 0,
            "next_attempt_at": "",
            "last_error": "",
            "delivered_at": "",
            "delivered_chat_id": None,
            "delivered_message_id": None,
            "dead_lettered_at": "",
        }
    }


@pytest.mark.asyncio
async def test_concurrent_user_updates_are_not_lost(isolated_storage: None) -> None:
    def increment(cfg: storage.UserData) -> None:
        current = dict(cfg.authorized_users.get("42") or {"user_id": 42, "access_state": "approved"})
        current["nickname"] = str(int(current.get("nickname") or 0) + 1)
        cfg.authorized_users["42"] = storage.UserData._normalize_user(current)

    await asyncio.gather(*(storage.update_user_data(increment) for _ in range(25)))

    assert storage.get_user_meta_copy(42)["nickname"] == "25"
    on_disk = SQLiteBackend(storage.storage_data_dir()).inspect()
    assert on_disk.data("users.profiles")["42"]["nickname"] == "25"


class _RecordingBot:
    def __init__(self) -> None:
        self.messages: list[dict] = []

    async def send_message(self, **kwargs) -> None:
        self.messages.append(kwargs)


class _ReviewBot:
    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.edited: list[dict] = []

    async def send_message(self, **kwargs):
        self.sent.append(kwargs)
        return SimpleNamespace(chat_id=kwargs["chat_id"], message_id=100 + len(self.sent))

    async def edit_message_text(self, **kwargs):
        self.edited.append(kwargs)
        return True


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["fail2ban_daily_digest", "tls_certificate_critical"])
async def test_old_monitoring_notifications_are_retired_without_delivery(isolated_storage, kind):
    event = storage.make_outbox_event(kind=kind, recipient_ids=[42], payload=outbox.message_payload("expired"))
    await storage.update_important_data(lambda cfg: storage.enqueue_important_outbox(cfg, event))
    bot = _RecordingBot()
    await outbox.process_outbox(bot)
    assert not bot.messages
    assert not storage.outbox_snapshot()


@pytest.mark.asyncio
async def test_blocked_recipient_gets_only_explicit_final_notification(isolated_storage: None) -> None:
    normal = storage.make_outbox_event(
        kind="queued_before_block",
        recipient_ids=[42],
        payload=outbox.message_payload("must not be delivered"),
    )
    final = storage.make_outbox_event(
        kind="access_blocked",
        recipient_ids=[42],
        payload=outbox.message_payload("final block notice"),
        allow_blocked_delivery=True,
    )

    def _seed(cfg: storage.UserData) -> None:
        cfg.authorized_users["42"] = storage.UserData._normalize_user(
            {"user_id": 42, "role": "user", "access_state": "blocked"}
        )
        storage.enqueue_user_outbox(cfg, normal)
        storage.enqueue_user_outbox(cfg, final)

    await storage.update_user_data(_seed)
    bot = _RecordingBot()

    processed = await outbox.process_outbox(bot)

    assert processed == 2
    assert [message["text"] for message in bot.messages] == ["final block notice"]
    assert storage.outbox_snapshot() == []


def test_blocking_suppresses_only_target_recipient_and_preserves_final_event() -> None:
    cfg = storage.UserData()
    shared = storage.make_outbox_event(
        kind="shared",
        recipient_ids=[42, 43],
        payload=outbox.message_payload("shared"),
        event_id="shared",
    )
    final = storage.make_outbox_event(
        kind="access_blocked",
        recipient_ids=[42],
        payload=outbox.message_payload("final"),
        event_id="final",
        allow_blocked_delivery=True,
    )
    storage.enqueue_user_outbox(cfg, shared)
    storage.enqueue_user_outbox(cfg, final)

    removed = storage.suppress_user_outbox_recipient(cfg, 42, keep_event_id="final")

    assert removed == 1
    assert set(cfg.outbox) == {"shared", "final"}
    assert set(cfg.outbox["shared"]["recipients"]) == {"43"}
    assert cfg.outbox["final"]["allow_blocked_delivery"] is True


@pytest.mark.asyncio
async def test_access_review_delivery_refs_are_persisted_and_all_cards_refresh(isolated_storage: None) -> None:
    generation = "2026-08-11T12:00:00+00:00"
    event = storage.make_outbox_event(
        kind="access_request",
        recipient_ids=[1, 2],
        payload=outbox.message_payload("stale pending card"),
        completion=review_completion(scope="access", target_id=42, generation=generation),
    )

    def seed(cfg: storage.UserData) -> None:
        for admin_id in (1, 2):
            cfg.authorized_users[str(admin_id)] = storage.UserData._normalize_user(
                {
                    "user_id": admin_id,
                    "role": "admin",
                    "access_state": "approved",
                    "admin_level": "admin",
                }
            )
        cfg.authorized_users["42"] = storage.UserData._normalize_user(
            {
                "user_id": 42,
                "role": "user",
                "access_state": "pending",
                "access_requested_at": generation,
            }
        )
        storage.enqueue_user_outbox(cfg, event)

    await storage.update_user_data(seed)
    bot = _ReviewBot()

    await outbox.process_outbox(bot)

    target = storage.get_user_meta_copy(42)
    assert target is not None
    assert set(target["review_messages"]) == {"1", "2"}
    assert storage.outbox_snapshot() == []

    await storage.mutate_user_meta(
        42,
        lambda meta: {**meta, "access_state": "approved", "access_reviewed_by_name": "Администратор"},
    )
    await sync_access_review_messages(bot, 42)

    refreshed = bot.edited[-2:]
    assert {item["chat_id"] for item in refreshed} == {1, 2}
    assert all("одобрена" in item["text"] for item in refreshed)
    assert all(item["reply_markup"] is None for item in refreshed)


@pytest.mark.asyncio
async def test_late_service_review_delivery_is_immediately_rendered_from_current_state(
    isolated_storage: None,
) -> None:
    generation = "2026-08-11T12:00:00+00:00"
    event = storage.make_outbox_event(
        kind="trial_request",
        recipient_ids=[1],
        payload=outbox.message_payload(
            "stale pending card",
            reply_markup=[[{"text": "Approve", "callback_data": "product:req:approve:7"}]],
        ),
        completion=review_completion(scope="service", target_id=7, generation=generation),
    )
    second_event = storage.make_outbox_event(
        kind="trial_request_followup",
        recipient_ids=[1],
        payload=outbox.message_payload("another stale card"),
        completion=review_completion(scope="service", target_id=7, generation=generation),
    )

    def seed(cfg: storage.UserData) -> None:
        cfg.authorized_users["1"] = storage.UserData._normalize_user(
            {
                "user_id": 1,
                "role": "admin",
                "access_state": "approved",
                "admin_level": "admin",
            }
        )
        cfg.authorized_users["42"] = storage.UserData._normalize_user(
            {"user_id": 42, "role": "user", "access_state": "approved"}
        )
        cfg.service_requests["7"] = {
            "id": 7,
            "kind": "trial",
            "status": "approved",
            "user_id": 42,
            "created_at": generation,
            "updated_at": generation,
        }
        storage.enqueue_user_outbox(cfg, event)
        storage.enqueue_user_outbox(cfg, second_event)

    await storage.update_user_data(seed)
    bot = _ReviewBot()

    await outbox.process_outbox(bot)

    request = storage.service_requests_snapshot()["7"]
    assert set(request["review_messages"]) == {"1"}
    assert len(request["review_messages"]["1"]) == 2
    assert "одобрена" in bot.edited[-1]["text"]
    callbacks = {
        button.callback_data
        for row in bot.edited[-1]["reply_markup"].inline_keyboard
        for button in row
        if button.callback_data
    }
    assert "product:req:approve:7" not in callbacks
