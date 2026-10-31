from __future__ import annotations

import asyncio
import sys
from types import SimpleNamespace

import httpx
import pytest

from app.config.servers import ServerTarget
from app.monitoring.docker import local as docker_service
from app.monitoring.remnawave import client as remnawave_metrics
from app.monitoring.status import cache as status_cache
from app.monitoring.status import collectors as status_collectors
from app.monitoring.status import dns as status_dns
from app.monitoring.status import docker as status_docker
from app.monitoring.status import jobs as status_jobs
from app.runtime.process import run_exec


@pytest.mark.asyncio
async def test_subprocess_output_is_bounded() -> None:
    rc, stdout, stderr = await run_exec(
        [sys.executable, "-c", "print('x' * 10000)"],
        timeout=5,
        max_output_bytes=1024,
    )

    assert rc == 0
    assert "output truncated by MaintBot" in stdout
    assert len(stdout) < 1200
    assert stderr == ""


@pytest.mark.asyncio
async def test_subprocess_timeout_returns_124() -> None:
    rc, _stdout, stderr = await run_exec(
        [sys.executable, "-c", "import time; time.sleep(10)"],
        timeout=1,
    )

    assert rc == 124
    assert "timeout" in stderr


@pytest.mark.asyncio
async def test_metrics_response_size_limit(monkeypatch) -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * 200)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(remnawave_metrics, "_HTTP_CLIENT", client)
    monkeypatch.setattr(remnawave_metrics, "REMNAWAVE_METRICS_URL", "https://metrics.invalid/metrics")
    monkeypatch.setattr(remnawave_metrics, "REMNAWAVE_METRICS_MAX_BYTES", 100)
    try:
        with pytest.raises(RuntimeError, match="size limit"):
            await remnawave_metrics._fetch_metrics_text()
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_dns_domains_are_checked_concurrently(monkeypatch) -> None:
    active = 0
    peak = 0

    async def fake_resolve(domain: str, resolver: str | None = None, timeout: float = 2.0) -> list[str]:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        try:
            await asyncio.sleep(0.01)
            return ["192.0.2.10"]
        finally:
            active -= 1

    server = ServerTarget(
        key="test",
        label="Test",
        flag="",
        mode="local",
        expected_a_ip="192.0.2.10",
        check_a_domains=[f"node-{index}.example" for index in range(20)],
        monitor_containers=[],
        fail2ban_log_path="/var/log/fail2ban.log",
    )
    monkeypatch.setattr(status_dns, "resolve_a_record", fake_resolve)
    monkeypatch.setattr(status_dns, "dns_supports_custom_resolver", lambda: True)
    monkeypatch.setattr(status_dns, "DNS_RESOLVERS", ["1.1.1.1", "8.8.8.8"])
    monkeypatch.setattr(status_dns, "_DNS_QUERY_SEMAPHORE", None)

    payload = await status_dns.build_dns_status_payload_live(server)

    assert payload["ok"] == 20
    assert payload["bad"] == 0
    assert 1 < peak <= 16


@pytest.mark.asyncio
async def test_local_docker_inventory_includes_all_states_and_expected_missing(monkeypatch) -> None:
    async def fake_run_exec(cmd, *, timeout):
        return (
            0,
            (
                "api|Up 2 hours (healthy)\n"
                "worker|Exited (1) 3 minutes ago\n"
                "cache|Up 1 hour (unhealthy)\n"
                "paused|Up 5 minutes (Paused)\n"
            ),
            "",
        )

    monkeypatch.setattr(docker_service, "run_exec", fake_run_exec)

    containers = await docker_service.docker_containers(["api", "expected"])

    assert containers == [
        ("api", True, "Up 2 hours (healthy)", "-"),
        ("worker", False, "Exited (1) 3 minutes ago", "-"),
        ("cache", True, "Up 1 hour (unhealthy)", "-"),
        ("paused", False, "Up 5 minutes (Paused)", "-"),
        ("expected", False, "не найден", "-"),
    ]


@pytest.mark.asyncio
async def test_regular_status_uses_docker_cache_without_live_docker_call(monkeypatch) -> None:
    server = ServerTarget(
        key="test",
        label="Test",
        flag="",
        mode="local",
        expected_a_ip="",
        check_a_domains=[],
        monitor_containers=["api"],
        fail2ban_log_path="/var/log/fail2ban.log",
    )

    async def value(result):
        return result

    async def unexpected_docker_call(_names):
        raise AssertionError("regular status must not execute Docker")

    monkeypatch.setattr(status_collectors, "check_uptime", lambda: value("1 ч"))
    monkeypatch.setattr(status_collectors, "meminfo", lambda: value("100 / 200 MiB"))
    monkeypatch.setattr(status_collectors, "disk_root", lambda: value("1G / 2G (50%)"))
    monkeypatch.setattr(status_collectors, "ufw_status_basic", lambda: value("active"))
    monkeypatch.setattr(docker_service, "docker_containers", unexpected_docker_call)
    monkeypatch.setattr(
        status_cache,
        "get_docker_status_cache",
        lambda _server_key: {
            "updated_at": "2026-07-23T02:00:00+03:00",
            "containers": [["api", True, "Up 6 hours (healthy)", "-"]],
        },
    )

    snapshot = await status_collectors.build_status_snapshot_uncached(
        SimpleNamespace(effective_user=SimpleNamespace(id=999)),
        server,
    )

    assert [(item.name, item.is_up, item.status_text) for item in snapshot.containers] == [
        ("api", True, "Up 6 hours (healthy)")
    ]


@pytest.mark.asyncio
async def test_docker_cache_refresh_persists_inventory(monkeypatch) -> None:
    server = ServerTarget(
        key="test",
        label="Test",
        flag="",
        mode="local",
        expected_a_ip="",
        check_a_domains=[],
        monitor_containers=["api"],
        fail2ban_log_path="/var/log/fail2ban.log",
    )
    saved: dict[str, object] = {}

    async def fake_docker(_names):
        return [("api", True, "Up 6 hours (healthy)", "-")]

    async def fake_save(server_key, payload):
        saved["server_key"] = server_key
        saved["payload"] = payload
        return payload

    monkeypatch.setattr(status_jobs, "SERVERS", {"test": server})
    monkeypatch.setattr(status_docker, "docker_containers", fake_docker)
    monkeypatch.setattr(status_docker, "set_docker_status_cache", fake_save)

    await status_jobs.docker_status_refresh(SimpleNamespace())

    assert saved["server_key"] == "test"
    assert saved["payload"]["containers"] == [["api", True, "Up 6 hours (healthy)", "-"]]


@pytest.mark.asyncio
async def test_daily_node_refresh_bounds_server_concurrency(monkeypatch) -> None:
    servers = {
        f"srv{index}": ServerTarget(
            key=f"srv{index}",
            label=f"Server {index}",
            flag="",
            mode="local",
            expected_a_ip="",
            check_a_domains=[],
            monitor_containers=[],
            fail2ban_log_path="/var/log/fail2ban.log",
        )
        for index in range(12)
    }
    active = 0
    peak = 0

    async def fake_collect(_server, *, admin_mode: bool):
        nonlocal active, peak
        assert admin_mode is True
        active += 1
        peak = max(peak, active)
        try:
            await asyncio.sleep(0.01)
            return {"ok": False}
        finally:
            active -= 1

    monkeypatch.setattr(status_jobs, "BOT_MODE", "mixed")
    monkeypatch.setattr(status_jobs, "SERVERS", servers)
    monkeypatch.setattr(status_jobs, "collect_disk_ufw", fake_collect)

    await status_jobs.daily_node_status_refresh(SimpleNamespace())

    assert peak == 4
