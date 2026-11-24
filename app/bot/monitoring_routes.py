"""Telegram navigation for live Zabbix views."""

from telegram.ext import Application, CallbackQueryHandler

from ..monitoring.status.handlers import status_host_cb, status_host_page_cb, status_hosts_cb, status_issues_cb


def register_monitoring_routes(application: Application) -> None:
    for callback, pattern in (
        (status_hosts_cb, r"^status:hosts:[0-9]{1,9}$"),
        (status_issues_cb, r"^status:issues:[0-9]{1,9}$"),
        (status_host_cb, r"^status:host:[0-9]{1,20}$"),
        (status_host_page_cb, r"^status:(?:metrics|hostissues):[0-9]{1,20}:[0-9]{1,9}$"),
    ):
        application.add_handler(CallbackQueryHandler(callback, pattern=pattern, block=False))
