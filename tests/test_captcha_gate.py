from __future__ import annotations

from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlsplit

import pytest
from telegram.error import TimedOut
from telegram.ext import ApplicationHandlerStop, TypeHandler

from app import storage
from app.access import captcha
from app.bot import routes
from app.main import build_app
from tests.product_support import _callback_update, _user


def test_gate_only_registered_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    assert -90 not in build_app().handlers
    monkeypatch.setattr(routes, "CAPTCHA_ENABLED", True)
    application = build_app()
    (handler,) = application.handlers[-90]
    assert isinstance(handler, TypeHandler) and handler.callback is captcha.captcha_guard
    assert sorted(application.handlers) == [-100, -90, -1, 0]


@pytest.mark.asyncio
async def test_start_opens_bound_mini_app_then_resumes_admission(captcha_storage: None) -> None:
    update, context = _callback_update(42)
    update.callback_query = None
    update.effective_message.text = "/start"
    with pytest.raises(ApplicationHandlerStop):
        await captcha.captcha_guard(update, context)
    markup = update.effective_message.reply_text.await_args.kwargs["reply_markup"].inline_keyboard
    button = markup[0][0]
    assert button.text == "Пройти проверку"
    assert len(markup) == 2
    assert markup[1][0].callback_data == "menu:privacy"
    session = parse_qs(urlsplit(button.web_app.url).query)["session"][0]
    assert storage.get_user_meta_copy(42) is None
    assert not await storage.captcha_allowed(42)
    assert await storage.captcha_claim(42, session, "answer")
    assert await storage.captcha_finish(42, session, "answer", success=True)
    bot = AsyncMock()
    await captcha.continue_registration(bot, 42)
    args = bot.send_message.await_args.kwargs
    assert args["chat_id"] == 42
    assert args["reply_markup"].inline_keyboard[0][0].callback_data == "access:request"
    await captcha.captcha_guard(update, context)
    assert update.effective_message.reply_text.await_count == 1


@pytest.mark.parametrize(
    ("command", "callback"),
    [
        ("/menu", None),
        ("/support", None),
        ("/help", None),
        ("/privacy", None),
        ("/auth password", None),
        ("ответ в открытом диалоге", None),
        ("", "access:request"),
        ("", "subscription:trial"),
        ("", "subscription:buyconfirm"),
        ("", "menu:status"),
        ("", "menu:help"),
        ("", "menu:ticket"),
    ],
)
@pytest.mark.asyncio
async def test_other_protected_updates_direct_to_start_without_session(
    command: str, callback: str | None, captcha_storage: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    issue = AsyncMock()
    monkeypatch.setattr(captcha, "captcha_session", issue)
    update, context = _callback_update(42, callback or "")
    if callback is None:
        update.callback_query = None
    update.effective_message.text = command
    with pytest.raises(ApplicationHandlerStop):
        await captcha.captcha_guard(update, context)
    reply = update.effective_message.reply_text.await_args
    assert "/start" in reply.args[0]
    assert "reply_markup" not in reply.kwargs
    issue.assert_not_awaited()
    if command.startswith("/auth"):
        update.effective_message.delete.assert_awaited_once()


@pytest.mark.asyncio
async def test_legacy_account_keeps_access_through_upgrade(isolated_storage: None) -> None:
    await storage.upsert_user_meta(42, _user(42))
    storage.initialize_storage(storage.storage_data_dir(), captcha_enabled=True)
    update, context = _callback_update(42, "subscription:trial")
    await captcha.captcha_guard(update, context)
    update.effective_message.reply_text.assert_not_awaited()
    assert not await storage.captcha_allowed(43)


@pytest.mark.asyncio
async def test_privacy_button_before_verification(captcha_storage: None) -> None:
    update, context = _callback_update(42, "menu:privacy")
    with pytest.raises(ApplicationHandlerStop):
        await captcha.captcha_guard(update, context)
    reply = update.effective_message.reply_text.await_args
    assert "Turnstile" in reply.args[0]
    assert "/support" not in reply.args[0]
    assert reply.kwargs["reply_markup"].inline_keyboard[0][0].url == "https://telegram.org/privacy-tpa"
    assert storage.get_user_meta_copy(42) is None


@pytest.mark.asyncio
async def test_gate_denies_access_on_storage_and_reply_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(captcha, "captcha_allowed", AsyncMock(side_effect=RuntimeError))
    update, context = _callback_update(42)
    update.effective_message.reply_text.side_effect = TimedOut()
    with pytest.raises(ApplicationHandlerStop):
        await captcha.captcha_guard(update, context)
