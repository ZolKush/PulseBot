# MaintBot configuration examples

Copy the appropriate template to the configured directory as `bot.json`. Set
`ZABBIX_API_URL` to the full HTTPS API endpoint of your Zabbix installation.
Keep the read-only API token in `app/env.secrets` under `ZABBIX_API_TOKEN`.
Zabbix host permissions determine which monitored hosts the bot can display.
