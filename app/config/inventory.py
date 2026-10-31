"""Strict schema and directory loader for per-server JSON configuration."""

from __future__ import annotations

import ipaddress
import re
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from .json_files import JsonConfigError, load_json_object
from .validators import SERVER_KEY_PATTERN, is_container_name, is_uuid

MAX_SERVER_FILES = 256


class InventoryError(RuntimeError):
    """Raised when the inventory cannot be loaded safely."""


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True, hide_input_in_errors=True)


class ConnectionInventory(_StrictModel):
    transport: Literal["local", "panel"]


class MonitoringInventory(_StrictModel):
    source: Literal["system", "remnawave"] = "system"
    node_uuid: str = ""

    @model_validator(mode="after")
    def _validate_node_uuid(self) -> MonitoringInventory:
        if self.node_uuid and not is_uuid(self.node_uuid):
            raise ValueError("monitoring.node_uuid has invalid UUID format")
        if self.source == "remnawave" and not self.node_uuid:
            raise ValueError("monitoring.node_uuid is required when source='remnawave'")
        if self.source == "system" and self.node_uuid:
            raise ValueError("monitoring.node_uuid is only valid when source='remnawave'")
        return self


class DnsInventory(_StrictModel):
    expected_a_ip: str = ""

    @field_validator("expected_a_ip")
    @classmethod
    def _validate_expected_ip(cls, value: str) -> str:
        if not value:
            return ""
        try:
            return str(ipaddress.ip_address(value))
        except ValueError as exc:
            raise ValueError("dns.expected_a_ip must be an IPv4 or IPv6 address") from exc


def _default_domain_checks() -> list[Literal["dns", "tls"]]:
    return ["dns", "tls"]


class DomainInventory(_StrictModel):
    host: str
    checks: list[Literal["dns", "tls"]] = Field(default_factory=_default_domain_checks)
    tls_primary_port: int = 443
    tls_fallback_ports: list[int] = Field(default_factory=list)

    @field_validator("host")
    @classmethod
    def _normalize_host(cls, value: str) -> str:
        host = value.strip().lower().rstrip(".")
        if not host or len(host) > 253:
            raise ValueError("domain host is empty or too long")
        try:
            ascii_host = host.encode("idna").decode("ascii")
        except UnicodeError as exc:
            raise ValueError("domain host is not valid IDNA") from exc
        labels = ascii_host.split(".")
        if any(
            not label
            or len(label) > 63
            or label.startswith("-")
            or label.endswith("-")
            or re.fullmatch(r"[a-z0-9-]+", label) is None
            for label in labels
        ):
            raise ValueError("domain host has invalid labels")
        return ascii_host

    @field_validator("checks")
    @classmethod
    def _validate_checks(cls, value: list[str]) -> list[str]:
        unique = list(dict.fromkeys(value))
        if not unique:
            raise ValueError("domains[].checks must contain dns and/or tls")
        return unique

    @field_validator("tls_primary_port")
    @classmethod
    def _validate_primary_port(cls, value: int) -> int:
        if not 1 <= int(value) <= 65535:
            raise ValueError("tls_primary_port must be in range 1..65535")
        return int(value)

    @field_validator("tls_fallback_ports")
    @classmethod
    def _validate_fallback_ports(cls, value: list[int]) -> list[int]:
        ports = list(dict.fromkeys(int(port) for port in value))
        if any(not 1 <= port <= 65535 for port in ports):
            raise ValueError("tls_fallback_ports must contain ports in range 1..65535")
        return ports

    @model_validator(mode="after")
    def _validate_tls_options(self) -> DomainInventory:
        if "tls" not in self.checks and (self.tls_primary_port != 443 or self.tls_fallback_ports):
            raise ValueError("TLS ports require 'tls' in domains[].checks")
        if self.tls_primary_port in self.tls_fallback_ports:
            raise ValueError("tls_fallback_ports must not repeat tls_primary_port")
        return self


class DockerInventory(_StrictModel):
    containers: list[str] = Field(default_factory=list)

    @field_validator("containers")
    @classmethod
    def _validate_containers(cls, value: list[str]) -> list[str]:
        containers = list(dict.fromkeys(item.strip() for item in value if item.strip()))
        invalid = [name for name in containers if not is_container_name(name)]
        if invalid:
            raise ValueError(f"invalid Docker container names: {', '.join(invalid)}")
        return containers


class Fail2BanInventory(_StrictModel):
    enabled: bool = True
    log_path: str = "/var/log/fail2ban.log"
    timezone: str = ""

    @field_validator("log_path")
    @classmethod
    def _validate_log_path(cls, value: str) -> str:
        if not value or "\x00" in value or len(value) > 1000:
            raise ValueError("fail2ban.log_path is invalid")
        return value

    @field_validator("timezone")
    @classmethod
    def _validate_timezone(cls, value: str) -> str:
        if not value:
            return ""
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"unknown fail2ban timezone: {value}") from exc
        return value


class ServerInventory(_StrictModel):
    label: str
    flag: str = ""
    display_order: int = 100
    connection: ConnectionInventory
    monitoring: MonitoringInventory = Field(default_factory=MonitoringInventory)
    dns: DnsInventory = Field(default_factory=DnsInventory)
    domains: list[DomainInventory] = Field(default_factory=list)
    docker: DockerInventory = Field(default_factory=DockerInventory)
    fail2ban: Fail2BanInventory = Field(default_factory=Fail2BanInventory)

    @field_validator("label")
    @classmethod
    def _validate_label(cls, value: str) -> str:
        if not value or len(value) > 80:
            raise ValueError("server label is empty or too long")
        return value

    @field_validator("flag")
    @classmethod
    def _validate_flag(cls, value: str) -> str:
        flag = value.upper()
        if flag and (len(flag) != 2 or not flag.isalpha()):
            raise ValueError("server flag must be an ISO 3166-1 alpha-2 code")
        return flag

    @field_validator("display_order")
    @classmethod
    def _validate_display_order(cls, value: int) -> int:
        if isinstance(value, bool) or not 0 <= int(value) <= 10_000:
            raise ValueError("server display_order must be in range 0..10000")
        return int(value)

    @model_validator(mode="after")
    def _validate_domains(self) -> ServerInventory:
        if self.connection.transport == "panel":
            if self.monitoring.source != "remnawave":
                raise ValueError("panel transport requires monitoring.source='remnawave'")
            if self.docker.containers or self.fail2ban.enabled:
                raise ValueError("panel transport requires empty docker.containers and fail2ban.enabled=false")
        hosts = [item.host for item in self.domains]
        if len(set(hosts)) != len(hosts):
            raise ValueError("server domains must contain unique hosts")
        if self.dns.expected_a_ip and not any("dns" in item.checks for item in self.domains):
            raise ValueError("dns.expected_a_ip requires at least one domain with the dns check")
        return self


class ServerConfigDocument(ServerInventory):
    """One complete server definition stored in one JSON file."""

    version: Literal[1]
    key: str

    @field_validator("key")
    @classmethod
    def _validate_server_key(cls, value: str) -> str:
        pattern = re.compile(rf"^{SERVER_KEY_PATTERN}$")
        if not pattern.fullmatch(value):
            raise ValueError(f"invalid server key: {value}")
        return value


def _discover_json_files(directory: Path) -> list[Path]:
    if not directory.exists():
        raise InventoryError(f"SERVER_CONFIG_DIR not found: {directory}")
    if not directory.is_dir():
        raise InventoryError(f"SERVER_CONFIG_DIR is not a directory: {directory}")
    try:
        files = sorted(
            (path for path in directory.iterdir() if path.suffix.lower() == ".json"),
            key=lambda path: (path.name.casefold(), path.name),
        )
    except OSError as exc:
        raise InventoryError(f"cannot scan SERVER_CONFIG_DIR {directory}: {exc}") from exc
    non_files = [path.name for path in files if not path.is_file()]
    if non_files:
        raise InventoryError(f"SERVER_CONFIG_DIR contains non-file JSON entries: {', '.join(non_files)}")
    if not files:
        raise InventoryError(f"SERVER_CONFIG_DIR contains no .json server files: {directory}")
    if len(files) > MAX_SERVER_FILES:
        raise InventoryError(f"SERVER_CONFIG_DIR contains more than {MAX_SERVER_FILES} server files")
    return files


def _fingerprint(files: list[Path]) -> tuple[tuple[str, int, int, int, int], ...]:
    try:
        return tuple(
            (path.name, stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)
            for path in files
            for stat in (path.stat(),)
        )
    except OSError as exc:
        raise InventoryError(f"cannot stat a server configuration file: {exc}") from exc


def load_inventory_directory(path: str | Path) -> dict[str, ServerConfigDocument]:
    """Load every immediate ``*.json`` file as one deterministic inventory."""

    directory = Path(path)
    files = _discover_json_files(directory)
    initial_fingerprint = _fingerprint(files)
    servers: dict[str, ServerConfigDocument] = {}
    sources: dict[str, str] = {}
    for config_path in files:
        try:
            raw = load_json_object(config_path, field_name="server configuration")
            document = ServerConfigDocument.model_validate(raw)
        except (JsonConfigError, ValidationError) as exc:
            raise InventoryError(f"invalid server configuration {config_path.name}: {exc}") from None
        if document.key in servers:
            raise InventoryError(
                f"duplicate server key '{document.key}' in {sources[document.key]} and {config_path.name}"
            )
        servers[document.key] = document
        sources[document.key] = config_path.name

    final_files = _discover_json_files(directory)
    if _fingerprint(final_files) != initial_fingerprint:
        raise InventoryError(f"SERVER_CONFIG_DIR changed while it was being read: {directory}")
    local_count = sum(item.connection.transport == "local" for item in servers.values())
    if local_count > 1:
        raise InventoryError("only one server may use connection.transport='local'")
    return servers


def load_inventory_document(path: str | Path) -> ServerConfigDocument:
    """Load one server file; retained as a small public validation helper."""

    try:
        raw = load_json_object(path, field_name="server configuration")
        return ServerConfigDocument.model_validate(raw)
    except (JsonConfigError, ValidationError) as exc:
        raise InventoryError(f"invalid server configuration {Path(path)}: {exc}") from None


__all__ = [
    "DomainInventory",
    "InventoryError",
    "MAX_SERVER_FILES",
    "ServerConfigDocument",
    "load_inventory_directory",
    "load_inventory_document",
]
