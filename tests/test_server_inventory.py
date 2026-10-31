from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Literal

import pytest

from app.config import inventory as inventory_module
from app.config.inventory import InventoryError, load_inventory_directory, load_inventory_document
from app.config.servers import load_servers, server_monitoring_fingerprint


def _server(*, key: str = "nl", transport: str = "panel") -> dict[str, object]:
    return {
        "version": 1,
        "key": key,
        "label": "Example remote server",
        "flag": "NL",
        "display_order": 20 if key == "nl" else 10,
        "connection": {"transport": transport},
        "monitoring": {
            "source": "remnawave",
            "node_uuid": "00000000-0000-0000-0000-000000000001",
        },
        "dns": {"expected_a_ip": "192.0.2.10"},
        "domains": [
            {
                "host": "TLS-FALLBACK.EXAMPLE.COM.",
                "checks": ["dns", "tls"],
                "tls_primary_port": 443,
                "tls_fallback_ports": [8443],
            }
        ],
        "docker": {"containers": ["remnanode", "remnawave-nginx"] if transport == "local" else []},
        "fail2ban": {
            "enabled": transport == "local",
            "log_path": "/var/log/fail2ban.log",
            "timezone": "Europe/Amsterdam",
        },
    }


def _write(directory: Path, name: str, document: dict[str, object]) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def test_inventory_scans_arbitrarily_named_json_files_in_stable_order(tmp_path: Path) -> None:
    directory = tmp_path / "servers"
    _write(directory, "00 arbitrary name.json", _server(key="nl"))
    _write(directory, "zz-not-the-key.JSON", _server(key="main", transport="local"))
    (directory / "README.txt").write_text("ignored", encoding="utf-8")

    servers = load_servers(directory, timezone_name="Europe/Moscow")

    assert list(servers) == ["main", "nl"]
    server = servers["nl"]
    assert server.mode == "panel"
    assert server.monitoring_source == "remnawave"
    assert server.check_a_domains == ["tls-fallback.example.com"]
    assert server.monitor_containers == []
    assert server.tls_endpoints[0].primary_port == 443
    assert server.tls_endpoints[0].fallback_ports == (8443,)


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        ("local", "4b789de89c0aad29c1dd2ff639b41ebc1bcbfeddb79a35ce55abd87691ebe71d"),
        ("panel", "e110d21d6a917f645ff9fe0394c7e1a15c95007ebbe7bff4f3c509c81583ccf0"),
    ],
)
def test_existing_monitoring_identity_survives_ssh_removal(
    tmp_path: Path, mode: Literal["local", "panel"], expected: str
) -> None:
    _write(tmp_path, "server.json", _server(transport=mode))
    server = load_servers(tmp_path, timezone_name="Europe/Moscow")["nl"]

    # Digests produced by the pre-removal fingerprint for the same inventory.
    assert server_monitoring_fingerprint(server) == expected
    other_mode: Literal["local", "panel"] = "panel" if mode == "local" else "local"
    assert server_monitoring_fingerprint(replace(server, mode=other_mode)) != expected


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ("repeated_port", "must not repeat"),
        ("invalid_port", "range 1..65535"),
        ("unknown", "Extra inputs"),
    ],
)
def test_inventory_rejects_unsafe_or_unknown_options(tmp_path: Path, change: str, message: str) -> None:
    document = _server()
    domains = document["domains"]
    assert isinstance(domains, list) and isinstance(domains[0], dict)
    if change == "repeated_port":
        domains[0]["tls_fallback_ports"] = [443]
    elif change == "invalid_port":
        domains[0]["tls_primary_port"] = 70000
    else:
        document["unknown_option"] = "typo"
    path = _write(tmp_path, "server.json", document)

    with pytest.raises(InventoryError, match=message):
        load_inventory_document(path)


def test_inventory_requires_uuid_for_remnawave_source(tmp_path: Path) -> None:
    document = _server()
    document["monitoring"] = {"source": "remnawave", "node_uuid": ""}
    path = _write(tmp_path, "server.json", document)

    with pytest.raises(InventoryError, match="node_uuid is required"):
        load_inventory_document(path)


@pytest.mark.parametrize("connection", [{"transport": "ssh"}, {"transport": "panel", "target": ""}])
def test_inventory_rejects_removed_connection_options(tmp_path: Path, connection: dict[str, str]) -> None:
    document = _server()
    document["connection"] = connection
    path = _write(tmp_path, "server.json", document)

    with pytest.raises(InventoryError):
        load_inventory_document(path)


def test_inventory_rejects_duplicate_logical_keys_across_files(tmp_path: Path) -> None:
    _write(tmp_path, "first.json", _server(key="same"))
    _write(tmp_path, "completely-different-name.json", _server(key="same"))

    with pytest.raises(InventoryError, match="duplicate server key 'same'"):
        load_inventory_directory(tmp_path)


def test_inventory_rejects_duplicate_json_keys(tmp_path: Path) -> None:
    path = tmp_path / "duplicate.json"
    path.write_text(
        '{"version":1,"key":"one","key":"two","label":"Server","connection":{"transport":"local"}}',
        encoding="utf-8",
    )

    with pytest.raises(InventoryError, match="duplicate JSON key: key"):
        load_inventory_document(path)


@pytest.mark.parametrize(
    ("field", "value"),
    [("display_order", "20"), ("flag", 42)],
)
def test_inventory_rejects_coerced_json_types(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    document = _server()
    document[field] = value
    path = _write(tmp_path, "server.json", document)

    with pytest.raises(InventoryError):
        load_inventory_document(path)


def test_inventory_rejects_empty_or_missing_directory(tmp_path: Path) -> None:
    with pytest.raises(InventoryError, match="not found"):
        load_inventory_directory(tmp_path / "missing")
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(InventoryError, match="contains no .json"):
        load_inventory_directory(empty)


def test_inventory_rejects_multiple_local_servers(tmp_path: Path) -> None:
    _write(tmp_path, "one.json", _server(key="one", transport="local"))
    _write(tmp_path, "two.json", _server(key="two", transport="local"))

    with pytest.raises(InventoryError, match="only one server"):
        load_inventory_directory(tmp_path)


def test_inventory_fails_if_directory_changes_during_scan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write(tmp_path, "one.json", _server(key="one"))
    original = inventory_module.load_json_object

    def add_file_while_loading(path: Path, *, field_name: str):
        result = original(path, field_name=field_name)
        _write(tmp_path, "two.json", _server(key="two"))
        return result

    monkeypatch.setattr(inventory_module, "load_json_object", add_file_while_loading)

    with pytest.raises(InventoryError, match="changed while it was being read"):
        load_inventory_directory(tmp_path)
