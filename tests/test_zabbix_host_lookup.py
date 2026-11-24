import json
import sys
from unittest.mock import Mock

import httpx
import pytest

from app.monitoring import inspect_zabbix
from app.monitoring.zabbix import ZabbixClient


@pytest.mark.parametrize(
    ("status", "flags", "expected"),
    [("0", "4", ("Мониторинг включён", "обнаружен через LLD")), ("1", "0", ("Мониторинг отключён", "обычный узел"))],
)
def test_operator_can_trace_exact_visible_host(status, flags, expected, monkeypatch, capsys):
    name = "Тестовый сервис"

    def respond(request):
        rpc = json.loads(request.content)
        assert rpc["method"] == "host.get"
        assert rpc["params"] == {
            "filter": {"name": [name]},
            "output": ["hostid", "host", "name", "status", "flags"],
            "selectHostGroups": ["name"],
        }
        row = {
            "hostid": "214",
            "host": "service-monitor",
            "name": name,
            "status": status,
            "flags": flags,
            "hostgroups": [{"name": "Сервисы"}],
        }
        other = {**row, "name": name.upper(), "host": "unexpected-neighbour"}
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": [row, other]})

    client = ZabbixClient(
        "https://zabbix.example.test/api_jsonrpc.php", "private-token", transport=httpx.MockTransport(respond)
    )
    monkeypatch.setattr(inspect_zabbix, "ZabbixClient", Mock(return_value=client))
    monkeypatch.setattr(sys, "argv", ["inspect_zabbix", "--host", name])
    assert inspect_zabbix.main() == 0
    out = capsys.readouterr().out
    assert all(text in out for text in ("#214", name, "service-monitor", "Сервисы", *expected))
    assert "private-token" not in out and "unexpected-neighbour" not in out


@pytest.mark.parametrize("status", [200, 403])
def test_missing_match_and_api_denial_produce_distinct_diagnostics(status, monkeypatch, capsys):
    response = httpx.Response(status, json={"jsonrpc": "2.0", "id": 1, "result": []})
    client = ZabbixClient(
        "https://zabbix.example.test/api_jsonrpc.php", "token", transport=httpx.MockTransport(lambda _: response)
    )
    monkeypatch.setattr(inspect_zabbix, "ZabbixClient", Mock(return_value=client))
    monkeypatch.setattr(sys, "argv", ["inspect_zabbix", "--host", "Пример"])
    code = inspect_zabbix.main()
    captured = capsys.readouterr()
    if status == 200:
        assert code == 0 and "точного совпадения нет" in captured.out and not captured.err
    else:
        assert code == 1 and not captured.out and "HTTP 403" in captured.err
