"""Find an inventory host named by a RemnaWave disconnection event."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterator, Mapping, Sequence

from .models import Host

_DISCONNECTION = re.compile(r"remnawave: *(.+?) отключ[её]на? от панели(?:[.!?]|\.\.\.)?", re.IGNORECASE)
_FLAG_PREFIX = re.compile(r"^[\U0001f1e6-\U0001f1ff]{2} *")
_SMALL_CAPITAL = "LATIN LETTER SMALL CAPITAL "
_COUNTRY_PREFIXES = {"EE", "NL", "PL", "RU", "SE"}


def _normalize(value: str) -> str:
    value = unicodedata.normalize("NFKC", value)
    if any(not char.isspace() and unicodedata.category(char) in {"Cc", "Cf"} for char in value):
        return ""
    return " ".join(value.split())


def _without_decoration(name: str) -> str:
    if flag := _FLAG_PREFIX.match(name):
        return name[flag.end() :]
    prefix, _, remaining = name.partition(" ")
    if len(prefix) != 2:
        return ""
    code = "".join(
        char if char.isascii() and char.isupper() else unicodedata.name(char, "").removeprefix(_SMALL_CAPITAL)
        for char in prefix
    )
    return remaining if code in _COUNTRY_PREFIXES else ""


def _name_candidates(name: str) -> Iterator[str]:
    while name:
        yield name.casefold()
        name = _without_decoration(name)


def remnawave_hosts(hosts: Sequence[Host]) -> dict[str, str]:
    """Index full names and decorative aliases, marking ambiguous names with an empty ID."""
    candidates: dict[str, set[str]] = {}
    for host in hosts:
        for name in _name_candidates(_normalize(host.name)):
            candidates.setdefault(name, set()).add(host.id)
    return {name: next(iter(host_ids)) if len(host_ids) == 1 else "" for name, host_ids in candidates.items()}


def remnawave_target(event_name: str, hosts_by_name: Mapping[str, str]) -> str | None:
    match = _DISCONNECTION.fullmatch(_normalize(event_name))
    if match is None:
        return None
    for name in _name_candidates(match[1]):
        host_id = hosts_by_name.get(name)
        if host_id is not None:
            return host_id or None
    return None
