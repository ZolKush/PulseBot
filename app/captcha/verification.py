"""Authenticate a Mini App launch and redeem its session-bound Turnstile token."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import re
import time
from urllib.parse import parse_qsl

import httpx

from .. import storage

SITEVERIFY = "https://challenges.cloudflare.com/turnstile/v0/siteverify"
ACTION = "registration"
INIT_DATA_TTL = 600


class VerificationError(Exception):
    def __init__(self, status: int):
        self.status = status


def telegram_user_id(init_data: str, bot_token: str) -> int:
    """Return only a signed, fresh identity; reject ambiguous field boundaries."""
    try:
        fields = parse_qsl(init_data, keep_blank_values=True, strict_parsing=True, errors="strict", max_num_fields=32)
        values = dict(fields)
        if len(values) != len(fields) or any(
            not re.fullmatch(r"[A-Za-z0-9_]+", key) or "\n" in value or "\r" in value for key, value in fields
        ):
            raise ValueError
        supplied = values.pop("hash")
        checked = "\n".join(f"{key}={values[key]}" for key in sorted(values))
        key = hmac.digest(b"WebAppData", bot_token.encode(), "sha256")
        expected = hmac.new(key, checked.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, supplied):
            raise ValueError
        age = int(time.time()) - int(values["auth_date"])
        if not -30 <= age < INIT_DATA_TTL:
            raise ValueError
        user_id = json.loads(values["user"])["id"]
        if type(user_id) is not int or not 0 < user_id < 2**52:
            raise ValueError
        return user_id
    except (ValueError, TypeError, KeyError, RecursionError) as exc:
        raise VerificationError(403) from exc


async def _siteverify(client: httpx.AsyncClient, secret: str, hostname: str, session: str, token: str) -> None:
    try:
        response = await asyncio.wait_for(client.post(SITEVERIFY, data={"secret": secret, "response": token}), 8)
        response.raise_for_status()
        result = response.json()
    except (httpx.HTTPError, asyncio.TimeoutError, ValueError) as exc:
        raise VerificationError(503) from exc
    if not isinstance(result, dict):
        raise VerificationError(503)
    errors = result.get("error-codes")
    if isinstance(errors, list) and any(
        code in ("internal-error", "missing-input-secret", "invalid-input-secret") for code in errors
    ):
        raise VerificationError(503)
    if not (
        result.get("success") is True
        and result.get("hostname") == hostname
        and result.get("action") == ACTION
        and result.get("cdata") == hashlib.sha256(session.encode("ascii")).hexdigest()
    ):
        raise VerificationError(422)


async def verify(body: bytes, *, bot_token: str, secret: str, hostname: str, client: httpx.AsyncClient) -> int:
    """Consume a claim exactly once, then hand its verified identity to the bot."""
    try:
        payload = json.loads(body)
        if not isinstance(payload, dict) or payload.keys() != {"session", "initData", "token"}:
            raise ValueError
        session, init_data, token = payload["session"], payload["initData"], payload["token"]
        if not isinstance(session, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,128}", session):
            raise ValueError
        if not isinstance(init_data, str) or not 0 < len(init_data) <= 16384:
            raise ValueError
        if not isinstance(token, str) or not 0 < len(token) <= 2048 or not token.isascii() or not token.isprintable():
            raise ValueError
    except (ValueError, TypeError, RecursionError) as exc:
        raise VerificationError(400) from exc
    user_id = telegram_user_id(init_data, bot_token)
    if not await storage.captcha_claim(user_id, session, token):
        raise VerificationError(409)
    try:
        await _siteverify(client, secret, hostname, session, token)
    except VerificationError:
        await storage.captcha_finish(user_id, session, token, success=False)
        raise
    if not await storage.captcha_finish(user_id, session, token, success=True):
        raise VerificationError(409)
    return user_id
