"""Status invalidation must survive a previously started collection."""

from __future__ import annotations

import asyncio

import pytest

from app.config import ServerTarget
from app.monitoring.status import cache
from app.monitoring.status.models import StatusSnapshot


@pytest.fixture
def server(monkeypatch) -> ServerTarget:
    monkeypatch.setattr(cache, "_STATUS_CACHE", {})
    monkeypatch.setattr(cache, "_STATUS_LOCKS", {})
    monkeypatch.setattr(cache, "_STATUS_GENERATIONS", {})
    monkeypatch.setattr(cache, "STATUS_CACHE_TTL_SEC", 60)
    return ServerTarget(
        key="test",
        label="Test",
        flag="",
        mode="local",
        expected_a_ip="",
        check_a_domains=[],
        monitor_containers=[],
        fail2ban_log_path="/var/log/fail2ban.log",
    )


def _snapshot(uptime: str) -> StatusSnapshot:
    return StatusSnapshot(
        title="Статус",
        server_label="Test",
        server_flag="",
        now_text="12:00",
        uptime_text=uptime,
        memory_raw="н/д",
        disk_raw="н/д",
        ufw_state="н/д",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("admin_mode", [False, True])
async def test_status_invalidation_discards_inflight_cache_write(server, admin_mode) -> None:
    old_snapshot = _snapshot("н/д")
    new_snapshot = _snapshot("2 ч")
    started = asyncio.Event()
    release = asyncio.Event()
    new_calls = 0

    async def old_loader():
        started.set()
        await release.wait()
        return old_snapshot

    async def new_loader():
        nonlocal new_calls
        new_calls += 1
        return new_snapshot

    old_task = asyncio.create_task(cache.cached_snapshot(server, admin_mode, old_loader))
    await asyncio.wait_for(started.wait(), timeout=1)
    cache.invalidate_status_cache(server.key)
    new_task = asyncio.create_task(cache.cached_snapshot(server, admin_mode, new_loader))
    release.set()

    old_result, new_result = await asyncio.wait_for(asyncio.gather(old_task, new_task), timeout=1)
    reused = await cache.cached_snapshot(server, admin_mode, new_loader)

    assert old_result is old_snapshot
    assert new_result is new_snapshot
    assert reused is new_snapshot
    assert new_calls == 1
