"""Runtime targets built from the auto-discovered JSON server directory."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .inventory import ServerConfigDocument, load_inventory_directory
from .parsing import country_flag


@dataclass(frozen=True)
class TLSEndpoint:
    host: str
    primary_port: int = 443
    fallback_ports: tuple[int, ...] = ()


@dataclass(frozen=True)
class ServerTarget:
    key: str
    label: str
    flag: str
    mode: Literal["local", "panel"]
    expected_a_ip: str
    check_a_domains: list[str]
    monitor_containers: list[str]
    fail2ban_log_path: str
    fail2ban_enabled: bool = True
    fail2ban_timezone: str = ""
    remnawave_uuid: str = ""
    monitoring_source: Literal["system", "remnawave"] = "system"
    tls_endpoints: tuple[TLSEndpoint, ...] = ()


def build_servers(
    inventory: dict[str, ServerConfigDocument],
    *,
    timezone_name: str,
) -> dict[str, ServerTarget]:
    servers: dict[str, ServerTarget] = {}
    ordered = sorted(inventory.items(), key=lambda pair: (pair[1].display_order, pair[0]))
    for key, item in ordered:
        dns_domains = [domain.host for domain in item.domains if "dns" in domain.checks]
        tls_endpoints = tuple(
            TLSEndpoint(
                host=domain.host,
                primary_port=domain.tls_primary_port,
                fallback_ports=tuple(domain.tls_fallback_ports),
            )
            for domain in item.domains
            if "tls" in domain.checks
        )
        servers[key] = ServerTarget(
            key=key,
            label=item.label,
            flag=country_flag(item.flag),
            mode=item.connection.transport,
            expected_a_ip=item.dns.expected_a_ip,
            check_a_domains=dns_domains,
            monitor_containers=list(item.docker.containers),
            fail2ban_log_path=item.fail2ban.log_path,
            fail2ban_enabled=item.fail2ban.enabled,
            fail2ban_timezone=item.fail2ban.timezone or timezone_name,
            remnawave_uuid=item.monitoring.node_uuid,
            monitoring_source=item.monitoring.source,
            tls_endpoints=tls_endpoints,
        )
    return servers


def load_servers(path: str | Path, *, timezone_name: str) -> dict[str, ServerTarget]:
    return build_servers(load_inventory_directory(path), timezone_name=timezone_name)


def server_monitoring_fingerprint(server: ServerTarget) -> str:
    """Return a stable identity for every setting that can affect cached monitoring data."""

    payload = {
        "mode": server.mode,
        "expected_a_ip": server.expected_a_ip,
        "check_a_domains": server.check_a_domains,
        "monitor_containers": server.monitor_containers,
        "fail2ban_log_path": server.fail2ban_log_path,
        "fail2ban_enabled": server.fail2ban_enabled,
        "fail2ban_timezone": server.fail2ban_timezone,
        # Preserve existing local/panel cache and maintenance identities after
        # removing SSH configuration; this is only a historical hash slot.
        "ssh_target": "",
        "remnawave_uuid": server.remnawave_uuid,
        "monitoring_source": server.monitoring_source,
        "tls_endpoints": [
            {
                "host": endpoint.host,
                "primary_port": endpoint.primary_port,
                "fallback_ports": endpoint.fallback_ports,
            }
            for endpoint in server.tls_endpoints
        ],
    }
    encoded = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


__all__ = [
    "ServerTarget",
    "TLSEndpoint",
    "build_servers",
    "load_servers",
    "server_monitoring_fingerprint",
]
