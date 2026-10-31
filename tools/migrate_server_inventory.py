"""Legacy stage 1: convert positional .env server fields to intermediate TOML."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from contextlib import suppress
from pathlib import Path
from typing import Any

from dotenv.parser import parse_stream


def _load_legacy_env(path: Path) -> dict[str, str]:
    if not path.is_file():
        raise ValueError(f"legacy env is not a regular file: {path}")
    result: dict[str, str] = {}
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as stream:
            for binding in parse_stream(stream):
                if binding.error:
                    raise ValueError(f"legacy env has invalid syntax at line {binding.original.line}")
                if binding.key is None:
                    continue
                key = str(binding.key)
                if key in result:
                    raise ValueError(f"legacy env has duplicate key: {key}")
                if binding.value is None:
                    raise ValueError(f"legacy env key has no assigned value: {key}")
                result[key] = str(binding.value)
    except UnicodeError as exc:
        raise ValueError(f"legacy env is not valid UTF-8: {path}") from exc
    return result


def _list(raw: object, *, keep_empty: bool = False) -> list[str]:
    normalized = str(raw or "").strip()
    if not normalized:
        return []
    values = [part.strip() for part in normalized.split(",")]
    return values if keep_empty else [value for value in values if value]


def _groups(raw: object) -> list[list[str]]:
    value = str(raw or "").strip()
    return [_list(part) for part in value.split(";")] if value else []


def _bool(raw: object, default: bool) -> bool:
    value = str(raw or "").strip().lower()
    if not value:
        return default
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise ValueError("invalid boolean value")


def _key(raw: object, fallback: str) -> str:
    value = re.sub(r"[^a-z0-9_-]+", "-", str(raw or fallback).strip().lower()).strip("-_")
    return (value or fallback)[:12]


def _at(values: list[str], index: int, default: str = "") -> str:
    return values[index] if index < len(values) else default


def _aligned(env: dict[str, str], name: str, total: int, *, keep_empty: bool = False) -> list[str]:
    values = _list(env.get(name), keep_empty=keep_empty)
    if values and len(values) != total:
        raise ValueError(f"{name} contains {len(values)} values; expected exactly {total}")
    return values


def _domain_group(groups: list[list[str]], index: int, total: int) -> list[str]:
    if not groups:
        return []
    if len(groups) == total:
        return groups[index]
    if len(groups) == 1 and total == 1:
        return groups[0]
    if len(groups) == 1 and len(groups[0]) == total:
        return [groups[0][index]]
    raise ValueError("REMOTE_SERVER_DOMAINS cannot be mapped unambiguously to SSH targets")


def _fallback_map(values: list[str]) -> dict[str, list[int]]:
    result: dict[str, list[int]] = {}
    for value in values:
        host, separator, raw_port = value.rpartition(":")
        if not separator or not host or not raw_port.isdigit():
            raise ValueError(f"invalid --tls-fallback value: {value}; expected HOST:PORT")
        port = int(raw_port)
        if not 1 <= port <= 65535:
            raise ValueError(f"fallback port out of range: {value}")
        result.setdefault(host.lower().rstrip("."), []).append(port)
    return result


def _server(
    *,
    key: str,
    label: str,
    flag: str,
    transport: str,
    node_uuid: str,
    expected_ip: str,
    domains: list[str],
    containers: list[str],
    fail2ban_enabled: bool,
    fail2ban_path: str,
    fail2ban_timezone: str,
    fallbacks: dict[str, list[int]],
) -> dict[str, Any]:
    return {
        "key": key,
        "label": label or key,
        "flag": flag.upper(),
        "transport": transport,
        "monitoring_source": "remnawave" if node_uuid else "system",
        "node_uuid": node_uuid,
        "expected_ip": expected_ip,
        "domains": [
            {
                "host": host.lower().rstrip("."),
                "fallback_ports": fallbacks.get(host.lower().rstrip("."), []),
            }
            for host in domains
        ],
        "containers": list(dict.fromkeys(containers)),
        "fail2ban_enabled": fail2ban_enabled,
        "fail2ban_path": fail2ban_path or "/var/log/fail2ban.log",
        "fail2ban_timezone": fail2ban_timezone,
    }


def migrate(env: dict[str, str], *, fallbacks: dict[str, list[int]]) -> list[dict[str, Any]]:
    timezone = env.get("TZ", "Europe/Moscow")
    default_containers = _list(env.get("MONITOR_CONTAINERS"))
    servers = [
        _server(
            key=_key(env.get("LOCAL_SERVER_CODE"), "local"),
            label=env.get("LOCAL_SERVER_LABEL", "Local server"),
            flag=env.get("LOCAL_SERVER_FLAG", ""),
            transport="local",
            node_uuid=env.get("LOCAL_SERVER_REMNAWAVE_UUID", "").strip(),
            expected_ip=env.get("EXPECTED_A_IP", "").strip(),
            domains=_list(env.get("CHECK_A_DOMAINS")),
            containers=default_containers,
            fail2ban_enabled=_bool(env.get("FAIL2BAN_ENABLED"), True),
            fail2ban_path=env.get("FAIL2BAN_LOG_PATH", "/var/log/fail2ban.log"),
            fail2ban_timezone=env.get("FAIL2BAN_TIMEZONE", "") or timezone,
            fallbacks=fallbacks,
        )
    ]
    if not _bool(env.get("REMOTE_SERVER_ENABLED"), True):
        return servers

    targets = _list(env.get("REMOTE_SERVER_SSH_TARGETS"))
    if not targets and env.get("REMOTE_SERVER_SSH_TARGET", "").strip():
        targets = [env["REMOTE_SERVER_SSH_TARGET"].strip()]
    total = len(targets)
    if not total:
        return servers

    codes = _aligned(env, "REMOTE_SERVER_CODES", total)
    labels = _aligned(env, "REMOTE_SERVER_LABELS", total)
    flags = _aligned(env, "REMOTE_SERVER_FLAGS", total)
    ips = _aligned(env, "REMOTE_SERVER_EXPECTED_A_IPS", total)
    uuids = _aligned(env, "REMOTE_SERVER_REMNAWAVE_UUIDS", total, keep_empty=True)
    singular_uuid = env.get("REMOTE_SERVER_REMNAWAVE_UUID", "").strip()
    if singular_uuid:
        if total != 1 or (uuids and uuids[0] != singular_uuid):
            raise ValueError("REMOTE_SERVER_REMNAWAVE_UUID conflicts with the remote server list")
        uuids = [singular_uuid]
    domain_groups = _groups(env.get("REMOTE_SERVER_DOMAINS"))
    for index in range(total):
        node_uuid = _at(uuids, index)
        if not node_uuid:
            raise ValueError("SSH monitoring was removed; configure a RemnaWave UUID for every remote server")
        code_fallback = env.get("REMOTE_SERVER_CODE", "remote") if total == 1 else f"srv{index + 1}"
        domains = _domain_group(domain_groups, index, total) if domain_groups else []
        if not domains and total == 1:
            domains = _list(env.get("REMOTE_SERVER_CHECK_A_DOMAINS"))
        servers.append(
            _server(
                key=_key(_at(codes, index), code_fallback),
                label=_at(labels, index, env.get("REMOTE_SERVER_LABEL", f"Server {index + 1}")),
                flag=_at(flags, index, env.get("REMOTE_SERVER_FLAG", "")),
                transport="panel",
                node_uuid=node_uuid,
                expected_ip=_at(ips, index, env.get("REMOTE_SERVER_EXPECTED_A_IP", "")),
                domains=domains,
                containers=[],
                fail2ban_enabled=False,
                fail2ban_path="/var/log/fail2ban.log",
                fail2ban_timezone=timezone,
                fallbacks=fallbacks,
            )
        )
    keys = [server["key"] for server in servers]
    if len(set(keys)) != len(keys):
        raise ValueError(f"server keys are not unique after normalization: {keys}")
    return servers


def _quoted(value: object) -> str:
    return json.dumps(str(value), ensure_ascii=False)


def render(servers: list[dict[str, Any]]) -> str:
    lines = ["version = 1", ""]
    for server in servers:
        prefix = f"servers.{server['key']}"
        lines.extend(
            [
                f"[{prefix}]",
                f"label = {_quoted(server['label'])}",
                f"flag = {_quoted(server['flag'])}",
                "",
                f"[{prefix}.connection]",
                f"transport = {_quoted(server['transport'])}",
            ]
        )
        lines.extend(["", f"[{prefix}.monitoring]", f"source = {_quoted(server['monitoring_source'])}"])
        if server["node_uuid"]:
            lines.append(f"node_uuid = {_quoted(server['node_uuid'])}")
        lines.extend(["", f"[{prefix}.dns]", f"expected_a_ip = {_quoted(server['expected_ip'])}"])
        for domain in server["domains"]:
            fallback_ports = ", ".join(str(port) for port in domain["fallback_ports"])
            lines.extend(
                [
                    "",
                    f"[[{prefix}.domains]]",
                    f"host = {_quoted(domain['host'])}",
                    'checks = ["dns", "tls"]',
                    "tls_primary_port = 443",
                    f"tls_fallback_ports = [{fallback_ports}]",
                ]
            )
        containers = ", ".join(_quoted(name) for name in server["containers"])
        lines.extend(
            [
                "",
                f"[{prefix}.docker]",
                f"containers = [{containers}]",
                "",
                f"[{prefix}.fail2ban]",
                f"enabled = {str(server['fail2ban_enabled']).lower()}",
                f"log_path = {_quoted(server['fail2ban_path'])}",
                f"timezone = {_quoted(server['fail2ban_timezone'])}",
                "",
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def _write_exclusive(path: Path, document: str) -> None:
    """Create ``path`` once with private permissions and clean failed writes."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)

    descriptor: int | None = None
    created = False
    try:
        descriptor = os.open(path, flags, 0o600)
        created = True
        if os.name != "nt":
            os.fchmod(descriptor, 0o600)  # type: ignore[attr-defined]
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as output:
            descriptor = None
            output.write(document)
            output.flush()
            os.fsync(output.fileno())
    except BaseException:
        if descriptor is not None:
            os.close(descriptor)
        if created:
            with suppress(FileNotFoundError):
                path.unlink()
        raise


def _safe_migration_error(exc: Exception, output: Path) -> str:
    if isinstance(exc, FileExistsError):
        return f"выходной файл уже существует и не был изменён: {output}"
    detail = " ".join(str(exc).split())
    return detail or type(exc).__name__


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", type=Path, default=Path("app/.env"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--tls-fallback", action="append", default=[], metavar="HOST:PORT")
    args = parser.parse_args()
    try:
        raw = _load_legacy_env(args.env)
        servers = migrate(raw, fallbacks=_fallback_map(args.tls_fallback))
        args.output.parent.mkdir(parents=True, exist_ok=True)
        _write_exclusive(args.output, render(servers))
    except (OSError, UnicodeError, ValueError) as exc:
        print(f"Ошибка миграции: {_safe_migration_error(exc, args.output)}", file=sys.stderr)
        return 1
    print(f"Created {args.output} with {len(servers)} server blocks.")
    print("Next run tools/migrate_config_layout.py with this TOML; MaintBot does not read TOML at runtime.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
