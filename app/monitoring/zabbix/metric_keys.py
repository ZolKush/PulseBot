"""Allowlisted Zabbix keys. Dynamic arguments come from real items on the host."""

import re

METRIC_PATTERNS = (
    "system.cpu.util",
    "vm.memory.util",
    "vfs.fs.dependent.size[/,pused]",
    "system.cpu.load[all,avg15]",
    "system.uptime",
    "system.cpu.util[,steal]",
    "vfs.dev.read.await[*]",
    "vfs.dev.write.await[*]",
    "net.if.in[*]",
    "net.if.out[*]",
    "docker.containers.running",
    "docker.containers.stopped",
    "docker.containers.total",
    "docker.ping",
    "remnawave.users.online",
    "remnawave.node.online[*]",
    "remnawave.node.status[*]",
    "remnawave.node.tx[*]",
    "remnawave.node.rx[*]",
    "proc.num[rw-core]",
    "zeronet.derp.route[*]",
    "net.tcp.service[tcp,*,443]",
    "zeronet.site.http[*]",
    "zeronet.tls.validation[*]",
    "zeronet.tls.expiry[*]",
    "web.https.ok",
    "web.https.code",
    "web.https.time",
)

# A wildcard denotes one nonempty Zabbix parameter. Quoted interface names
# can themselves contain commas, closing brackets or escaped double quotes.
_ARGUMENT = r'(?:"(?:\\"|[^"\r\n])+"|[^",\[\]\r\n]+)'
_KEY_PATTERNS = {pattern: re.compile(re.escape(pattern).replace(r"\*", _ARGUMENT)) for pattern in METRIC_PATTERNS}


def metric_family(key: str) -> str | None:
    return next((pattern for pattern, matcher in _KEY_PATTERNS.items() if matcher.fullmatch(key)), None)
