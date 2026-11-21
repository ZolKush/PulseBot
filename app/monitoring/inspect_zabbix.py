"""Print a shareable Zabbix inventory report using the bot's configured API access."""

import asyncio
import sys
from collections import defaultdict

from .zabbix import Metric, ZabbixClient, ZabbixError
from .zabbix.metric_keys import metric_family


async def inspect(client: ZabbixClient) -> None:
    print(f"Версия Zabbix API: {await client.version()}")
    hosts = await client.hosts()
    print(f"Узлов под мониторингом: {len(hosts)}")
    problems = await client.problems(hosts)
    await client.check_trigger_access(hosts[0].id)
    print(f"Публичных проблем: {len({problem.event_id for problem in problems})}")
    print("Метрики по группам: поддерживается / всего включено (есть значение).")
    for index, host in enumerate(hosts, start=1):
        metrics = await client.metrics(host.id)
        print(f"Узел {index}: {len(metrics)} элементов из списка метрик бота")
        groups: dict[str, list[Metric]] = defaultdict(list)
        for metric in metrics:
            family = metric_family(metric.key)
            if family:
                groups[family].append(metric)
        for family, members in sorted(groups.items()):
            supported = sum(metric.supported for metric in members)
            sampled = sum(metric.value is not None for metric in members)
            print(f"  {family}  {supported}/{len(members)} ({sampled})")
    print("Чтение host.get, problem.get, trigger.get и item.get доступно.")


def main() -> int:
    try:
        from ..config import ZABBIX_API_TOKEN, ZABBIX_API_URL
    except (OSError, RuntimeError, ValueError):
        print("Не удалось загрузить настройки бота. Проверьте bot.json и env.secrets.", file=sys.stderr)
        return 1
    try:
        asyncio.run(inspect(ZabbixClient(ZABBIX_API_URL, ZABBIX_API_TOKEN)))
    except ZabbixError as exc:
        print(f"Проверка Zabbix не завершена: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
