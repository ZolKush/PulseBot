from __future__ import annotations

import asyncio
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from app import storage
from app.persistence import captcha
from app.persistence.sqlite_backend import SQLiteBackend


@pytest.mark.asyncio
async def test_activation_grandfathers_registered_accounts_once(tmp_path: Path) -> None:
    backend = SQLiteBackend(tmp_path)
    backend.bootstrap()
    backend.commit(
        base_revision=1,
        changes={
            "users.profiles": {"101": {"user_id": 101}},
            "access.grants": {"101": {"access_state": "approved"}},
            "subscriptions.accounts": {"101": {}},
        },
    )
    storage.initialize_storage(tmp_path)
    with closing(backend.connect()) as db:
        assert db.execute("SELECT 1 FROM sqlite_master WHERE name='captcha_passes'").fetchone() is None
    storage.initialize_storage(tmp_path, captcha_enabled=True)
    assert await storage.captcha_allowed(101)
    assert not await storage.captcha_allowed(102)
    with closing(backend.connect()) as db:
        assert db.execute("SELECT verified_at FROM captcha_passes WHERE user_id=101").fetchone() == (None,)

    await storage.upsert_user_meta(102, {"user_id": 102})
    # Opening the DB in an offline recovery tool must preserve the activation marker.
    storage.initialize_storage(tmp_path)
    storage.initialize_storage(tmp_path, captcha_enabled=True)
    assert not await storage.captcha_allowed(102)

    nonce = await storage.captcha_session(104)
    assert await storage.captcha_claim(104, nonce, "pending")
    storage.initialize_storage(tmp_path, captcha_enabled=False)
    with closing(backend.connect()) as db:
        assert db.execute("SELECT 1 FROM captcha_sessions").fetchone() is None
        assert db.execute("SELECT 1 FROM captcha_tokens").fetchone() is None
    await storage.upsert_user_meta(103, {"user_id": 103})
    storage.initialize_storage(tmp_path, captcha_enabled=True)
    assert await storage.captcha_allowed(103)
    assert not await storage.captcha_allowed(104)


@pytest.mark.asyncio
async def test_session_single_winner_and_durable_success(captcha_storage) -> None:
    # Concurrent Telegram updates return the same nonce and a competing HTTP request cannot claim it.
    first, second = await asyncio.gather(storage.captcha_session(101), storage.captcha_session(101))
    assert first == second
    assert not await storage.captcha_claim(102, first, "token")
    results = await asyncio.gather(
        storage.captcha_claim(101, first, "winner"), storage.captcha_claim(101, second, "contender")
    )
    assert sum(results) == 1
    winner, loser = ("winner", "contender") if results[0] else ("contender", "winner")
    assert not await storage.captcha_finish(101, first, loser, success=True)
    assert not await storage.captcha_finish(102, first, winner, success=True)
    assert await storage.captcha_finish(101, first, winner, success=True)
    assert not await storage.captcha_finish(101, first, winner, success=True)
    assert not await storage.captcha_claim(101, first, "new")

    storage.initialize_storage(storage.storage_data_dir(), captcha_enabled=True)
    assert await storage.captcha_allowed(101)
    assert storage.get_user_meta_copy(101) is None
    with closing(SQLiteBackend(storage.storage_data_dir()).connect()) as db:
        assert isinstance(db.execute("SELECT verified_at FROM captcha_passes WHERE user_id=101").fetchone()[0], int)


@pytest.mark.asyncio
async def test_failed_tokens_cannot_replay_across_sessions_or_accounts(captcha_storage) -> None:
    one = await storage.captcha_session(101)
    two = await storage.captcha_session(102)
    assert await storage.captcha_claim(101, one, "spent")
    assert not await storage.captcha_finish(101, one, "spent", success=False)
    assert not await storage.captcha_allowed(101)
    assert not await storage.captcha_claim(102, two, "spent")
    assert not await storage.captcha_claim(101, one, "spent")
    assert not await storage.captcha_claim(101, one, "x" * 2049)
    for attempt in range(1, captcha.MAX_ATTEMPTS):
        token = f"fresh-{attempt}"
        assert await storage.captcha_claim(101, one, token)
        await storage.captcha_finish(101, one, token, success=False)
    assert not await storage.captcha_claim(101, one, "over-limit")
    assert await storage.captcha_session(101) == one


@pytest.mark.asyncio
async def test_expired_claims_and_sessions_fail_closed_and_are_pruned(captcha_storage, monkeypatch) -> None:
    clock = 1_700_000_000
    monkeypatch.setattr(captcha, "time", type("Clock", (), {"time": lambda: clock}))
    nonce = await storage.captcha_session(101)
    assert await storage.captcha_claim(101, nonce, "first")
    clock += captcha.CLAIM_SECONDS
    assert not await storage.captcha_finish(101, nonce, "first", success=True)
    assert await storage.captcha_claim(101, nonce, "retry")
    assert not await storage.captcha_finish(101, nonce, "first", success=True)
    clock += captcha.SESSION_SECONDS
    assert not await storage.captcha_finish(101, nonce, "retry", success=True)
    assert not await storage.captcha_claim(101, nonce, "late")
    await storage.captcha_cleanup()
    backend = SQLiteBackend(storage.storage_data_dir())
    with closing(sqlite3.connect(backend.database_path)) as db:
        assert db.execute("SELECT COUNT(*) FROM captcha_sessions").fetchone() == (0,)
        assert db.execute("SELECT COUNT(*) FROM captcha_tokens").fetchone() == (0,)
    assert await storage.captcha_session(101) != nonce
    assert not await storage.captcha_allowed(101)
