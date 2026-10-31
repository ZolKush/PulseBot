from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from app import config, storage
from app.config import ServerTarget
from app.config.servers import TLSEndpoint
from app.monitoring.docker import local as local_docker
from app.monitoring.remnawave.models import MetricsSnapshot, NodeMetrics
from app.monitoring.status import cache, collectors, common, handlers, keyboards, presenter
from app.monitoring.tls import checks as tls_checks
from app.monitoring.tls import policy as tls_policy
from app.monitoring.tls import service as tls_service


def _server(**changes):
    target = ServerTarget(
        key="test",
        label="Test server",
        flag="",
        mode="local",
        expected_a_ip="",
        check_a_domains=[],
        monitor_containers=["api"],
        fail2ban_log_path="/var/log/fail2ban.log",
    )
    return replace(target, **changes)


def _valid_tls_certificate(domain, port):
    now = datetime.now(timezone.utc)
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, domain)])
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=60))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(domain)]), critical=False)
        .sign(key, hashes.SHA256())
    )
    return certificate.public_bytes(serialization.Encoding.DER), True, None


@pytest.fixture
def monitoring(isolated_storage, monkeypatch):
    monkeypatch.setattr(cache, "_STATUS_CACHE", {})
    monkeypatch.setattr(cache, "_STATUS_LOCKS", {})
    monkeypatch.setattr(handlers, "_REFRESH_LOCKS", {})
    monkeypatch.setattr(tls_service, "_TLS_REFRESH_LOCK", asyncio.Lock())
    monkeypatch.setattr(collectors, "is_admin", lambda update: True)
    monkeypatch.setattr(collectors, "check_uptime", AsyncMock(return_value="2 days"))
    monkeypatch.setattr(collectors, "meminfo", AsyncMock(return_value="1 / 2 GiB"))
    monkeypatch.setattr(collectors, "disk_root", AsyncMock(return_value="2 / 10 GiB"))
    monkeypatch.setattr(collectors, "ufw_summary_for_admin", AsyncMock(return_value=("active", [], [], [])))

    def configure(*targets):
        inventory = {target.key: target for target in targets}
        for module in (config, common, handlers, keyboards, presenter, tls_policy):
            monkeypatch.setattr(module, "SERVERS", inventory)
        dns = AsyncMock(return_value={"total": 0, "ok": 0, "bad": 0, "unknown": 0, "details": []})
        docker_process = AsyncMock(return_value=(0, "api|Up 2 days\n", ""))
        tls_fetch = AsyncMock(side_effect=_valid_tls_certificate)
        metrics = AsyncMock(
            return_value=MetricsSnapshot(
                nodes={
                    target.remnawave_uuid: NodeMetrics(
                        uuid=target.remnawave_uuid,
                        status=1,
                        online_users=3,
                        uptime_s=172800,
                        mem_total=2 * 1024**3,
                        mem_free=1024**3,
                        cpu_count=2,
                        network_rx_per_sec=0,
                        network_tx_per_sec=0,
                    )
                    for target in targets
                    if target.remnawave_uuid
                }
            )
        )
        monkeypatch.setattr(handlers, "build_dns_status_payload_live", dns)
        monkeypatch.setattr(handlers, "get_metrics_snapshot", metrics)
        monkeypatch.setattr(collectors, "get_metrics_snapshot", metrics)
        monkeypatch.setattr(local_docker, "run_exec", docker_process)
        monkeypatch.setattr(tls_checks, "_fetch_der_with_trust", tls_fetch)
        query = SimpleNamespace(answer=AsyncMock(), edit_message_text=AsyncMock())
        return SimpleNamespace(
            update=SimpleNamespace(callback_query=query),
            dns=dns,
            uptime=collectors.check_uptime,
            docker_process=docker_process,
            metrics=metrics,
            tls_fetch=tls_fetch,
        )

    return configure


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["system", "remnawave"])
async def test_refresh_recovers_cached_container_error(monitoring, source):
    target = _server(monitoring_source=source, remnawave_uuid="node" if source == "remnawave" else "")
    env = monitoring(target)
    await storage.set_docker_status_cache(target.key, {"containers": [["api", False, "ошибка: timeout", "-"]]})
    before, _ = await presenter.build_status_message(env.update, target.key)
    assert "Docker ⚠️ н/д" in before

    await handlers._refresh_status_screen(env.update, server_key=target.key)

    saved = storage.get_docker_status_cache(target.key)
    assert saved["containers"] == [["api", True, "Up 2 days", "-"]]
    text = env.update.callback_query.edit_message_text.call_args.args[0]
    assert "Docker 🟢 1/1" in text
    assert "ошибка: timeout" not in text
    assert "Контейнеры — проблемы" not in text
    env.docker_process.assert_awaited_once()
    if source == "remnawave":
        env.metrics.assert_any_await(force_refresh=True)
        env.uptime.assert_not_awaited()
    else:
        env.metrics.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("dns_fails", [False, True])
async def test_refresh_replaces_cached_host_failure_even_when_dns_fails(monitoring, dns_fails):
    target = _server()
    env = monitoring(target)
    env.uptime.side_effect = OSError("host probe failed")
    await storage.set_docker_status_cache(target.key, {"containers": [["api", False, "ошибка: timeout", "-"]]})
    before = await collectors.build_status_snapshot(env.update, target)
    assert before.uptime_text == "н/д"
    env.uptime.side_effect = None
    if dns_fails:
        env.dns.side_effect = RuntimeError("resolver failed")

    await handlers._refresh_status_screen(env.update, server_key=target.key)

    after = await collectors.build_status_snapshot(env.update, target)
    assert after is not before
    assert all(value != "н/д" for value in (after.uptime_text, after.memory_raw, after.disk_raw))
    assert after.ufw_state == "active"
    assert after.containers[0].is_up
    assert env.uptime.await_count == 2
    text = env.update.callback_query.edit_message_text.call_args.args[0]
    assert "Docker 🟢 1/1" in text
    if dns_fails:
        assert "DNS: RuntimeError" in text


@pytest.mark.asyncio
async def test_refresh_replaces_healthy_containers_on_exception_and_next_click_recovers(monitoring):
    target = _server()
    env = monitoring(target)
    await storage.set_docker_status_cache(target.key, {"containers": [["api", True, "Up 1 day", "-"]]})
    before, _ = await presenter.build_status_message(env.update, target.key)
    assert "Docker 🟢 1/1" in before
    transport = env.docker_process
    transport.side_effect = OSError("connection lost")

    await handlers._refresh_status_screen(env.update, server_key=target.key)

    failed = storage.get_docker_status_cache(target.key)["containers"]
    assert failed[0][0] == "api"
    assert failed[0][1] is False
    assert "ошибка" in failed[0][2]
    text = env.update.callback_query.edit_message_text.call_args.args[0]
    assert "Docker ⚠️ н/д" in text
    assert "Docker 🟢 1/1" not in text

    transport.side_effect = None
    await handlers._refresh_status_screen(env.update, server_key=target.key)

    assert storage.get_docker_status_cache(target.key)["containers"] == [["api", True, "Up 2 days", "-"]]
    text = env.update.callback_query.edit_message_text.call_args.args[0]
    assert "Docker 🟢 1/1" in text
    assert "connection lost" not in text


@pytest.mark.asyncio
async def test_refresh_only_changes_selected_server(monitoring):
    selected = _server()
    other = _server(key="other", label="Other server")
    env = monitoring(selected, other)
    for target in (selected, other):
        await storage.set_docker_status_cache(target.key, {"containers": [["api", False, "ошибка: timeout", "-"]]})
    other_snapshot = await collectors.build_status_snapshot(env.update, other)
    other_cache = storage.get_docker_status_cache(other.key)
    env.uptime.reset_mock()

    await handlers._refresh_status_screen(env.update, server_key=selected.key)

    assert storage.get_docker_status_cache(selected.key)["containers"][0][1] is True
    assert storage.get_docker_status_cache(other.key) == other_cache
    assert await collectors.build_status_snapshot(env.update, other) is other_snapshot
    env.dns.assert_awaited_once_with(selected)
    env.docker_process.assert_awaited_once()
    env.uptime.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("dns_fails", [False, True])
async def test_refresh_rechecks_expired_tls_certificate_even_when_dns_fails(monitoring, dns_fails):
    target = _server(tls_endpoints=(TLSEndpoint("tls.example"),))
    env = monitoring(target)
    await storage.set_tls_certificates_snapshot(
        {
            "tls.example:443": {
                "domain": "tls.example",
                "port": 443,
                "servers": [target.key],
                "status": "expired",
                "not_after": (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(),
                "fingerprint": "expired-certificate",
            }
        }
    )
    before, _ = await presenter.build_status_message(env.update, target.key)
    assert "TLS 🔴 0/1" in before
    assert "просрочен" in before
    if dns_fails:
        env.dns.side_effect = RuntimeError("resolver failed")

    await handlers._refresh_status_screen(env.update, server_key=target.key)

    env.tls_fetch.assert_awaited_once_with("tls.example", 443)
    saved = storage.tls_certificates_snapshot()["tls.example:443"]
    assert saved["status"] == "ok"
    assert saved["fingerprint"] != "expired-certificate"
    text = env.update.callback_query.edit_message_text.call_args.args[0]
    assert "TLS 🟢 1/1" in text
    assert "просрочен" not in text
    if dns_fails:
        assert "DNS: RuntimeError" in text


@pytest.mark.asyncio
async def test_refresh_recovers_tls_network_error_on_next_click(monitoring):
    target = _server(tls_endpoints=(TLSEndpoint("tls.example"),))
    env = monitoring(target)
    env.tls_fetch.side_effect = TimeoutError("TLS handshake timed out")

    await handlers._refresh_status_screen(env.update, server_key=target.key)

    failed = storage.tls_certificates_snapshot()["tls.example:443"]
    assert failed["status"] == "error"
    assert "TLS handshake timed out" in failed["error"]
    text = env.update.callback_query.edit_message_text.call_args.args[0]
    assert "TLS ⚠️ 0/1" in text
    assert "Docker 🟢 1/1" in text

    env.tls_fetch.side_effect = _valid_tls_certificate
    await handlers._refresh_status_screen(env.update, server_key=target.key)

    assert env.tls_fetch.await_count == 2
    assert storage.tls_certificates_snapshot()["tls.example:443"]["status"] == "ok"
    text = env.update.callback_query.edit_message_text.call_args.args[0]
    assert "TLS 🟢 1/1" in text
    assert "TLS handshake timed out" not in text


@pytest.mark.asyncio
async def test_tls_refresh_exception_does_not_block_dns_docker_or_metrics(monitoring, monkeypatch):
    target = _server(monitoring_source="remnawave", remnawave_uuid="node")
    env = monitoring(target)
    monkeypatch.setattr(handlers, "refresh_tls_certificates", AsyncMock(side_effect=RuntimeError("TLS cache failed")))

    await handlers._refresh_status_screen(env.update, server_key=target.key)

    env.dns.assert_awaited_once_with(target)
    env.metrics.assert_any_await(force_refresh=True)
    assert storage.get_dns_status_cache(target.key) is not None
    assert storage.get_docker_status_cache(target.key)["containers"][0][1] is True
    snapshot = await collectors.build_status_snapshot(env.update, target)
    assert snapshot.node_online is True
    assert snapshot.memory_raw != "н/д"
    text = env.update.callback_query.edit_message_text.call_args.args[0]
    assert "Docker 🟢 1/1" in text
    assert "TLS: RuntimeError" in text
