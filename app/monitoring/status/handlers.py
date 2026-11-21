"""Read-only status navigation. Every open and refresh reads current Zabbix data."""

from __future__ import annotations

from telegram import LinkPreviewOptions, Update
from telegram.error import BadRequest
from telegram.ext import ContextTypes

from ...bot.guards import is_admin, require_admin, require_subscriber
from ...config import ZABBIX_API_TOKEN, ZABBIX_API_URL, logger
from ...messaging.message_cleanup import record_navigation_result
from ..zabbix import ZabbixClient, ZabbixError
from .views import UNAVAILABLE, Screen, host_list, metric_list, notice, problem_list


async def _show(update: Update, *, view: str, page: int = 0, host_id: str = "") -> None:
    query = update.callback_query
    message = update.effective_message
    if message is None:
        return
    if query:
        await query.answer()

    staff = is_admin(update)
    if view in {"hosts", "metrics"} and not staff:
        return
    screen: Screen
    try:
        client = ZabbixClient(ZABBIX_API_URL, ZABBIX_API_TOKEN)
        hosts = await client.hosts()
        if not hosts:
            screen = notice(UNAVAILABLE, "menu:status")
        elif view == "hosts":
            screen = host_list(hosts, page)
        elif view == "metrics":
            host = next((item for item in hosts if item.id == host_id), None)
            if host is None:
                screen = notice("Этот узел больше недоступен. Обновите список узлов.", "status:hosts:0")
            else:
                screen = metric_list(host, await client.metrics(host_id), page)
        else:
            problems = await client.problems(hosts, staff=staff)
            screen = problem_list(hosts, problems, page, staff=staff)
    except ZabbixError as error:
        logger.warning("Zabbix status request failed: %s", error, extra={"action": "zabbix_status_failed"})
        retry = str(query.data) if query and query.data else "menu:status"
        screen = notice(UNAVAILABLE, retry)

    # Staff access can change while an API request is in flight.
    if (staff or view in {"hosts", "metrics"}) and not is_admin(update):
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
async def status_metrics_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query and isinstance(query.data, str):
        _, _, host_id, page = query.data.split(":")
        await _show(update, view="metrics", host_id=host_id, page=int(page))
