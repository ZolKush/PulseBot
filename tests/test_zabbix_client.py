import json

import httpx
import pytest

from app.monitoring.inspect_zabbix import inspect
from app.monitoring.zabbix import Host, Problem, ZabbixClient, ZabbixError

URL = "https://zabbix.example.test/api_jsonrpc.php"
TOKEN = "test-token"


def mock_client(results):
    calls = []

    def respond(request):
        assert str(request.url) == URL
        rpc = json.loads(request.content)
        method = rpc["method"]
        if method == "apiinfo.version":
            assert "Authorization" not in request.headers
            assert rpc["params"] == []
        else:
            assert request.headers["Authorization"] == f"Bearer {TOKEN}"
        calls.append((method, rpc["params"]))
        return httpx.Response(200, json={"jsonrpc": "2.0", "result": results[method], "id": rpc["id"]})

    return ZabbixClient(URL, TOKEN, transport=httpx.MockTransport(respond)), calls


@pytest.mark.asyncio
async def test_problem_updates_are_reflected_on_refresh_and_private_data_is_staff_only():
    event = {
        "eventid": "17",
        "objectid": "25",
        "acknowledged": "0",
        "severity": "2",
        "suppressed": "0",
        "cause_eventid": None,
        "r_eventid": None,
        "name": "Detailed trigger",
        "acknowledges": [{"message": ""}, {"message": "Investigation details"}],
    }
    results = {
        "host.get": [{"hostid": "19", "name": "Server A"}, {"hostid": "22", "name": "Server B"}],
        "problem.get": [event],
        "trigger.get": [{"triggerid": "25", "hosts": [{"hostid": "19"}, {"hostid": "22"}]}],
    }
    client, calls = mock_client(results)
    hosts = await client.hosts()
    assert hosts == (Host("19", "Server A"), Host("22", "Server B"))
    assert calls[0][1]["monitored_hosts"] is True
    assert await client.problems(hosts) == (Problem("19", "17", 2, False), Problem("22", "17", 2, False))
    params = calls[1][1]
    assert params["severities"] == [2, 3, 4, 5]
    assert params["suppressed"] is False and "symptom" not in params and params["recent"] is False
    assert "name" not in params["output"] and "selectAcknowledges" not in params

    event.update(acknowledged="1", severity="4", suppressed="1")
    assert await client.problems(hosts) == ()
    assert (await client.problems(hosts, staff=True))[0] == Problem(
        "19", "17", 4, True, True, "0", "Detailed trigger", "Investigation details"
    )
    event.update(suppressed="0", cause_eventid="200")
    assert (await client.problems(hosts))[0] == Problem("19", "17", 4, True, cause_eventid="200")
    event["cause_eventid"] = "0"
    assert (await client.problems(hosts))[0] == Problem("19", "17", 4, True)
    event["severity"] = "1"
    assert await client.problems(hosts) == ()
    event.update(severity="5", r_eventid="80")
    assert await client.problems(hosts) == ()
    results["problem.get"] = []
    assert await client.problems(hosts) == ()


@pytest.mark.asyncio
async def test_incomplete_inventory_or_unmappable_problem_raises():
    client, calls = mock_client(
        {
            "problem.get": [
                {
                    "eventid": "17",
                    "objectid": "25",
                    "acknowledged": "0",
                    "severity": "3",
                    "suppressed": "0",
                    "cause_eventid": "0",
                    "r_eventid": "0",
                }
            ],
            "trigger.get": [],
        }
    )
    with pytest.raises(ZabbixError):
        await client.problems([])
    assert not calls
    with pytest.raises(ZabbixError):
        await client.problems([Host("1", "Host A")])


@pytest.mark.asyncio
async def test_metrics_use_actual_item_keys_no_host_io_and_no_fictitious_zero():
    keys = [
        "docker.containers.total",
        'net.if.in["Ethernet, primary"]',
        "vfs.dev.read.await[nvme0n1]",
        "net.tcp.service[tcp,example.test,443]",
        "remnawave.node.status[1234-5678]",
        "remnawave.node.status[1234-9999]",
        "system.uptime",
        "web.https.time",
        "net.tcp.service[tcp,example.test,80]",
        "net.if.in[eth0,errors]",
        "system.run[anything]",
    ]
    rows = [{"key_": key, "name": key, "units": "", "lastvalue": "0", "lastclock": "123", "state": "0"} for key in keys]
    rows[6]["lastclock"] = "0"
    rows[7]["state"] = "1"
    client, calls = mock_client({"item.get": rows})
    metrics = {metric.key: metric for metric in await client.metrics("12")}
    assert set(metrics) == set(keys[:8])
    assert metrics["system.uptime"].value is None
    assert metrics["web.https.time"].value is None
    assert metrics["docker.containers.total"].value == "0"
    assert calls[0][1]["hostids"] == ["12"]


@pytest.mark.asyncio
async def test_diagnostics_redacts_host_names_and_parameters_and_checks_trigger_permission(capsys):
    client, calls = mock_client(
        {
            "apiinfo.version": "7.0.1",
            "host.get": [{"hostid": "10", "name": "private-host.example"}],
            "problem.get": [],
            "trigger.get": [],
            "item.get": [
                {
                    "key_": 'net.if.in["private-adapter"]',
                    "name": "private-item",
                    "units": "B",
                    "lastvalue": "private-value",
                    "lastclock": "123",
                    "state": "0",
                }
            ],
        }
    )
    await inspect(client)
    output = capsys.readouterr().out
    assert "7.0.1" in output and "net.if.in[*]  1/1 (1)" in output
    assert "private" not in output and TOKEN not in output
    assert [name for name, _ in calls] == ["apiinfo.version", "host.get", "problem.get", "trigger.get", "item.get"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(401),
        httpx.Response(302, headers={"Location": "https://elsewhere.test"}),
        httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "error": {"message": TOKEN}}),
        httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": None}),
        httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": [{"name": "No ID"}]}),
        httpx.Response(200, text="<html>Login</html>"),
    ],
)
async def test_failures_never_return_empty_healthy_result_or_leak_response(response):
    client = ZabbixClient(URL, TOKEN, transport=httpx.MockTransport(lambda _: response))
    with pytest.raises(ZabbixError) as exc:
        await client.hosts()
    assert TOKEN not in str(exc.value)
