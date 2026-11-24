"""Access challenges in the existing database, independent of PTB and account state."""

from __future__ import annotations

import asyncio
import hashlib
import secrets
import sqlite3
import time
from collections.abc import Callable, Iterable
from contextlib import closing
from typing import TypeVar

T = TypeVar("T")
SESSION_SECONDS = 600
CLAIM_SECONDS = 45
MAX_ATTEMPTS = 5

_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS captcha_passes (user_id INTEGER PRIMARY KEY, verified_at INTEGER)",
    "CREATE TABLE IF NOT EXISTS captcha_activation (singleton INTEGER PRIMARY KEY CHECK (singleton = 1))",
    "CREATE TABLE IF NOT EXISTS captcha_sessions (user_id INTEGER PRIMARY KEY, nonce TEXT NOT NULL, "
    "expires_at INTEGER NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, claim BLOB, claim_until INTEGER)",
    "CREATE TABLE IF NOT EXISTS captcha_tokens (digest BLOB PRIMARY KEY, expires_at INTEGER NOT NULL)",
    "CREATE INDEX IF NOT EXISTS captcha_sessions_expiry ON captcha_sessions(expires_at)",
    "CREATE INDEX IF NOT EXISTS captcha_tokens_expiry ON captcha_tokens(expires_at)",
)


class CaptchaStore:
    def __init__(self, connect: Callable[[], sqlite3.Connection]) -> None:
        self.connect = connect

    def initialize(self, registered_ids: Iterable[str], *, enabled: bool) -> None:
        # The launcher already owns the instance lock and validated the SQLite
        # aggregates (including users imported from a legacy split JSON layout).
        with closing(self.connect()) as db, db:
            if not enabled:
                # Older/off installations need no tables. Removing the marker
                # ensures registrations made while disabled are included next time.
                existing = db.execute("SELECT 1 FROM sqlite_master WHERE name='captcha_activation' AND type='table'")
                if existing.fetchone():
                    db.execute("BEGIN IMMEDIATE")
                    db.execute("DELETE FROM captcha_activation")
                    db.execute("DELETE FROM captcha_sessions")
                    db.execute("DELETE FROM captcha_tokens")
                return
            db.execute("BEGIN IMMEDIATE")
            for statement in _SCHEMA:
                db.execute(statement)
            activated = db.execute("INSERT OR IGNORE INTO captcha_activation VALUES (1)").rowcount
            if activated:
                db.executemany(
                    "INSERT OR IGNORE INTO captcha_passes(user_id, verified_at) VALUES (?, NULL)",
                    ((user_id,) for user_id in registered_ids),
                )
            self._cleanup(db, int(time.time()))

    async def _run(self, operation: Callable[[sqlite3.Connection, int], T], *, write: bool = True) -> T:
        def run() -> T:
            with closing(self.connect()) as db, db:
                if write:
                    db.execute("BEGIN IMMEDIATE")
                return operation(db, int(time.time()))

        worker = asyncio.create_task(asyncio.to_thread(run))
        try:
            return await asyncio.shield(worker)
        except asyncio.CancelledError:
            # Do not release process resources while a writer can still commit.
            while not worker.done():
                try:
                    await asyncio.shield(worker)
                except asyncio.CancelledError:
                    continue
            worker.result()
            raise

    async def allowed(self, user_id: int) -> bool:
        def read(db: sqlite3.Connection, now: int) -> bool:
            return db.execute("SELECT 1 FROM captcha_passes WHERE user_id=?", (user_id,)).fetchone() is not None

        return await self._run(read, write=False)

    async def session(self, user_id: int) -> str:
        def issue(db: sqlite3.Connection, now: int) -> str:
            row = db.execute(
                "SELECT nonce FROM captcha_sessions WHERE user_id=? AND expires_at>?", (user_id, now)
            ).fetchone()
            if row:
                return str(row[0])
            nonce = secrets.token_urlsafe(32)
            db.execute(
                "INSERT OR REPLACE INTO captcha_sessions(user_id, nonce, expires_at) VALUES (?, ?, ?)",
                (user_id, nonce, now + SESSION_SECONDS),
            )
            return nonce

        return await self._run(issue)

    async def claim(self, user_id: int, session: str, token: str) -> bool:
        if not 0 < len(token) <= 2048:
            return False
        digest = hashlib.sha256(token.encode("utf-8")).digest()

        def reserve(db: sqlite3.Connection, now: int) -> bool:
            eligible = db.execute(
                "SELECT 1 FROM captcha_sessions WHERE user_id=? AND nonce=? AND expires_at>? AND attempts<? "
                "AND (claim IS NULL OR claim_until<=?) AND NOT EXISTS "
                "(SELECT 1 FROM captcha_passes WHERE user_id=?)",
                (user_id, session, now, MAX_ATTEMPTS, now, user_id),
            ).fetchone()
            if not eligible:
                return False
            # Keep the digest across failures and users for longer than the provider's token TTL.
            if not db.execute(
                "INSERT OR IGNORE INTO captcha_tokens VALUES (?, ?)", (digest, now + SESSION_SECONDS)
            ).rowcount:
                return False
            db.execute(
                "UPDATE captcha_sessions SET attempts=attempts+1, claim=?, claim_until=? WHERE user_id=?",
                (digest, now + CLAIM_SECONDS, user_id),
            )
            return True

        return await self._run(reserve)

    async def finish(self, user_id: int, session: str, token: str, *, success: bool) -> bool:
        digest = hashlib.sha256(token.encode("utf-8")).digest()

        def finish_claim(db: sqlite3.Connection, now: int) -> bool:
            released = db.execute(
                "UPDATE captcha_sessions SET claim=NULL, claim_until=NULL "
                "WHERE user_id=? AND nonce=? AND claim=? AND expires_at>? AND claim_until>?",
                (user_id, session, digest, now, now),
            ).rowcount
            if not released or not success:
                return False
            db.execute("DELETE FROM captcha_sessions WHERE user_id=?", (user_id,))
            return bool(
                db.execute(
                    "INSERT OR IGNORE INTO captcha_passes(user_id, verified_at) VALUES (?, ?)", (user_id, now)
                ).rowcount
            )

        return await self._run(finish_claim)

    @staticmethod
    def _cleanup(db: sqlite3.Connection, now: int) -> None:
        db.execute("DELETE FROM captcha_sessions WHERE expires_at<=?", (now,))
        db.execute("DELETE FROM captcha_tokens WHERE expires_at<=?", (now,))

    async def cleanup(self) -> None:
        await self._run(self._cleanup)
