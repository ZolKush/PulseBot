from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from app import storage
from app.access import commands as access_commands
from app.access import request_handlers as access_requests
from app.config import TZ
from app.users.admin import detail_handlers as users
from app.users.staff import (
    STAFF_DISPLAY_TITLE_ALIAS,
    STAFF_TITLE_LEAD,
    STAFF_TITLE_OWNER,
    STAFF_TITLE_SUPPORT,
    is_lead_or_owner_meta,
    staff_internal_identity,
    staff_public_signature,
)
from app.users.views import user_card_kb
from tests.product_support import _admin, _callback_names, _callback_update, _user


@pytest.mark.parametrize("state", [None, "pending", "rejected", "logged_out", "blocked"])
@pytest.mark.asyncio
async def test_help_requires_approved_access(state: str | None, isolated_storage: None) -> None:
    await storage.update_user_data(lambda data: data.product_settings.update(support_email="team@example.org"))
    if state is not None:
        await storage.upsert_user_meta(42, _user(42, access_state=state, enabled=False))
    update, context = _callback_update(42, "menu:help")
    await access_commands.cmd_help(update, context)
    reply = update.effective_message.reply_text.await_args.args[0]
    assert "team@example.org" not in reply
    update.callback_query.edit_message_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_help_remains_available_after_approval(isolated_storage: None) -> None:
    await storage.upsert_user_meta(42, _user(42))
    await storage.update_user_data(lambda data: data.product_settings.update(support_email="team@example.org"))
    update, context = _callback_update(42, "menu:help")
    await access_commands.cmd_help(update, context)
    assert "team@example.org" in update.callback_query.edit_message_text.await_args.args[0]


def test_staff_signature_is_title_or_title_plus_alias() -> None:
    support = _admin(
        1,
        staff_alias="Север",
        staff_display_mode=STAFF_DISPLAY_TITLE_ALIAS,
    )
    owner = _admin(
        2,
        admin_level="owner",
        staff_title=STAFF_TITLE_SUPPORT,
        staff_alias="Кирилл",
        staff_display_mode=STAFF_DISPLAY_TITLE_ALIAS,
        username="owner",
    )

    assert staff_public_signature(support) == "Специалист поддержки «Север»"
    assert staff_public_signature(support, allow_alias=False) == "Специалист поддержки"
    assert staff_public_signature(owner).startswith("Руководитель сервиса")
    assert owner["staff_title"] == STAFF_TITLE_OWNER
    assert "Admin 2" in staff_internal_identity(owner)
    assert "ID 2" in staff_internal_identity(owner)


def test_lead_and_owner_permissions_are_separate_from_regular_admin() -> None:
    assert not is_lead_or_owner_meta(_admin(1))
    assert is_lead_or_owner_meta(_admin(2, staff_title=STAFF_TITLE_LEAD))
    assert is_lead_or_owner_meta(_admin(3, admin_level="owner"))


def test_tier_payment_invariants_and_owner_perpetual_access() -> None:
    assert _user(1, service_tier="basic", is_paid=True)["is_paid"] is False
    unlimited = _user(
        2,
        service_tier="unlimited_trial",
        is_paid=True,
        subscription_end_at=datetime.now(TZ).isoformat(),
    )
    assert unlimited["is_paid"] is False
    assert unlimited["subscription_end_at"] is None
    staff = _admin(3, service_tier="basic", is_paid=True)
    assert staff["role"] == "admin"
    assert staff["service_tier"] == "basic"
    assert staff["is_paid"] is False
    owner = _admin(
        4,
        admin_level="owner",
        service_tier="basic",
        is_paid=False,
        subscription_end_at=datetime.now(TZ).isoformat(),
    )
    assert owner["service_tier"] == "subscriber"
    assert owner["is_paid"] is True
    assert owner["subscription_end_at"] is None


@pytest.mark.asyncio
async def test_reauthorization_preserves_existing_service_tier_and_connection(isolated_storage: None) -> None:
    await storage.update_user_data(
        lambda cfg: cfg.authorized_users.update(
            {
                "42": _user(
                    42,
                    access_state="logged_out",
                    enabled=False,
                    service_tier="subscriber",
                    is_paid=True,
                    subscription_end_at=(datetime.now(TZ) + timedelta(days=5)).isoformat(),
                    connection_url="https://connect.test/42",
                )
            }
        )
    )
    update, context = _callback_update(42)

    await access_requests.access_request_cb(update, context)

    current = storage.get_user_meta_copy(42)
    assert current is not None
    assert current["access_state"] == "approved"
    assert current["enabled"] is True
    assert current["service_tier"] == "subscriber"
    assert current["is_paid"] is True
    assert current["connection_url"] == "https://connect.test/42"
    assert storage.outbox_snapshot() == []


@pytest.mark.asyncio
async def test_logged_out_paid_flag_without_future_end_still_requires_review(isolated_storage: None) -> None:
    await storage.update_user_data(
        lambda cfg: cfg.authorized_users.update(
            {
                "42": _user(
                    42,
                    access_state="logged_out",
                    enabled=False,
                    service_tier="subscriber",
                    is_paid=True,
                    subscription_end_at=None,
                )
            }
        )
    )
    update, context = _callback_update(42)

    await access_requests.access_request_cb(update, context)

    current = storage.get_user_meta_copy(42)
    assert current is not None
    assert current["access_state"] == "pending"


@pytest.mark.asyncio
async def test_paid_logout_explains_automatic_return_without_review(isolated_storage: None) -> None:
    await storage.update_user_data(
        lambda cfg: cfg.authorized_users.update(
            {
                "42": _user(
                    42,
                    service_tier="subscriber",
                    is_paid=True,
                    subscription_end_at=(datetime.now(TZ) + timedelta(days=5)).isoformat(),
                )
            }
        )
    )
    update, context = _callback_update(42)
    update.callback_query = None

    await access_commands.cmd_logout(update, context)

    reply = update.effective_message.reply_text.await_args.args[0]
    assert "/start" in reply
    assert "без повторного одобрения" in reply


@pytest.mark.asyncio
async def test_rejected_access_request_can_still_be_blocked(isolated_storage: None) -> None:
    await storage.update_user_data(
        lambda cfg: cfg.authorized_users.update(
            {
                "1": _admin(1),
                "42": _user(42, access_state="pending", enabled=False),
            }
        )
    )
    reject_update, context = _callback_update(1, "access:reject:42")
    reject_update.callback_query.message.text_html = "Заявка пользователя"

    await access_requests.access_review_cb(reject_update, context)

    rejected = storage.get_user_meta_copy(42)
    assert rejected is not None
    assert rejected["access_state"] == "rejected"
    post_reject_markup = reject_update.callback_query.edit_message_text.await_args.kwargs["reply_markup"]
    assert _callback_names(post_reject_markup) == {"access:block:42"}
    assert {
        "users:access:approve:42",
        "users:access:block:42",
    }.issubset(_callback_names(user_card_kb(42)))

    block_update, context = _callback_update(1, "access:block:42")
    block_update.callback_query.message.text_html = "Отклонённая заявка пользователя"
    await access_requests.access_review_cb(block_update, context)

    blocked = storage.get_user_meta_copy(42)
    assert blocked is not None
    assert blocked["access_state"] == "blocked"
    assert blocked["enabled"] is False


@pytest.mark.asyncio
async def test_rejected_user_card_block_action_is_explicit(isolated_storage: None) -> None:
    await storage.update_user_data(
        lambda cfg: cfg.authorized_users.update(
            {
                "1": _admin(1),
                "42": _user(42, access_state="rejected", enabled=False),
            }
        )
    )
    update, context = _callback_update(1, "users:accessapply:block:42")

    await users.users_user_menu(update, context)

    blocked = storage.get_user_meta_copy(42)
    assert blocked is not None
    assert blocked["access_state"] == "blocked"
    assert blocked["blocked_by_id"] == 1


@pytest.mark.asyncio
async def test_owner_claim_is_atomic_and_second_claim_is_noop(
    isolated_storage: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    def seed(cfg: storage.UserData) -> None:
        cfg.authorized_users.update({"1": _admin(1), "2": _admin(2)})
        cfg.request_seq = 1
        cfg.service_requests["1"] = {
            "id": 1,
            "kind": "renewal",
            "status": "payment_reported",
            "user_id": 1,
        }

    await storage.update_user_data(seed)
    monkeypatch.setattr(access_commands, "OWNER_PASSWORD", "owner-password-for-test")
    show_menu = AsyncMock()
    monkeypatch.setattr(access_commands, "show_main_menu", show_menu)

    first, context = _callback_update(1)
    first.callback_query = None
    first.effective_message.text = "/owner owner-password-for-test"
    await access_commands.cmd_owner(first, context)

    second, context = _callback_update(2)
    second.callback_query = None
    second.effective_message.text = "/owner owner-password-for-test"
    await access_commands.cmd_owner(second, context)

    assert storage.get_user_meta_copy(1)["admin_level"] == "owner"
    assert storage.get_user_meta_copy(2)["admin_level"] == "admin"
    request = storage.service_requests_snapshot()["1"]
    assert request["status"] == "cancelled"
    assert request["decision_reason"] == "service_manager_billing_exempt"
    assert show_menu.await_count == 1
    first.effective_message.delete.assert_awaited_once()
    second.effective_message.delete.assert_awaited_once()
