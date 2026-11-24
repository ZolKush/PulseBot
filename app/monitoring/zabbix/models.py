from dataclasses import dataclass


@dataclass(frozen=True)
class Host:
    id: str
    name: str


@dataclass(frozen=True)
class HostOrigin(Host):
    technical_name: str
    enabled: bool
    discovered: bool
    groups: tuple[str, ...]


@dataclass(frozen=True)
class Problem:
    host_id: str
    event_id: str
    severity: int
    acknowledged: bool
    suppressed: bool = False
    cause_eventid: str = "0"
    name: str = ""
    comment: str = ""


@dataclass(frozen=True)
class Metric:
    key: str
    name: str
    units: str
    value: str | None
    lastclock: int
    supported: bool = True
