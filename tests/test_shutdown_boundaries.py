from __future__ import annotations

import asyncio
import json

import pytest
from telegram.ext import ApplicationBuilder, ExtBot
from telegram.request import BaseRequest

from app.bot.job_queue import ManagedJobQueue


class LocalRequest(BaseRequest):
    read_timeout = 5

    async def initialize(self):
        pass

    async def shutdown(self):
        pass

    async def do_request(self, url, method, request_data=None, **kwargs):
        assert url.endswith("/getMe")
        result = {"ok": True, "result": {"id": 123, "is_bot": True, "first_name": "Local"}}
        return 200, json.dumps(result).encode()


@pytest.mark.asyncio
async def test_ptb_application_stop_cancels_shielded_scheduled_callback():
    queue = ManagedJobQueue(drain_timeout=0.01)
    bot = ExtBot("123:LOCAL_ONLY", request=LocalRequest(), get_updates_request=LocalRequest())
    application = ApplicationBuilder().bot(bot).job_queue(queue).build()
    started = asyncio.Event()
    closed = asyncio.Event()

    async def callback(context):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            closed.set()

    queue.run_once(queue.managed(callback), when=0.01)
    async with application:
        await application.start()
        try:
            await asyncio.wait_for(started.wait(), timeout=1)
        finally:
            await asyncio.wait_for(application.stop(), timeout=1)
    assert closed.is_set()
    assert not queue.scheduler.running


@pytest.mark.asyncio
async def test_shutdown_drains_completed_jobs_and_rejects_new_callbacks():
    queue = ManagedJobQueue(drain_timeout=0.1)
    called = []

    async def callback(context):
        await asyncio.sleep(0)
        called.append(context)

    task = asyncio.create_task(queue.managed(callback)("before"))
    await asyncio.sleep(0)
    await queue.stop()
    await task
    await queue.managed(callback)("after")
    assert called == ["before"]
    assert not queue._active_callbacks


@pytest.mark.asyncio
async def test_shutdown_cancels_stuck_jobs_and_awaits_resource_cleanup():
    queue = ManagedJobQueue(drain_timeout=0.01)
    started = asyncio.Event()
    closed = asyncio.Event()

    async def callback(context):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0.01)
            closed.set()

    task = asyncio.create_task(queue.managed(callback)(None))
    await started.wait()
    await asyncio.wait_for(queue.stop(), timeout=1)
    assert task.cancelled()
    assert closed.is_set()
    assert not queue._active_callbacks
