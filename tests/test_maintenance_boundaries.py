from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app import storage
from app.config import TZ
from app.maintenance import calendar, jobs, operations, policy, records
from app.messaging.outbox import message_payload, process_outbox
from app.monitoring.zabbix import ZabbixError
from app.persistence.sqlite_backend import SQLiteBackend
from app.users.staff import STAFF_TITLE_MAINTAINER
from tests.product_support import _admin, _user


def test_scope_pages_reach_all_hosts_and_clamp_after_inventory_shrinks():
    hosts = {f"z{i}": f"Узел {i:03}" for i in range(1, 106)}
    found = []
    for page in range(11):
        buttons = [b.callback_data for row in calendar.scope_kb(hosts, page).inline_keyboard for b in row]
        keys = [data.removeprefix("maint:scope:") for data in buttons if data.startswith("maint:scope:z")]
        assert len(keys) <= 10
        assert ("maint:scope:page:" + str(page + 1) in buttons) == (page < 10)
        assert ("maint:scope:page:" + str(page - 1) in buttons) == (page > 0)
        assert "maint:scope:all" in buttons
        found.extend(keys)
    assert found == list(hosts)
    assert calendar.scope_kb({"z12": "Узел"}, 99) == calendar.scope_kb({"z12": "Узел"}, 0)


def test_unknown_maintenance_scope_never_expands_to_all_servers():
    assert policy.normalize_scope("removed") == "removed"
    assert policy.scope_label("removed") != policy.scope_label("all")


@pytest.mark.asyncio
async def test_schedule_for_removed_server_is_not_activated(isolated_storage, monkeypatch):
    now = datetime.now(TZ)
    scheduled = dict(
        records.build_scheduled_maintenance_record(
            "all", now - timedelta(minutes=1), now + timedelta(hours=1), 1, "Admin"
        )
    )
    scheduled["scope"] = "removed"
    await storage.set_scheduled_maintenance_record(scheduled)
    monkeypatch.setattr(jobs, "maintenance_manager_ids", lambda: [1])
    await jobs.maint_schedule_tick(SimpleNamespace())
    assert storage.get_active_maintenance() is None
    assert storage.get_scheduled_maintenance() is None
    assert any(event["kind"] == "maintenance_schedule_invalid" for _, event in storage.outbox_snapshot())


@pytest.mark.asyncio
@pytest.mark.parametrize("transition", ["cancel", "expire", "activate", "invalid"])
async def test_obsolete_schedule_warnings_are_removed_atomically(isolated_storage, transition):
    now = datetime.now(TZ)
    scheduled = dict(records.build_scheduled_maintenance_record("all", now, now + timedelta(hours=1), 1, "Admin"))
    await storage.set_scheduled_maintenance_record(scheduled)
    warning = storage.make_outbox_event(
        kind="maintenance_schedule_warning", recipient_ids=[1], payload=message_payload("Soon")
    )
    await operations.mark_schedule_thresholds(
        scheduled, updated_notified=[15], due=[15], updated_at=now, notice_event=warning
    )
    assert any(event["id"] == warning["id"] for _, event in storage.outbox_snapshot())
    if transition == "cancel":
        await operations.cancel_scheduled_maintenance(scheduled["id"], actor_id=1)
    elif transition == "expire":
        await operations.expire_schedule(scheduled, None)
    elif transition == "activate":
        await operations.activate_scheduled_maintenance(scheduled, None)
    else:
        await operations.clear_invalid_schedule(scheduled["id"])
    assert not any(event["id"] == warning["id"] for _, event in storage.outbox_snapshot())


@pytest.mark.parametrize(("year", "month"), [(2026, 99), (9999, 12), (0, 0)])
def test_calendar_safely_bounds_untrusted_navigation(year, month):
    today = datetime(2026, 9, 10).date()
    keyboard = calendar.schedule_calendar_kb(year, month, today=today)
    assert keyboard.inline_keyboard[0][0].text == "Сентябрь 2026"


async def _seed_active_maintenance(*, deadline: datetime) -> dict:
    await storage.update_user_data(
        lambda cfg: cfg.authorized_users.update({"1": _admin(1, staff_title=STAFF_TITLE_MAINTAINER), "42": _user(42)})
    )
    record = records.build_maintenance_record("all", "planned", 0, 30, 1, "Engineer")
    record["expected_end"] = deadline.isoformat()
    await storage.set_maintenance_record(record)
    return record


def _reminder_events(maintenance_id: str) -> list[dict]:
    kind = f"maintenance_admin_reminder_{maintenance_id}"
    return [event for _, event in storage.outbox_snapshot() if event["kind"] == kind]


@pytest.mark.asyncio
async def test_active_reminder_is_sent_once_only_after_declared_deadline(isolated_storage):
    deadline = datetime.now(TZ) + timedelta(hours=1)
    record = await _seed_active_maintenance(deadline=deadline)
    maintenance_id = record["id"]
    kind = f"maintenance_admin_reminder_{maintenance_id}"
    await jobs.maint_restart_notify(MagicMock())
    exact_deadline_event = storage.make_outbox_event(kind=kind, recipient_ids=[1], payload=message_payload("Overdue"))
    assert not await operations.queue_active_reminder(
        maintenance_id, deadline.isoformat(), deadline, kind, exact_deadline_event
    )
    assert not _reminder_events(maintenance_id)

    overdue_end = (datetime.now(TZ) - timedelta(minutes=1)).isoformat()
    await storage.set_maintenance_record({**record, "expected_end": overdue_end})
    await jobs.maint_restart_notify(MagicMock())
    await jobs.maint_restart_notify(MagicMock())

    reminders = _reminder_events(maintenance_id)
    assert len(reminders) == 1
    assert set(reminders[0]["recipients"]) == {"1"}
    assert "превысили заявленный срок" in reminders[0]["payload"]["text"]
    stored = SQLiteBackend(storage.storage_data_dir()).inspect().data("maintenance.state")
    assert stored["active"]["overdue_reminded_for"] == overdue_end

    bot = SimpleNamespace(send_message=AsyncMock(return_value=None))
    assert await process_outbox(bot) == 1
    assert not _reminder_events(maintenance_id)
    storage.initialize_storage(storage.storage_data_dir())
    await jobs.maint_restart_notify(MagicMock())
    assert not _reminder_events(maintenance_id)


@pytest.mark.asyncio
async def test_extending_maintenance_retires_old_reminder_and_arms_new_deadline(isolated_storage):
    record = await _seed_active_maintenance(deadline=datetime.now(TZ) - timedelta(minutes=1))
    maintenance_id = record["id"]
    old_end = record["expected_end"]
    await jobs.maint_restart_notify(MagicMock())
    assert len(_reminder_events(maintenance_id)) == 1

    extended, _, _ = await operations.extend_maintenance(
        maintenance_id,
        duration_min=15,
        hours=0,
        minutes=15,
        author="Engineer",
        author_id=1,
    )
    assert not _reminder_events(maintenance_id)
    assert extended["overdue_reminded_for"] == old_end
    kind = f"maintenance_admin_reminder_{maintenance_id}"
    stale_event = storage.make_outbox_event(kind=kind, recipient_ids=[1], payload=message_payload("Stale"))
    assert not await operations.queue_active_reminder(maintenance_id, old_end, datetime.now(TZ), kind, stale_event)

    new_end = datetime.fromisoformat(extended["expected_end"])
    new_event = storage.make_outbox_event(
        kind=kind, recipient_ids=[1], payload=message_payload("New deadline exceeded")
    )
    assert await operations.queue_active_reminder(
        maintenance_id, extended["expected_end"], new_end + timedelta(seconds=1), kind, new_event
    )
    assert not await operations.queue_active_reminder(
        maintenance_id, extended["expected_end"], new_end + timedelta(seconds=2), kind, new_event
    )
    assert len(_reminder_events(maintenance_id)) == 1
    assert storage.get_active_maintenance()["overdue_reminded_for"] == extended["expected_end"]


@pytest.mark.asyncio
async def test_ending_maintenance_cancels_pending_overdue_notice(isolated_storage):
    record = await _seed_active_maintenance(deadline=datetime.now(TZ) - timedelta(minutes=1))
    maintenance_id = record["id"]
    await jobs.maint_restart_notify(MagicMock())
    assert len(_reminder_events(maintenance_id)) == 1

    await operations.end_maintenance(maintenance_id, author="Engineer", author_id=1, ended_at=datetime.now(TZ))
    await jobs.maint_restart_notify(MagicMock())

    assert storage.get_active_maintenance() is None
    assert not _reminder_events(maintenance_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("expected_end", [None, "invalid"])
async def test_active_reminder_requires_a_valid_declared_deadline(isolated_storage, expected_end):
    record = await _seed_active_maintenance(deadline=datetime.now(TZ) - timedelta(minutes=1))
    record["expected_end"] = expected_end
    await storage.set_maintenance_record(record)

    await jobs.maint_restart_notify(MagicMock())

    assert not _reminder_events(record["id"])


@pytest.mark.asyncio
async def test_schedule_waits_for_zabbix_before_activation(isolated_storage, monkeypatch):
    now = datetime.now(TZ)
    schedule = records.build_scheduled_maintenance_record(
        "z42", now - timedelta(minutes=1), now + timedelta(hours=1), 1, "Admin", label="Узел А"
    )
    await storage.set_scheduled_maintenance_record(dict(schedule))
    inventory = AsyncMock(side_effect=[ZabbixError("timeout"), {"z42": "Узел А"}])
    monkeypatch.setattr(jobs, "server_items", inventory)

    await jobs.maint_schedule_tick(SimpleNamespace())
    assert storage.get_scheduled_maintenance()["id"] == schedule["id"]
    assert storage.get_active_maintenance() is None
    await jobs.maint_schedule_tick(SimpleNamespace())
    assert storage.get_scheduled_maintenance() is None
    assert storage.get_active_maintenance()["scope_label"] == "Узел А"
