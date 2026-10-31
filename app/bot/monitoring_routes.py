"""Registration of status, TLS, Docker and fail2ban callbacks."""

from __future__ import annotations

from telegram.ext import Application, CallbackQueryHandler, CommandHandler

from ..monitoring.docker.handlers import (
    docker_back_to_status,
    docker_inspect,
    docker_list_menu,
    docker_logs,
    docker_show,
)
from ..monitoring.fail2ban.handlers import (
    f2b_back_cb,
    f2b_digest_cb,
    f2b_menu_cb,
    f2b_tail_cb,
    fail2ban_menu,
)
from ..monitoring.status.handlers import (
    dns_back_cb,
    status_dns_refresh_cb,
    status_pick_cb,
    status_refresh_cb,
    status_show_cb,
    status_tls_refresh_cb,
    status_ufw_cb,
)
from ..monitoring.tls.handlers import tls_report_cb


def _register_status_routes(
    application: Application,
    *,
    server_key_pattern: str,
) -> None:
    application.add_handler(CallbackQueryHandler(status_pick_cb, pattern=r"^status:pick$", block=False))
    for callback, pattern in (
        (status_show_cb, rf"^status:show:{server_key_pattern}$"),
        (status_refresh_cb, rf"^status:refresh:{server_key_pattern}$"),
        (status_ufw_cb, rf"^status:ufw:{server_key_pattern}$"),
        (status_dns_refresh_cb, rf"^status:dnsrefresh:{server_key_pattern}$"),
        (status_tls_refresh_cb, rf"^status:tlsrefresh:{server_key_pattern}$"),
        (tls_report_cb, rf"^tls:list:{server_key_pattern}$"),
    ):
        application.add_handler(CallbackQueryHandler(callback, pattern=pattern, block=False))

    for callback, pattern in (
        (dns_back_cb, rf"^dns:back:{server_key_pattern}$"),
        (docker_list_menu, rf"^docker:list:{server_key_pattern}$"),
        (docker_back_to_status, rf"^docker:back:{server_key_pattern}$"),
        (
            docker_show,
            rf"^docker:show:{server_key_pattern}:[a-zA-Z0-9_.\-]{{1,64}}$",
        ),
        (
            docker_inspect,
            rf"^docker:inspect:{server_key_pattern}:[a-zA-Z0-9_.\-]{{1,64}}$",
        ),
        (
            docker_logs,
            rf"^docker:logs:{server_key_pattern}:[a-zA-Z0-9_.\-]{{1,64}}:\d{{1,4}}$",
        ),
    ):
        application.add_handler(CallbackQueryHandler(callback, pattern=pattern, block=False))


def _register_fail2ban_routes(application: Application, *, server_key_pattern: str) -> None:
    application.add_handler(CommandHandler("fail2ban", fail2ban_menu, block=False))
    for callback, pattern in (
        (f2b_menu_cb, rf"^f2b:menu:{server_key_pattern}$"),
        (f2b_tail_cb, rf"^f2b:tail:{server_key_pattern}:\d{{1,5}}$"),
        (f2b_digest_cb, rf"^f2b:digest:{server_key_pattern}$"),
        (f2b_back_cb, rf"^f2b:back:{server_key_pattern}$"),
    ):
        application.add_handler(CallbackQueryHandler(callback, pattern=pattern, block=False))


def register_monitoring_routes(
    application: Application,
    *,
    server_key_pattern: str,
) -> None:
    """Register monitoring routes in their externally visible stable order."""
    _register_status_routes(
        application,
        server_key_pattern=server_key_pattern,
    )
    _register_fail2ban_routes(application, server_key_pattern=server_key_pattern)


__all__ = ["register_monitoring_routes"]
