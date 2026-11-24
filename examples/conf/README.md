# MaintBot configuration examples

Copy the appropriate template to the configured directory as `bot.json`. Set
`ZABBIX_API_URL` to the full HTTPS API endpoint of your Zabbix installation.
Keep the read-only API token in `app/env.secrets` under `ZABBIX_API_TOKEN`.
Zabbix host permissions determine which monitored hosts the bot can display.

For first-time registration checks set `CAPTCHA_ENABLED`, the dedicated HTTPS
origin `CAPTCHA_URL`, and the widget's public `TURNSTILE_SITE_KEY` in JSON. Keep
`TURNSTILE_SECRET_KEY` in the secret env file. The direct template listens on
loopback, and the Docker template uses the container interface. See the project's
README for the Nginx location and loopback-only Docker port mapping.
