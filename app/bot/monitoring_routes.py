"""Telegram navigation for live Zabbix views."""

from telegram.ext import Application, CallbackQueryHandler

from ..monitoring.status.handlers import status_hosts_cb, status_issues_cb, status_metrics_cb


def register_monitoring_routes(application: Application) -> None:
    for callback, pattern in (
        (status_hosts_cb, r"^status:hosts:[0-9]{1,9}$"),
        (status_issues_cb, r"^status:issues:[0-9]{1,9}$"),
        (status_metrics_cb, r"^status:metrics:[0-9]{1,20}:[0-9]{1,9}$"),
    ):
        application.add_handler(CallbackQueryHandler(callback, pattern=pattern, block=False))
