"""Exercise the real listener and SQLite claims with an offline Siteverify transport."""

import asyncio
import hashlib
import hmac
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlencode

import httpx
import pytest
import pytest_asyncio

from app import storage
from app.captcha.server import CaptchaServer
from app.captcha.verification import SITEVERIFY

ORIGIN = "https://verify.example.test"
BOT_TOKEN = "test-telegram"


def launch(user_id=1234, age=0):
    fields = {"auth_date": str(int(time.time()) - age), "user": json.dumps({"id": user_id}), "query_id": "test"}
    key = hmac.digest(b"WebAppData", BOT_TOKEN.encode(), "sha256")
    checked = "\n".join(f"{name}={value}" for name, value in sorted(fields.items()))
    fields["hash"] = hmac.digest(key, checked.encode(), "sha256").hex()
    return urlencode(fields)


@pytest_asyncio.fixture
async def service(captcha_storage):
    session = await storage.captcha_session(1234)
    ctx = SimpleNamespace(
        payload={"session": session, "initData": launch(), "token": "synthetic-widget-token"},
        provider_result={
            "success": True,
            "action": "registration",
            "hostname": "verify.example.test",
            "cdata": hashlib.sha256(session.encode()).hexdigest(),
        },
        continued=AsyncMock(),
        calls=[],
    )

    async def provider(request):
        ctx.calls.append(request)
        assert str(request.url) == SITEVERIFY and request.method == "POST"
        assert parse_qs(request.content.decode()) == {"secret": ["synthetic-key"], "response": [ctx.payload["token"]]}
        if isinstance(ctx.provider_result, Exception):
            raise ctx.provider_result
        return httpx.Response(200, json=ctx.provider_result)

    server = CaptchaServer(
        url=ORIGIN,
        site_key="public-key",
        secret_key="synthetic-key",
        bot_token=BOT_TOKEN,
        on_verified=ctx.continued,
        transport=httpx.MockTransport(provider),
    )
    await server.start("127.0.0.1", 0)
    port = server._listener.sockets[0].getsockname()[1]
    try:
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}", headers={"Origin": ORIGIN}, trust_env=False
        ) as ctx.client:
            yield ctx
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_widget_page_and_verified_continuation_once(service):
    ctx = service
    page = await ctx.client.get("/", params={"session": ctx.payload["session"]})
    assert page.status_code == 200 and "public-key" in page.text and "synthetic-key" not in page.text
    assert page.headers["referrer-policy"] == "strict-origin"
    for path in ("/app.js", "/app.css"):
        assert (await ctx.client.get(path)).is_success
    responses = await asyncio.gather(*(ctx.client.post("/verify", json=ctx.payload) for _ in range(2)))
    assert sorted(r.status_code for r in responses) == [200, 409]
    assert next(r.json() for r in responses if r.is_success) == {"continued": True}
    assert len(ctx.calls) == 1 and await storage.captcha_allowed(1234)
    ctx.continued.assert_awaited_once_with(1234)
    assert (await ctx.client.post("/verify", json=ctx.payload)).status_code == 409


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("changes", "status"),
    [
        ({"initData": lambda: launch().replace("1234", "9876")}, 403),
        ({"initData": lambda: launch(user_id=9876)}, 409),
        ({"initData": lambda: launch(age=601)}, 403),
        ({"initData": lambda: launch(age=-300)}, 403),
        ({"initData": lambda: launch() + "&user=%7B%7D"}, 403),
        ({"user_id": lambda: 9876}, 400),
        ({"token": lambda: ""}, 400),
        ({"token": lambda: "a" * 2049}, 400),
    ],
)
async def test_invalid_launch_or_token_never_reaches_provider(service, changes, status):
    ctx = service
    payload = {**ctx.payload, **{key: factory() for key, factory in changes.items()}}
    assert (await ctx.client.post("/verify", json=payload)).status_code == status
    assert not ctx.calls and not await storage.captcha_allowed(1234)
    ctx.continued.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "override",
    [
        {"hostname": "other.example.test"},
        {"action": "another_action"},
        {"cdata": "another_session"},
        {"success": False, "error-codes": ["timeout-or-duplicate"]},
        {"success": False, "error-codes": ["invalid-input-response"]},
    ],
)
async def test_provider_rejection_burns_token_and_permits_fresh_challenge(service, override):
    ctx = service
    good = ctx.provider_result.copy()
    ctx.provider_result.update(override)
    assert (await ctx.client.post("/verify", json=ctx.payload)).status_code == 422
    assert not await storage.captcha_allowed(1234)
    assert (await ctx.client.post("/verify", json=ctx.payload)).status_code == 409
    assert len(ctx.calls) == 1
    ctx.provider_result = good
    ctx.payload["token"] = "fresh-token"
    assert (await ctx.client.post("/verify", json=ctx.payload)).is_success


@pytest.mark.asyncio
async def test_provider_unreachable_and_registration_delivery_failure(service):
    ctx = service
    good = ctx.provider_result
    ctx.provider_result = httpx.ReadTimeout("offline")
    assert (await ctx.client.post("/verify", json=ctx.payload)).status_code == 503
    assert not await storage.captcha_allowed(1234)
    ctx.continued.assert_not_awaited()

    ctx.provider_result = good
    ctx.payload["token"] = "fresh-token"
    ctx.continued.side_effect = TimeoutError
    response = await ctx.client.post("/verify", json=ctx.payload)
    assert response.status_code == 200 and response.json() == {"continued": False}
    assert await storage.captcha_allowed(1234)


@pytest.mark.asyncio
async def test_rejects_foreign_origin_and_oversized_body(service):
    ctx = service
    assert (
        await ctx.client.post("/verify", json=ctx.payload, headers={"Origin": "https://other.test"})
    ).status_code == 403
    assert (await ctx.client.post("/verify", content=b"x" * 25000)).status_code == 413
    assert not ctx.calls and not await storage.captcha_allowed(1234)


@pytest.mark.asyncio
async def test_rate_limit_resets_and_failed_requests_do_not_consume_session(service, monkeypatch):
    from app.captcha import server

    ctx = service
    now = 100.0
    monkeypatch.setattr(server, "time", SimpleNamespace(monotonic=lambda: now))
    for _ in range(32):
        assert (await ctx.client.post("/verify", json={})).status_code == 400
    assert (await ctx.client.post("/verify", json=ctx.payload)).status_code == 429
    assert not ctx.calls and not await storage.captcha_allowed(1234)
    now += 1
    assert (await ctx.client.post("/verify", json=ctx.payload)).status_code == 200
