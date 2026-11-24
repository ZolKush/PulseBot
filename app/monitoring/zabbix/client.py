from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Sequence
from typing import Any

import httpx

from .metric_keys import METRIC_PATTERNS, metric_family
from .models import Host, HostOrigin, Metric, Problem

_MAX_RESPONSE = 8 * 1024 * 1024


class ZabbixError(Exception):
    """Safe to log. Never includes credentials, response data or host URLs."""


def _rows(value: Any) -> list[dict]:
    if not isinstance(value, list) or not all(isinstance(row, dict) for row in value):
        raise ZabbixError("Invalid Zabbix record list")
    return value


def _text(row: dict, key: str) -> str:
    value = row.get(key)
    if not isinstance(value, str):
        raise ZabbixError(f"Invalid Zabbix field {key}")
    return value


def _number(row: dict, key: str, *, nullable: bool = False) -> int:
    if nullable and key in row and row[key] is None:
        return 0
    value = str(row.get(key))
    if not value.isascii() or not value.isdecimal() or len(value) > 20:
        raise ZabbixError(f"Invalid Zabbix field {key}")
    return int(value)


def _id(row: dict, key: str) -> str:
    value = _number(row, key)
    if not value:
        raise ZabbixError(f"Empty Zabbix identifier {key}")
    return str(value)


def _flag(row: dict, key: str) -> bool:
    value = _number(row, key)
    if value not in (0, 1):
        raise ZabbixError(f"Invalid Zabbix flag {key}")
    return bool(value)


def _monitored_trigger_hosts(trigger: dict, host_ids: set[str]) -> set[str]:
    # Reading explicit statuses in one request distinguishes a disabled check
    # from a trigger missing due to a permission change or incomplete response.
    if _flag(trigger, "status"):
        return set()
    items = _rows(trigger.get("items"))
    hosts = _rows(trigger.get("hosts"))
    if not items or not hosts:
        raise ZabbixError("Incomplete Zabbix trigger relations")
    if any(_flag(item, "status") for item in items) or any(_flag(host, "status") for host in hosts):
        return set()
    members = {_id(host, "hostid") for host in hosts} & host_ids
    if not members:
        raise ZabbixError("Cannot associate active Zabbix problem with a host")
    return members


class ZabbixClient:
    def __init__(self, url: str, token: str, *, transport: httpx.AsyncBaseTransport | None = None):
        self._url = url
        self._token = token
        self._transport = transport

    async def _rpc(self, method: str, params: dict | list, *, authenticated: bool = True) -> Any:
        if not self._url or (authenticated and not self._token):
            raise ZabbixError("Zabbix API credentials are missing")
        try:
            payload = await asyncio.wait_for(self._post(method, params, authenticated=authenticated), timeout=15)
            envelope = json.loads(payload)
        except httpx.HTTPStatusError as exc:
            raise ZabbixError(f"{method} returned HTTP {exc.response.status_code}") from None
        except (httpx.HTTPError, httpx.InvalidURL, OSError, ValueError, RecursionError, asyncio.TimeoutError) as exc:
            raise ZabbixError(f"{method} failed ({type(exc).__name__})") from None
        if not isinstance(envelope, dict) or envelope.get("jsonrpc") != "2.0" or envelope.get("id") != 1:
            raise ZabbixError(f"{method} returned an invalid JSON-RPC envelope")
        if "error" in envelope:
            error = envelope["error"]
            code = error.get("code") if isinstance(error, dict) else None
            detail = f" {code}" if type(code) is int else ""
            raise ZabbixError(f"{method} returned JSON-RPC error{detail}")
        return envelope.get("result")

    async def _request(self, method: str, params: dict) -> list[dict]:
        return _rows(await self._rpc(method, params))

    async def _post(self, method: str, params: dict | list, *, authenticated: bool) -> bytearray:
        headers = {"Content-Type": "application/json-rpc"}
        if authenticated:
            headers["Authorization"] = f"Bearer {self._token}"
        request = {"jsonrpc": "2.0", "method": method, "params": params, "id": 1}
        async with (
            httpx.AsyncClient(transport=self._transport, timeout=10, follow_redirects=False) as client,
            client.stream("POST", self._url, headers=headers, json=request) as response,
        ):
            response.raise_for_status()
            data = bytearray()
            async for chunk in response.aiter_bytes():
                if len(data) + len(chunk) > _MAX_RESPONSE:
                    raise ZabbixError(f"{method} response is too large")
                data.extend(chunk)
            return data

    async def version(self) -> str:
        version = await self._rpc("apiinfo.version", [], authenticated=False)
        if not isinstance(version, str) or not re.fullmatch(r"[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,4}", version):
            raise ZabbixError("Invalid Zabbix API version")
        return version

    async def lookup_host(self, visible_name: str) -> tuple[HostOrigin, ...]:
        rows = await self._request(
            "host.get",
            {
                "filter": {"name": [visible_name]},
                "output": ["hostid", "host", "name", "status", "flags"],
                "selectHostGroups": ["name"],
            },
        )
        matches = []
        for row in rows:
            name = _text(row, "name")
            if name != visible_name:
                continue
            flags = _number(row, "flags")
            if flags not in (0, 4):
                raise ZabbixError("Invalid Zabbix host origin")
            groups = tuple(sorted(_text(group, "name") for group in _rows(row.get("hostgroups"))))
            matches.append(
                HostOrigin(
                    id=_id(row, "hostid"),
                    name=name,
                    technical_name=_text(row, "host"),
                    enabled=not _flag(row, "status"),
                    discovered=flags == 4,
                    groups=groups,
                )
            )
        return tuple(matches)

    async def check_trigger_access(self, host_id: str) -> None:
        # problem.get can legitimately return no active events. Exercise
        # trigger.get even then so the first real incident can be mapped.
        await self._request("trigger.get", {"hostids": [host_id], "output": ["triggerid"], "limit": 1})

    async def hosts(self) -> tuple[Host, ...]:
        rows = await self._request("host.get", {"output": ["hostid", "name"], "monitored_hosts": True})
        hosts = [Host(_id(row, "hostid"), _text(row, "name")) for row in rows]
        return tuple(sorted(hosts, key=lambda host: (host.name.casefold(), host.id)))

    async def problems(self, hosts: Sequence[Host], *, staff: bool = False) -> tuple[Problem, ...]:
        if not hosts:
            raise ZabbixError("No monitored hosts visible in Zabbix")
        host_ids = {host.id for host in hosts}
        fields = ["eventid", "objectid", "severity", "acknowledged", "suppressed", "cause_eventid", "r_eventid"]
        params: dict[str, Any] = {
            "output": fields,
            "hostids": sorted(host_ids),
            "source": 0,
            "object": 0,
            "recent": False,
        }
        if staff:
            fields.append("name")
            params["selectAcknowledges"] = ["message"]
        else:
            params.update(severities=[2, 3, 4, 5], suppressed=False)

        rows = await self._request("problem.get", params)
        if not rows:
            return ()
        trigger_ids = sorted({_id(row, "objectid") for row in rows})
        triggers = await self._request(
            "trigger.get",
            {
                "triggerids": trigger_ids,
                "output": ["triggerid", "status"],
                "selectHosts": ["hostid", "status"],
                "selectItems": ["status"],
            },
        )
        affected = {_id(trigger, "triggerid"): _monitored_trigger_hosts(trigger, host_ids) for trigger in triggers}
        if affected.keys() != set(trigger_ids):
            raise ZabbixError("Incomplete Zabbix trigger data")
        problems = []
        for row in rows:
            severity = _number(row, "severity")
            if severity > 5:
                raise ZabbixError("Invalid Zabbix severity")
            suppressed = _flag(row, "suppressed")
            cause = _number(row, "cause_eventid", nullable=True)
            if _number(row, "r_eventid", nullable=True) or (not staff and (severity < 2 or suppressed)):
                continue
            members = affected[_id(row, "objectid")]
            if not members:
                continue
            comment = ""
            if staff:
                messages = (_text(update, "message").strip() for update in _rows(row.get("acknowledges")))
                comment = next((message for message in messages if message), "")
            for host_id in members:
                problems.append(
                    Problem(
                        host_id,
                        _id(row, "eventid"),
                        severity,
                        _flag(row, "acknowledged"),
                        suppressed,
                        str(cause),
                        _text(row, "name") if staff else "",
                        comment,
                    )
                )
        return tuple(sorted(problems, key=lambda p: (p.host_id, -p.severity, -int(p.event_id))))

    async def metrics(self, host_id: str) -> tuple[Metric, ...]:
        rows = await self._request(
            "item.get",
            {
                "output": ["key_", "name", "units", "lastvalue", "lastclock", "state"],
                "hostids": [host_id],
                "monitored": True,
                "webitems": True,
                "search": {"key_": list(METRIC_PATTERNS)},
                "searchWildcardsEnabled": True,
                "searchByAny": True,
            },
        )
        metrics = []
        for row in rows:
            key = _text(row, "key_")
            if metric_family(key) is not None:
                lastclock = _number(row, "lastclock")
                supported = not _flag(row, "state")
                value = _text(row, "lastvalue") if lastclock and supported else None
                metrics.append(Metric(key, _text(row, "name"), _text(row, "units"), value, lastclock, supported))
        return tuple(sorted(metrics, key=lambda metric: metric.key))
