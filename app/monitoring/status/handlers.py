"""Read-only status navigation. Every open and refresh reads current Zabbix data."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from weakref import WeakValueDictionary

from telegram import LinkPreviewOptions, Update
from telegram.error import BadRequest
from telegram.ext import ContextTypes

from ...bot.guards import (
    get_user_id,
    get_user_meta,
    has_subscriber_access,
    is_admin,
    is_enabled,
    require_admin,
    require_subscriber,
)
from ...config import ZABBIX_API_TOKEN, ZABBIX_API_URL, logger
from ...messaging.message_cleanup import record_navigation_result
from ..zabbix import Host, Problem, ZabbixClient, ZabbixError
from .views import HOSTS_PER_PAGE, UNAVAILABLE, Screen, host_card, host_list, metric_list, notice, problem_list


@dataclass
class _PanelEdits:
    latest: int = 0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


# Only running requests retain the per-message edit order.
_panels: WeakValueDictionary[tuple[int, int], _PanelEdits] = WeakValueDictionary()


async def _problems_for_host(client: ZabbixClient, hosts: tuple[Host, ...], host_id: str) -> tuple[Problem, ...]:
    problems = await client.problems(hosts, staff=True)
    return tuple(issue for issue in problems if issue.host_id == host_id)


async def _load_screen(
    client: ZabbixClient, view: str, page: int, host_id: str, staff: bool, *, category: str = ""
) -> Screen:
    hosts = await client.hosts()
    if not hosts:
        return notice(UNAVAILABLE, "menu:status")
    if view == "hosts":
        return host_list(hosts, await client.problems(hosts, staff=True), page)
    if view == "issues":
        return problem_list(hosts, await client.problems(hosts, staff=staff), page, staff=staff)

    index = next((i for i, host in enumerate(hosts) if host.id == host_id), None)
    if index is None:
        return notice("Этот узел больше недоступен. Обновите список узлов.", "status:hosts:0")
    host = hosts[index]
    if view == "metrics":
        return metric_list(host, await client.metrics(host.id), page, category=category)
    if view == "hostissues":
        return problem_list((host,), await _problems_for_host(client, hosts, host.id), page, staff=True, host=host)
    metrics, problems = await asyncio.gather(client.metrics(host.id), _problems_for_host(client, hosts, host.id))
    return host_card(host, metrics, problems, index // HOSTS_PER_PAGE)


async def _show(update: Update, *, view: str, page: int = 0, host_id: str = "", category: str = "") -> None:
    query = update.callback_query
    message = update.effective_message
    if message is None:
        return
    panel = _panels.setdefault((message.chat_id, message.message_id), _PanelEdits())
    panel.latest += 1
    selection = panel.latest
    if query:
        await query.answer()

    staff = is_admin(update)
    if view != "issues" and not staff:
        return
    screen: Screen
    try:
        client = ZabbixClient(ZABBIX_API_URL, ZABBIX_API_TOKEN)
        screen = await _load_screen(client, view, page, host_id, staff, category=category)
    except ZabbixError as error:
        logger.warning("Zabbix status request failed: %s", error, extra={"action": "zabbix_status_failed"})
        retry = str(query.data) if query and query.data else "menu:status"
        screen = notice(UNAVAILABLE, retry)

    async with panel.lock:
        # Both the selection and access can change while waiting for Zabbix or Telegram.
        if selection != panel.latest or (staff and not is_admin(update)):
            return
        if not staff and (not is_enabled(update) or not has_subscriber_access(get_user_meta(get_user_id(update) or 0))):
            return
        text, markup = screen
        preview = LinkPreviewOptions(is_disabled=True)
        if query:
            try:
                result = await query.edit_message_text(text, reply_markup=markup, link_preview_options=preview)
            except BadRequest as error:
                if "message is not modified" in str(error).lower():
                    return
                raise
        else:
            result = await message.reply_text(text, reply_markup=markup, link_preview_options=preview)
        await record_navigation_result(update, result)


@require_subscriber
async def cmd_health(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _show(update, view="hosts" if is_admin(update) else "issues")


@require_subscriber
async def status_issues_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query and isinstance(query.data, str):
        await _show(update, view="issues", page=int(query.data.rsplit(":", 1)[1]))


@require_admin
async def status_hosts_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query and isinstance(query.data, str):
        await _show(update, view="hosts", page=int(query.data.rsplit(":", 1)[1]))


@require_admin
async def status_host_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query and isinstance(query.data, str):
        await _show(update, view="host", host_id=query.data.rsplit(":", 1)[1])


@require_admin
async def status_host_page_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query and isinstance(query.data, str):
        _, view, host_id, target = query.data.split(":", 3)
        category, _, page = target.rpartition(":")
        await _show(update, view=view, host_id=host_id, page=int(page), category=category)
