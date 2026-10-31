from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app import config, storage
from app.config.inventory import InventoryError, load_inventory_document
from app.config.servers import ServerTarget, build_servers, server_monitoring_fingerprint
from app.monitoring.docker import handlers as docker_handlers
from app.monitoring.fail2ban import source as fail2ban_source
from app.monitoring.remnawave.models import MetricsSnapshot, NodeMetrics
from app.monitoring.status import cache, collectors, docker, jobs, keyboards, local
from app.monitoring.status.views import format_status_message

ROOT = Path(__file__).resolve().parents[1]


def _server() -> ServerTarget:
    document = load_inventory_document(ROOT / "examples/conf/docker-server.json")
    return build_servers({document.key: document}, timezone_name="UTC")[document.key]


@pytest.mark.parametrize(
    "change",
    [
        {"connection": {"transport": "panel", "target": "maintbot@example.com"}},
        {"monitoring": {"source": "system", "node_uuid": ""}},
        {"docker": {"containers": ["api"]}},
        {"fail2ban": {"enabled": True}},
    ],
)
def test_panel_inventory_rejects_host_operations(tmp_path, change):
    document = json.loads((ROOT / "examples/conf/docker-server.json").read_text(encoding="utf-8"))
    document.update(change)
    target = tmp_path / "panel.json"
    target.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(InventoryError):
        load_inventory_document(target)


def test_panel_transport_changes_monitoring_identity():
    server = _server()
    assert server.mode == "panel"
    assert server_monitoring_fingerprint(server) != server_monitoring_fingerprint(replace(server, mode="local"))


@pytest.mark.asyncio
async def test_panel_status_uses_metrics_and_ignores_old_host_cache(isolated_storage, monkeypatch):
    server = _server()
    await storage.set_daily_node_status_cache(server.key, {"disk_raw": "old-host-data", "ufw_state": "active"})
    await storage.set_docker_status_cache(server.key, {"containers": [["old-host-api", True, "Up", "0"]]})
    metrics = AsyncMock(
        return_value=MetricsSnapshot(
            nodes={
                server.remnawave_uuid: NodeMetrics(
                    uuid=server.remnawave_uuid,
                    status=1,
                    online_users=3,
                    uptime_s=3600,
                    mem_total=2 * 1024**3,
                    mem_free=1024**3,
                    cpu_count=2,
                    network_rx_per_sec=0,
                    network_tx_per_sec=0,
                )
            }
        )
    )
    monkeypatch.setattr(collectors, "get_metrics_snapshot", metrics)
    monkeypatch.setattr(collectors, "is_admin", lambda update: True)
    for name in ("check_uptime", "meminfo", "disk_root", "ufw_summary_for_admin"):
        monkeypatch.setattr(collectors, name, AsyncMock(side_effect=AssertionError("host probe executed")))

    snapshot = await collectors.build_status_snapshot_uncached(SimpleNamespace(), server)

    assert snapshot.node_online is True
    assert snapshot.memory_raw != "н/д"
    assert snapshot.disk_raw == "н/д"
    assert snapshot.ufw_state == "н/д"
    assert snapshot.containers == []
    assert not snapshot.show_containers_block
    text = format_status_message(snapshot)
    assert "old-host" not in text
    assert "Docker 🟢" not in text
    metrics.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["network", "missing", "incomplete", "offline"])
async def test_unavailable_panel_metrics_remain_unknown_without_host_probes(monkeypatch, failure):
    server = _server()
    node = NodeMetrics(
        server.remnawave_uuid, 0 if failure == "offline" else 1, None, None, None, None, None, None, None
    )
    metrics = MetricsSnapshot(
        nodes={server.remnawave_uuid: node} if failure in {"incomplete", "offline"} else {},
        error="panel unavailable" if failure == "network" else "",
    )
    monkeypatch.setattr(collectors, "get_metrics_snapshot", AsyncMock(return_value=metrics))
    monkeypatch.setattr(collectors, "is_admin", lambda update: True)
    for name in ("check_uptime", "meminfo", "disk_root", "ufw_summary_for_admin"):
        monkeypatch.setattr(collectors, name, AsyncMock(side_effect=AssertionError("host probe executed")))

    snapshot = await collectors.build_status_snapshot_uncached(SimpleNamespace(), server)

    assert (snapshot.uptime_text, snapshot.memory_raw, snapshot.disk_raw, snapshot.ufw_state) == ("н/д",) * 4
    assert snapshot.node_online is (False if failure == "offline" else None)
    assert bool(snapshot.metrics_error) is (failure != "offline")
    callbacks = {
        button.callback_data
        for row in keyboards.status_actions_keyboard(True, server.key).inline_keyboard
        for button in row
    }
    assert not any("ssh" in str(value) for value in callbacks)


@pytest.mark.asyncio
async def test_panel_manual_and_scheduled_host_refreshes_do_not_probe(isolated_storage, monkeypatch):
    server = _server()
    for module, names in (
        (local, ("disk_root", "ufw_summary_for_admin")),
        (docker, ("docker_containers",)),
    ):
        for name in names:
            monkeypatch.setattr(module, name, AsyncMock(side_effect=AssertionError("host probe executed")))
    monkeypatch.setattr(jobs, "SERVERS", {server.key: server})
    monkeypatch.setattr(jobs, "BOT_MODE", "mixed")
    scheduled_docker = AsyncMock(side_effect=AssertionError("scheduled host refresh executed"))
    scheduled_disk = AsyncMock(side_effect=AssertionError("scheduled host refresh executed"))
    monkeypatch.setattr(jobs, "refresh_docker_status", scheduled_docker)
    monkeypatch.setattr(jobs, "collect_disk_ufw", scheduled_disk)

    assert not (await local.collect_disk_ufw(server, admin_mode=True))["ok"]
    await docker.refresh_docker_status(server)
    await jobs.docker_status_refresh(SimpleNamespace())
    await jobs.daily_node_status_refresh(SimpleNamespace())

    scheduled_docker.assert_not_awaited()
    scheduled_disk.assert_not_awaited()
    assert cache.daily_cache_for(server)[:2] == ("н/д", "н/д")


@pytest.mark.asyncio
async def test_panel_stale_controls_cannot_read_host(monkeypatch):
    server = _server()
    inventory = {server.key: server}
    monkeypatch.setattr(keyboards, "SERVERS", inventory)
    monkeypatch.setattr(docker_handlers, "get_server_target", lambda key: replace(server, monitor_containers=["api"]))
    monkeypatch.setattr(fail2ban_source, "SERVERS", inventory)
    forbidden = AsyncMock(side_effect=AssertionError("local fail2ban probe executed"))
    monkeypatch.setattr(fail2ban_source, "fail2ban_identity_with_sudo_async", forbidden)

    callbacks = {
        button.callback_data
        for row in keyboards.status_actions_keyboard(True, server.key).inline_keyboard
        for button in row
    }
    assert f"status:refresh:{server.key}" in callbacks
    assert f"tls:list:{server.key}" in callbacks
    assert not any(str(value).startswith(("docker:", "f2b:", "status:ufw:")) for value in callbacks)
    assert not docker_handlers._is_server_container_allowed(server.key, "api")
    assert docker_handlers._filtered_containers(server.key) == []
    assert await fail2ban_source.file_identity(server.key, "/var/log/fail2ban.log") is None
    forbidden.assert_not_awaited()


def test_panel_preflight_does_not_require_local_helper(monkeypatch):
    from app.config import checks

    monkeypatch.setattr(config, "SERVERS", {"panel": _server()})
    monkeypatch.setattr(config, "PRIVILEGED_HELPER_BIN", "/missing/helper")
    monkeypatch.setattr(config, "SUDO_BIN", "")

    errors = checks.validate_configuration()

    assert not any("helper" in item or "sudo" in item for item in errors)
