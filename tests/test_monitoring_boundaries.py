from __future__ import annotations

import asyncio
import ssl
from dataclasses import replace

import httpx
import pytest

from app import config, storage
from app.config import ServerTarget, server_monitoring_fingerprint
from app.messaging import outbox
from app.monitoring.docker import handlers as docker
from app.monitoring.remnawave import client, parser
from app.monitoring.status import dns, reconciliation
from app.monitoring.tls import checks, policy


def server(**changes):
    base = ServerTarget(
        "test", "Test", "", "local", "192.0.2.10", ["node.test"], ["api", "db"], "/var/log/fail2ban.log"
    )
    return replace(base, **changes)


@pytest.mark.parametrize("value", ["NaN", "+Inf", "-Inf", "1e999", "-1"])
def test_invalid_metric_does_not_break_other_nodes(value):
    text = (
        'remnawave_node_status{node_uuid="good"} 1\n'
        f'remnawave_node_online_users{{node_uuid="bad"}} {value}\n'
        'remnawave_node_status{node_uuid="bad"} 1\n'
    )
    nodes = parser.build_nodes(parser.parse_prometheus_text(text))
    assert nodes["good"].is_online
    assert nodes["bad"].online_users is None


def test_metric_labels_with_braces_and_timestamp_are_supported():
    text = '  remnawave_node_basic_info{node_uuid="a",node_name="Node {east}"} 1 1700000000000 \n'
    assert parser.build_nodes(parser.parse_prometheus_text(text))["a"].node_name == "Node {east}"


def test_integer_metric_values_are_accepted_without_float_only_methods():
    assert parser.build_nodes({"remnawave_node_status": {"node": ({}, 1)}})["node"].is_online


@pytest.mark.asyncio
async def test_metrics_fetch_has_a_total_deadline(monkeypatch):
    cancelled = asyncio.Event()

    async def never_finishes():
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr(client, "_fetch_metrics_text", never_finishes)
    monkeypatch.setattr(client, "REMNAWAVE_METRICS_TIMEOUT_SEC", 0.01)
    result = await asyncio.wait_for(client._do_fetch_and_build_snapshot(), timeout=1)
    assert not result.ok
    assert cancelled.is_set()


@pytest.mark.asyncio
async def test_network_error_does_not_expose_metrics_url_secrets(monkeypatch, caplog):
    marker = "PRIVATE_METRICS_QUERY_TOKEN"

    async def fail():
        raise httpx.ConnectError(f"Could not connect to https://example.test/?token={marker}")

    monkeypatch.setattr(client, "_fetch_metrics_text", fail)
    result = await client._do_fetch_and_build_snapshot()
    assert not result.ok
    assert marker not in str(result.error)
    assert marker not in caplog.text


@pytest.mark.asyncio
async def test_ipv6_expected_address_uses_aaaa_and_normalizes_addresses(monkeypatch):
    async def ipv6(domain, resolver=None):
        return ["2001:0db8:0000::1"]

    async def unexpected_ipv4(*args, **kwargs):
        raise AssertionError("IPv6 inventory must not query A")

    monkeypatch.setattr(dns, "resolve_aaaa_record", ipv6)
    monkeypatch.setattr(dns, "resolve_a_record", unexpected_ipv4)
    monkeypatch.setattr(dns, "dns_supports_custom_resolver", lambda: False)
    result = await dns.build_dns_status_payload_live(server(expected_a_ip="2001:db8::1"))
    assert result["ok"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(("other", "status"), [(["192.0.2.20"], "bad"), ([], "unknown")])
async def test_dns_does_not_hide_disagreeing_or_unreachable_resolvers(monkeypatch, other, status):
    async def resolve(domain, resolver=None):
        return ["192.0.2.10"] if resolver == "one" else other

    monkeypatch.setattr(dns, "DNS_RESOLVERS", ["one", "two"])
    monkeypatch.setattr(dns, "dns_supports_custom_resolver", lambda: True)
    monkeypatch.setattr(dns, "resolve_a_record", resolve)
    payload = await dns.build_dns_status_payload_live(server())
    assert payload[status] == 1
    assert payload["ok"] == 0


@pytest.mark.asyncio
async def test_failed_diagnostic_handshake_cannot_hide_a_bad_primary_certificate(monkeypatch):
    ports = []

    async def open_tls(domain, port, context):
        ports.append(port)
        if context.verify_mode == ssl.CERT_NONE:
            raise OSError("diagnostic backend unavailable")
        raise ssl.SSLCertVerificationError(1, "certificate verify failed")

    monkeypatch.setattr(checks, "_open_tls", open_tls)
    result = await policy.check_tls_with_fallback(policy.ConfiguredTLSEndpoint("test.example", 443, (8443,), ("test",)))
    assert result["status"] == "invalid"
    assert result["failure_kind"] == "certificate"
    assert set(ports) == {443}
    assert result["effective_port"] == 443
    assert result["used_fallback"] is False


@pytest.mark.asyncio
async def test_docker_fingerprint_survives_storage_roundtrip(isolated_storage, monkeypatch):
    target = server()
    monkeypatch.setattr(config, "SERVERS", {"test": target})
    await storage.set_docker_status_cache("test", {"containers": [["api", True, "Up", "-"]]})
    storage.initialize_storage(storage.storage_data_dir())
    removed = await reconciliation.reconcile_configured_servers({"test": target})
    assert removed["docker_status"] == 0
    assert storage.get_docker_status_cache("test")["_config_fingerprint"] == server_monitoring_fingerprint(target)


@pytest.mark.asyncio
async def test_old_digest_does_not_restore_a_deleted_server_cursor(isolated_storage):
    event = storage.make_outbox_event(
        kind="fail2ban_daily",
        recipient_ids=[42],
        payload=outbox.message_payload("historical digest"),
        completion={"type": "fail2ban_cursor", "server_key": "deleted", "cursor": {"offset": 123}},
    )
    await storage.update_important_data(lambda cfg: storage.enqueue_important_outbox(cfg, event))
    await reconciliation.reconcile_configured_servers({"test": server()})
    await storage.finalize_outbox_event("important", event["id"], success=True)
    assert storage.get_fail2ban_cursor("deleted") is None


def test_docker_token_never_selects_a_different_container_after_reordering(monkeypatch):
    target = [server()]
    monkeypatch.setattr(docker, "get_server_target", lambda key: target[0])
    token = docker._container_token("test", "api")
    target[0] = replace(target[0], monitor_containers=["db", "api"])
    assert docker._resolve_container_token("test", token) in ("api", None)
